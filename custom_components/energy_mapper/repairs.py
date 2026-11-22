"""Repairs fix flows of Energy Mapper.

``takeover_removed_<key>`` (#13): an appliance that had taken over a device
of the Energy dashboard was deleted (or merged into another), so its
statistic is gone; after the user's confirmation the device it replaced is
put back in the Energy dashboard.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .takeover import TakeoverError


class RestoreTakeoverFlow(ConfirmRepairFlow):
    """Confirm, then restore the device the deleted appliance took over."""

    def __init__(self, entry_id: str, key: str) -> None:
        self._entry_id = entry_id
        self._key = key

    async def async_step_confirm(self, user_input: dict[str, str] | None = None):
        if user_input is None:
            return await super().async_step_confirm()
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        try:
            await entry.runtime_data.coordinator.async_undo_take_over(self._key)
        except TakeoverError:
            return self.async_abort(reason="nothing_to_restore")
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    data = data or {}
    return RestoreTakeoverFlow(str(data.get("entry_id")), str(data.get("key")))
