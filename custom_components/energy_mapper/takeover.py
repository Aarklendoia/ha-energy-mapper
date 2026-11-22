"""Take over tracking (#13): an appliance's statistic in place of a device of
the Energy dashboard.

Users often track a device in the Energy dashboard with an estimate (a
powercalc sensor driven by its relay, a fixed-power template). Once Energy
Mapper measures the appliance, its hourly statistic can replace that entry.
Two sources for one device would count it twice, so the replacement is one
update of ``device_consumption``: the old entry goes, the statistic takes its
place, in the same position.

Pure functions on the plain prefs (``energy/get_prefs``), testable without
Home Assistant:

- :func:`suggestion` tells whether a device of the dashboard is probably the
  appliance (estimated from one of its relays, or on the same Home Assistant
  device as one of them);
- :func:`take_over` replaces an entry and returns what it replaced (the
  record kept for *Undo*);
- :func:`restore` puts the replaced entry back, never leaving both.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .const import DOMAIN

PREFIX = f"{DOMAIN}:"

# why a device of the dashboard is suggested for an appliance
REASON_ESTIMATE = "estimate"  # its energy sensor is estimated from a relay of the appliance
REASON_DEVICE = "device"  # it belongs to the Home Assistant device of a relay of the appliance


class TakeoverError(Exception):
    """A take-over or an undo that cannot be applied; ``code`` is a websocket error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def is_own(stat_id: str | None) -> bool:
    """One of Energy Mapper's statistics (never taken over)."""
    return bool(stat_id) and str(stat_id).startswith(PREFIX)


def stat_of(entry: Mapping[str, Any]) -> str | None:
    return entry.get("stat_consumption")


def in_dashboard(devices: Iterable[Mapping[str, Any]], stat_id: str) -> bool:
    return any(stat_of(d) == stat_id for d in devices)


def pickable(devices: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The devices of the dashboard an appliance may take over: all but
    Energy Mapper's own statistics."""
    return [d for d in devices if stat_of(d) and not is_own(stat_of(d))]


def suggestion(
    entry: Mapping[str, Any],
    relays: Iterable[str],
    source_of: Mapping[str, str | None],
    device_of: Mapping[str, str | None],
) -> str | None:
    """Why this dashboard device is probably the appliance with these relays
    (:data:`REASON_ESTIMATE`, :data:`REASON_DEVICE`), or None.

    ``source_of``: entity id -> its ``source_entity`` attribute (estimation
    integrations); ``device_of``: entity id -> its Home Assistant device."""
    stat = stat_of(entry)
    relays = set(relays)
    if not stat or is_own(stat) or not relays:
        return None
    source = source_of.get(stat)
    if source and source in relays:
        return REASON_ESTIMATE
    relay_devices = {device_of.get(r) for r in relays} - {None}
    if device_of.get(stat) in relay_devices or (source and device_of.get(source) in relay_devices):
        return REASON_DEVICE
    return None


def take_over(
    devices: list[Mapping[str, Any]], old_stat: str, new_stat: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``device_consumption`` with ``old_stat`` replaced by ``new_stat``, and
    the record of what was replaced (``{"statistic_id", "replaced",
    "children"}``).

    The new entry keeps the position and the ``included_in_stat`` of the old
    one; it has no ``name`` (the statistic's own name, the appliance's,
    follows renames) and no ``stat_rate`` (the old one was the estimate's
    power). The devices nested in the old entry are nested in the new one.
    Refused (:class:`TakeoverError`) when the old entry is not there
    (``not_found``), is one of ours (``invalid_format``), or when the new
    statistic is already in the dashboard (``already_tracked``): never two
    sources for one device."""
    if is_own(old_stat):
        raise TakeoverError("invalid_format", f"{old_stat} is an Energy Mapper statistic")
    if in_dashboard(devices, new_stat):
        raise TakeoverError("already_tracked", f"{new_stat} is already in the Energy dashboard")
    index = next((i for i, d in enumerate(devices) if stat_of(d) == old_stat), None)
    if index is None:
        raise TakeoverError("not_found", f"{old_stat} is not an individual device of the Energy dashboard")
    old = dict(devices[index])
    new: dict[str, Any] = {"stat_consumption": new_stat}
    if old.get("included_in_stat"):
        new["included_in_stat"] = old["included_in_stat"]
    out: list[dict[str, Any]] = []
    children: list[str] = []
    for i, d in enumerate(devices):
        if i == index:
            out.append(new)
        elif d.get("included_in_stat") == old_stat:
            out.append({**d, "included_in_stat": new_stat})
            children.append(str(stat_of(d)))
        else:
            out.append(dict(d))
    return out, {"statistic_id": new_stat, "replaced": old, "children": children}


def restore(devices: list[Mapping[str, Any]], record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``device_consumption`` with the entry ``record`` replaced put back.

    - The statistic is there: the old entry takes its place (with its current
      ``included_in_stat``), or, when the old entry is already back (added
      again by hand), the statistic is just removed.
    - The statistic is gone and the old entry too: the old entry is added at
      the end.
    - The devices nested in the statistic are nested in the old entry again.

    Never both: the statistic always leaves."""
    ours = str(record["statistic_id"])
    old = dict(record["replaced"])
    old_stat = stat_of(old)
    if not old_stat:
        raise TakeoverError("invalid_format", "nothing to restore")
    has_old = in_dashboard(devices, old_stat)
    out: list[dict[str, Any]] = []
    placed = has_old
    for d in devices:
        if stat_of(d) == ours:
            if not placed:
                back = {k: v for k, v in old.items() if k != "included_in_stat"}
                if d.get("included_in_stat"):
                    back["included_in_stat"] = d["included_in_stat"]
                out.append(back)
                placed = True
            continue
        out.append(dict(d))
    if not placed:
        parents = {stat_of(d) for d in out}
        if old.get("included_in_stat") not in parents:
            old.pop("included_in_stat", None)
        out.append(old)
    for d in out:
        if d.get("included_in_stat") == ours:
            d["included_in_stat"] = old_stat
    return out


def label(entry: Mapping[str, Any], names: Mapping[str, str]) -> str:
    """The name the dashboard shows for a device: its custom name, else the
    friendly name of its sensor, else its statistic id."""
    stat = str(stat_of(entry) or "")
    return str(entry.get("name") or names.get(stat) or stat)
