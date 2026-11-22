"""The live view subscription (#17): one long-poll of a fake add-on, streamed
to every subscribed panel."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.energy_mapper import live as live_module
from custom_components.energy_mapper.const import DOMAIN

from .conftest import URL

T0 = 1_800_000_000.0
SEQ = 1_800_000_000_001


def event(eid: int, seq: int, **kw: Any) -> dict[str, Any]:
    return {
        "id": eid,
        "seq": seq,
        "t_on": T0 - 60,
        "delta_w": 2000,
        "state": "confirmed",
        "running": True,
        "t_off": None,
        "off_w": None,
        "duration_s": None,
        "power_w": 2000,
        "session": None,
        "pv_moved": False,
        "confidence": "unknown",
        "candidates": [],
        "steps": [[T0 - 60, 2000]],
        **kw,
    }


def now(residual: int, running: list[int]) -> dict[str, Any]:
    return {"ts": T0, "house_w": residual, "residual_w": residual, "running": running, "switches": []}


SNAPSHOT = {
    "seq": SEQ + 1,
    "session": SEQ,
    "available": True,
    "reset": True,
    "history_s": 900.0,
    "threshold_w": 300.0,
    "night_threshold_w": 150.0,
    "time_zone": "UTC",
    "curve": [[T0 - 4, 2300, 2300], [T0 - 2, 2300, 2300]],
    "events": [event(1, SEQ + 1)],
    "now": now(2300, [1]),
}


class FakeLive:
    """``GET /api/live``: answers what the test queues, one per request."""

    def __init__(self) -> None:
        self.answers: asyncio.Queue = asyncio.Queue()
        self.queries: list[dict[str, str]] = []

    def answer(self, body: dict[str, Any] | Exception, status: int = 200) -> None:
        self.answers.put_nowait((status, body))

    async def handler(self, method, url, data):
        self.queries.append(dict(url.query))
        status, body = await self.answers.get()
        if isinstance(body, Exception):
            return AiohttpClientMockResponse(method, url, exc=body)
        return AiohttpClientMockResponse(method, url, status=status, json=body)


@pytest.fixture
def fake_live(addon: AiohttpClientMocker) -> FakeLive:
    fake = FakeLive()
    addon.get(f"{URL}/api/live", side_effect=fake.handler)
    return fake


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_module, "RETRY_S", dict.fromkeys(live_module.RETRY_S, 0.01))
    monkeypatch.setattr(live_module, "UNAVAILABLE_RETRY_S", (0.01,))
    monkeypatch.setattr(live_module, "OTHER_ERROR_RETRY_S", 0.01)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _subscribe(client) -> int:
    await client.send_json_auto_id({"type": "energy_mapper/live/subscribe"})
    msg = await client.receive_json()
    assert msg["success"], msg
    return msg["id"]


async def _event(client) -> dict[str, Any]:
    msg = await asyncio.wait_for(client.receive_json(), 5)
    assert msg["type"] == "event", msg
    return msg["event"]


async def test_stream_to_every_user(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    fake_live: FakeLive,
    entry: MockConfigEntry,
) -> None:
    await _setup(hass, entry)
    admin = await hass_ws_client(hass)
    sub_id = await _subscribe(admin)

    fake_live.answer(SNAPSHOT)
    snap = await _event(admin)
    assert snap["type"] == "snapshot"
    assert snap["available"] is True
    assert snap["seq"] == SEQ + 1
    assert snap["curve"] == SNAPSHOT["curve"]
    assert [e["id"] for e in snap["events"]] == [1]
    assert snap["threshold_w"] == 300.0
    assert snap["night_threshold_w"] == 150.0
    assert fake_live.queries[0] == {"wait": "25"}  # first: a snapshot

    # an update: a new sample, the event completed, a new provisional one
    update = {
        "seq": SEQ + 2,
        "session": SEQ,
        "available": True,
        "reset": False,
        "curve": [[T0, 300, 300]],
        "events": [
            event(1, SEQ + 2, running=False, t_off=T0 - 1, off_w=2000, duration_s=59),
            event(2, SEQ + 2, state="provisional", delta_w=800, t_on=T0),
        ],
        "now": now(300, []),
    }
    fake_live.answer(update)
    msg = await _event(admin)
    assert msg == {
        "type": "update",
        "seq": SEQ + 2,
        "curve": update["curve"],
        "events": update["events"],
        "now": update["now"],
    }
    assert fake_live.queries[1] == {"wait": "25", "since": str(SEQ + 1)}

    # a non-admin user joins: the merged snapshot at once (read-only, open to all)
    user = await hass_ws_client(hass, hass_read_only_access_token)
    await _subscribe(user)
    joined = await _event(user)
    assert joined["type"] == "snapshot"
    assert joined["seq"] == SEQ + 2
    assert [p[0] for p in joined["curve"]] == [T0 - 4, T0 - 2, T0]
    assert [(e["id"], e["running"]) for e in joined["events"]] == [(1, False), (2, True)]
    assert joined["now"] == update["now"]

    # the provisional step was a spike: cancelled, gone from the snapshot
    fake_live.answer(
        {
            "seq": SEQ + 3,
            "session": SEQ,
            "available": True,
            "reset": False,
            "curve": [],
            "events": [event(2, SEQ + 3, state="cancelled", running=False)],
            "now": update["now"],
        }
    )
    for client in (admin, user):
        msg = await _event(client)
        assert msg["type"] == "update"
        assert msg["events"][0]["state"] == "cancelled"
    hub = entry.runtime_data.live
    assert [e["id"] for e in hub._snapshot_message()["events"]] == [1]

    # the add-on started a new session (restart, configuration change)
    fake_live.answer({**SNAPSHOT, "seq": SEQ + 10, "session": SEQ + 9, "events": []})
    for client in (admin, user):
        msg = await _event(client)
        assert msg["type"] == "snapshot"
        assert msg["events"] == []

    # both leave: the poll stops (the add-on stops 2 minutes later)
    await admin.send_json_auto_id({"type": "unsubscribe_events", "subscription": sub_id})
    assert (await admin.receive_json())["success"]
    assert hub.subscribers == 1
    await user.close()
    await hass.async_block_till_done()
    assert hub.subscribers == 0
    assert hub._task is None


async def test_unavailable_reasons(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_live: FakeLive,
    entry: MockConfigEntry,
) -> None:
    await _setup(hass, entry)
    client = await hass_ws_client(hass)
    await _subscribe(client)

    fake_live.answer({"seq": SEQ, "available": False, "reason": "disabled"})
    assert await _event(client) == {"type": "status", "available": False, "reason": "disabled"}
    fake_live.answer({"seq": SEQ, "available": False, "reason": "disabled"})  # same: not repeated
    fake_live.answer({"seq": SEQ, "available": False, "reason": "overloaded", "retry_at": T0 + 300})
    assert await _event(client) == {
        "type": "status",
        "available": False,
        "reason": "overloaded",
        "retry_at": T0 + 300,
    }
    fake_live.answer(
        {
            "seq": SEQ,
            "available": False,
            "reason": "error",
            "error": {"code": "no_grid", "message": "no grid power sensor"},
            "retry_at": T0 + 30,
        }
    )
    msg = await _event(client)
    assert (msg["reason"], msg["error"]["code"]) == ("error", "no_grid")
    fake_live.answer(aiohttp.ClientConnectionError("refused"))
    msg = await _event(client)
    assert msg["reason"] == "addon_unavailable"
    # back: a snapshot (the poll starts again without since)
    fake_live.answer(SNAPSHOT)
    assert (await _event(client))["type"] == "snapshot"
    assert all("since" not in q for q in fake_live.queries[:6])
    # a late panel gets the snapshot, not the old status
    other = await hass_ws_client(hass)
    await _subscribe(other)
    assert (await _event(other))["type"] == "snapshot"


async def test_addon_too_old(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    addon: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    addon.get(f"{URL}/api/live", status=404, json={"error": "not_found"})
    await _setup(hass, entry)
    client = await hass_ws_client(hass)
    await _subscribe(client)
    assert await _event(client) == {
        "type": "status",
        "available": False,
        "reason": "addon_too_old",
        "min_version": "0.12.0",
        "addon_version": "0.3.0",
    }


async def test_not_loaded(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "energy_mapper/live/subscribe"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_loaded"


async def test_unload(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_live: FakeLive,
    entry: MockConfigEntry,
) -> None:
    await _setup(hass, entry)
    client = await hass_ws_client(hass)
    await _subscribe(client)
    fake_live.answer(SNAPSHOT)
    assert (await _event(client))["type"] == "snapshot"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await _event(client) == {"type": "status", "available": False, "reason": "not_loaded"}
