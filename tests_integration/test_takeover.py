"""Take over tracking in the Energy dashboard (#13): the pure replacement
rules, the websocket commands, the Energy dashboard prefs before and after,
Undo, and the Repairs issue of a deleted appliance."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator

from custom_components.energy_mapper.const import DOMAIN
from custom_components.energy_mapper.coordinator import ISSUE_TAKEOVER_PREFIX
from custom_components.energy_mapper.takeover import (
    REASON_DEVICE,
    REASON_ESTIMATE,
    TakeoverError,
    pickable,
    restore,
    suggestion,
    take_over,
)

from .test_appliances import full_registry, reconfigure, subentries, two_days
from .test_statistics import HEATER, KETTLE, mock_addon, settle, setup

ESTIMATE = "sensor.boiler_estimated_energy"  # estimated from the relay switch.water_heater
FRIDGE = "sensor.fridge_energy"
LAMP = "sensor.lamp_energy"  # nested in the estimate
SOLAR = {"type": "solar", "stat_energy_from": "sensor.solar_energy"}


def prefs_devices() -> list[dict[str, Any]]:
    return [
        {"stat_consumption": FRIDGE},
        {"stat_consumption": ESTIMATE, "name": "Boiler", "stat_rate": "sensor.boiler_estimated_power"},
        {"stat_consumption": LAMP, "included_in_stat": ESTIMATE},
    ]


# ---------------------------------------------------------------------------
# pure rules
# ---------------------------------------------------------------------------
def test_take_over_in_place() -> None:
    devices = [{"stat_consumption": "sensor.house_sub"}, *prefs_devices()]
    devices[2]["included_in_stat"] = "sensor.house_sub"
    before = [dict(d) for d in devices]
    out, record = take_over(devices, ESTIMATE, HEATER)
    assert devices == before  # not modified
    assert out == [
        {"stat_consumption": "sensor.house_sub"},
        {"stat_consumption": FRIDGE},
        # same place, same parent; no name (the appliance's), no estimated power
        {"stat_consumption": HEATER, "included_in_stat": "sensor.house_sub"},
        {"stat_consumption": LAMP, "included_in_stat": HEATER},
    ]
    assert record == {"statistic_id": HEATER, "replaced": before[2], "children": [LAMP]}
    # undo gives back exactly what was there
    assert restore(out, record) == before


def test_take_over_refusals() -> None:
    devices = prefs_devices()
    with pytest.raises(TakeoverError) as err:
        take_over(devices, "sensor.unknown_energy", HEATER)
    assert err.value.code == "not_found"
    with pytest.raises(TakeoverError) as err:
        take_over([*devices, {"stat_consumption": HEATER}], ESTIMATE, HEATER)
    assert err.value.code == "already_tracked"
    with pytest.raises(TakeoverError) as err:
        take_over([*devices, {"stat_consumption": KETTLE}], KETTLE, HEATER)
    assert err.value.code == "invalid_format"
    assert [d["stat_consumption"] for d in pickable([*devices, {"stat_consumption": KETTLE}])] == [
        FRIDGE,
        ESTIMATE,
        LAMP,
    ]


def test_restore_never_leaves_both() -> None:
    out, record = take_over(prefs_devices(), ESTIMATE, HEATER)
    # the old device was added again by hand: the statistic just leaves
    again = [*out, {"stat_consumption": ESTIMATE}]
    restored = restore(again, record)
    assert [d["stat_consumption"] for d in restored] == [FRIDGE, LAMP, ESTIMATE]
    assert restored[1]["included_in_stat"] == ESTIMATE
    # the statistic was removed by hand: the old device comes back at the end
    gone = [d for d in out if d["stat_consumption"] != HEATER]
    restored = restore(gone, record)
    assert [d["stat_consumption"] for d in restored] == [FRIDGE, LAMP, ESTIMATE]
    assert all(d["stat_consumption"] != HEATER for d in restored)
    # the statistic was nested meanwhile: the old device takes its nesting
    moved = [{**d, "included_in_stat": FRIDGE} if d["stat_consumption"] == HEATER else d for d in out]
    assert restore(moved, record)[1] == {
        "stat_consumption": ESTIMATE,
        "name": "Boiler",
        "stat_rate": "sensor.boiler_estimated_power",
        "included_in_stat": FRIDGE,
    }


def test_suggestion() -> None:
    relays = ["switch.water_heater"]
    source_of = {ESTIMATE: "switch.water_heater", FRIDGE: None, "sensor.plug_energy": None}
    device_of = {"switch.water_heater": "dev1", "sensor.plug_energy": "dev1", FRIDGE: "dev2"}
    assert suggestion({"stat_consumption": ESTIMATE}, relays, source_of, device_of) == REASON_ESTIMATE
    assert (
        suggestion({"stat_consumption": "sensor.plug_energy"}, relays, source_of, device_of) == REASON_DEVICE
    )
    assert suggestion({"stat_consumption": FRIDGE}, relays, source_of, device_of) is None
    assert suggestion({"stat_consumption": ESTIMATE}, [], source_of, device_of) is None
    assert (
        suggestion({"stat_consumption": HEATER}, relays, {HEATER: "switch.water_heater"}, device_of) is None
    )


# ---------------------------------------------------------------------------
# Home Assistant
# ---------------------------------------------------------------------------
async def _call(client, **msg) -> dict[str, Any]:
    await client.send_json_auto_id(msg)
    return await client.receive_json()


async def _prepare(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry):
    """Energy Mapper with a heater (relay) and a kettle (signature), and an
    Energy dashboard where the heater is estimated from its relay."""
    mock_addon(aioclient_mock, two_days(), full_registry())
    await setup(hass, entry)
    hass.states.async_set(
        ESTIMATE, "12.5", {"source_entity": "switch.water_heater", "friendly_name": "Boiler"}
    )
    hass.states.async_set(FRIDGE, "3.1", {"friendly_name": "Fridge"})
    hass.states.async_set(LAMP, "0.4", {"friendly_name": "Lamp"})
    manager = await async_get_manager(hass)
    await manager.async_update({"energy_sources": [SOLAR], "device_consumption": prefs_devices()})
    return manager


async def test_commands_are_for_admins(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    manager = await _prepare(hass, aioclient_mock, entry)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    for msg in (
        {"type": "energy_mapper/tracking"},
        {
            "type": "energy_mapper/take_over",
            "appliance": "load:switch.water_heater",
            "stat_consumption": ESTIMATE,
        },
        {"type": "energy_mapper/undo_take_over", "appliance": "load:switch.water_heater"},
    ):
        answer = await _call(client, **msg)
        assert answer["error"]["code"] == "unauthorized", msg
    assert manager.data["device_consumption"] == prefs_devices()


async def test_take_over_and_undo(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    manager = await _prepare(hass, aioclient_mock, entry)
    client = await hass_ws_client(hass)

    # the estimate of the heater's relay is suggested; any other device can be picked
    answer = await _call(client, type="energy_mapper/tracking")
    assert answer["success"], answer
    result = answer["result"]
    assert result["devices"] == [
        {"stat_consumption": FRIDGE, "name": "Fridge", "included_in_stat": None},
        {"stat_consumption": ESTIMATE, "name": "Boiler", "included_in_stat": None},
        {"stat_consumption": LAMP, "name": "Lamp", "included_in_stat": ESTIMATE},
    ]
    by_key = {a["key"]: a for a in result["appliances"]}
    heater = by_key["load:switch.water_heater"]
    assert heater == {
        "key": "load:switch.water_heater",
        "statistic_id": HEATER,
        "in_dashboard": False,
        "taken_over": None,
        "suggested": [{"stat_consumption": ESTIMATE, "reason": REASON_ESTIMATE}],
    }
    assert by_key["signature:short-2000w"]["suggested"] == []

    # take over: one update, in place, nothing else touched
    answer = await _call(
        client,
        type="energy_mapper/take_over",
        appliance="load:switch.water_heater",
        stat_consumption=ESTIMATE,
    )
    assert answer["success"], answer
    assert manager.data["energy_sources"] == [SOLAR]
    assert manager.data["device_consumption"] == [
        {"stat_consumption": FRIDGE},
        {"stat_consumption": HEATER},
        {"stat_consumption": LAMP, "included_in_stat": HEATER},
    ]
    record = entry.runtime_data.coordinator.takeovers["load:switch.water_heater"]
    assert record["replaced"] == prefs_devices()[1]
    heater = {
        a["key"]: a for a in (await _call(client, type="energy_mapper/tracking"))["result"]["appliances"]
    }["load:switch.water_heater"]
    assert heater["in_dashboard"] is True
    assert heater["taken_over"] == {"stat_consumption": ESTIMATE, "name": "Boiler"}
    assert heater["suggested"] == []

    # never two sources: a second take-over of the same appliance is refused
    answer = await _call(
        client, type="energy_mapper/take_over", appliance="load:switch.water_heater", stat_consumption=FRIDGE
    )
    assert answer["error"]["code"] == "already_tracked"
    # the kettle cannot take over a device that is no longer there
    answer = await _call(
        client, type="energy_mapper/take_over", appliance="signature:short-2000w", stat_consumption=ESTIMATE
    )
    assert answer["error"]["code"] == "not_found"
    answer = await _call(client, type="energy_mapper/take_over", appliance="nope", stat_consumption=FRIDGE)
    assert answer["error"]["code"] == "not_found"

    # undo: the estimate is back where it was
    answer = await _call(client, type="energy_mapper/undo_take_over", appliance="load:switch.water_heater")
    assert answer["success"], answer
    assert manager.data["device_consumption"] == prefs_devices()
    assert entry.runtime_data.coordinator.takeovers == {}
    answer = await _call(client, type="energy_mapper/undo_take_over", appliance="load:switch.water_heater")
    assert answer["error"]["code"] == "not_found"


async def test_appliance_without_statistic(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    """A power sensor's appliance has no statistic of its own; one whose
    statistic is not imported yet cannot take over either."""
    manager = await _prepare(hass, aioclient_mock, entry)
    await reconfigure(
        hass, entry, "signature:medium-2000w", "link_sensor", {"entity_id": "sensor.plug_power"}
    )
    client = await hass_ws_client(hass)
    keys = {a["key"] for a in (await _call(client, type="energy_mapper/tracking"))["result"]["appliances"]}
    assert "signature:medium-2000w" not in keys
    answer = await _call(
        client, type="energy_mapper/take_over", appliance="signature:medium-2000w", stat_consumption=FRIDGE
    )
    assert answer["error"]["code"] == "no_statistic"
    assert manager.data["device_consumption"] == prefs_devices()


async def test_suggested_by_device(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    """An energy sensor of the relay's own device (a smart plug) is suggested."""
    manager = await _prepare(hass, aioclient_mock, entry)
    await manager.async_update(
        {"device_consumption": [*prefs_devices(), {"stat_consumption": "sensor.plug_energy"}]}
    )
    other = MockConfigEntry(domain="test")
    other.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("test", "plug")}
    )
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "switch", "test", "relay", suggested_object_id="water_heater", device_id=dev.id
    )
    plug = ent_reg.async_get_or_create(
        "sensor", "test", "plug", suggested_object_id="plug_energy", device_id=dev.id
    )
    assert plug.entity_id == "sensor.plug_energy"
    client = await hass_ws_client(hass)
    heater = {
        a["key"]: a for a in (await _call(client, type="energy_mapper/tracking"))["result"]["appliances"]
    }["load:switch.water_heater"]
    assert heater["suggested"] == [
        {"stat_consumption": ESTIMATE, "reason": REASON_ESTIMATE},
        {"stat_consumption": "sensor.plug_energy", "reason": REASON_DEVICE},
    ]


