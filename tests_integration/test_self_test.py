"""Nightly self-test (#15): the websocket command relays the add-on's
success rates to every user, and tells when the add-on is too old."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from .conftest import URL
from .test_appliances import two_days
from .test_review import reg, replace
from .test_statistics import mock_addon, setup

DEVICE = {
    "entity_id": "sensor.heater_power",
    "name": "Water heater",
    "appliance": None,
    "state": "ok",
    "tests": 6,
    "uses": 6,
    "found": 5,
    "rate": 0.833,
    "precision": 1.0,
    "energy_ratio": 0.97,
    "trend": "stable",
    "last_day": "2027-03-09",
}
LISTING = {
    "enabled": True,
    "devices_per_night": 1,
    "window_days": 60,
    "min_uses": 5,
    "last_day": "2027-03-09",
    "devices": [DEVICE],
}


async def _call(client, **msg):
    await client.send_json_auto_id(msg)
    return await client.receive_json()


async def test_self_test_for_every_user(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
) -> None:
    mock_addon(aioclient_mock, two_days(), reg())
    aioclient_mock.get(f"{URL}/api/self_test", json=LISTING)
    await setup(hass, entry)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    answer = await _call(client, type="energy_mapper/self_test")
    assert answer["success"], answer
    assert answer["result"] == {**LISTING, "supported": True}

    # an add-on before 0.11.0
    replace(aioclient_mock, "get", "/api/self_test", status=404, json={"error": "not_found"})
    answer = await _call(client, type="energy_mapper/self_test")
    assert answer["result"] == {"supported": False, "enabled": False, "devices": []}