async def test_deleted_appliance_offers_a_restore(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    manager = await _prepare(hass, aioclient_mock, entry)
    assert await async_setup_component(hass, "repairs", {})
    client = await hass_ws_client(hass)
    answer = await _call(
        client,
        type="energy_mapper/take_over",
        appliance="load:switch.water_heater",
        stat_consumption=ESTIMATE,
    )
    assert answer["success"], answer
    issue_id = ISSUE_TAKEOVER_PREFIX + "load:switch.water_heater"

    mock_addon(aioclient_mock, two_days(), full_registry())
    hass.config_entries.async_remove_subentry(
        entry, subentries(entry)["load:switch.water_heater"].subentry_id
    )
    await settle(hass)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.is_fixable
    assert issue.translation_placeholders == {"name": "Water heater", "device": "Boiler"}
    # nothing changed without a confirmation
    assert {"stat_consumption": HEATER} in manager.data["device_consumption"]
    # the record is stored: after a restart (issues not persistent) the issue is back
    ir.async_delete_issue(hass, DOMAIN, issue_id)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    http = await hass_client()
    resp = await http.post("/api/repairs/issues/fix", json={"handler": DOMAIN, "issue_id": issue_id})
    assert resp.status == 200
    flow = await resp.json()
    assert flow["step_id"] == "confirm"
    assert flow["description_placeholders"] == {"name": "Water heater", "device": "Boiler"}
    resp = await http.post(f"/api/repairs/issues/fix/{flow['flow_id']}", json={})
    assert resp.status == 200
    assert (await resp.json())["type"] == "create_entry"
    assert manager.data["device_consumption"] == prefs_devices()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert entry.runtime_data.coordinator.takeovers == {}
