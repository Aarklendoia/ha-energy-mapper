"""Labelling a live event (#17): websocket ``energy_mapper/live/label``.

The user points at a line of the panel's live view (or at the use the guided
mode measured) and says what it is. The use becomes a **labelled example**
of an appliance (``appliances.py``, stored in its subentry), and, depending
on the target, the existing paths of the review and of the appliances do the
rest:

* ``signature``: an unnamed signature of the registry gets a name: the
  review's *validate* decision, creating the appliance (``async_validate``).
  A signature that already belongs to an appliance only adds the example.
* ``appliance``: an existing appliance; with ``signature_id``, an unnamed
  signature joins its sources (``async_merge_signature``, #43), one of its
  own is a confirmation, one of another appliance is a disagreement ("No,
  it is..."): only the example is kept, the nightly analysis decides.
* ``new``: a new appliance and how to track it: ``curve`` (recognised in the
  whole-house curve: when the use is whole, its signature at once through
  ``POST /api/signatures``; else when the stop arrives or at night),
  ``switch`` / ``sensor`` (an entity source) or ``teacher`` (a power sensor
  that teaches it, #14).
* ``ignore``: "never propose this again", the review's *ignore* decision.

The answer says what happened (``effect``) so the panel can tell the user.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from homeassistant.util import dt as dt_util

from .api import EnergyMapperApiError
from .appliances import EXAMPLE_LIVE, Appliance, Teacher, add_example, signature, signature_group
from .coordinator import EnergyMapperCoordinator, ReviewError

TARGETS = ("signature", "appliance", "new", "ignore")
TRACKS = ("curve", "switch", "sensor", "teacher")

# what the label did (``effect``)
APPLIANCE_CREATED = "appliance_created"
SIGNATURE_NAMED = "signature_named"
SIGNATURE_MERGED = "signature_merged"
EXAMPLE_ADDED = "example_added"
SIGNATURE_IGNORED = "signature_ignored"


@dataclass
class Use:
    """The labelled use: from the live event and/or the explicit times."""

    example: dict[str, Any]
    cycling: bool = False

    @property
    def open(self) -> bool:
        return self.example.get("t_off") is None


async def _use(coordinator: EnergyMapperCoordinator, msg: dict[str, Any]) -> Use:
    event: dict[str, Any] | None = None
    session = msg.get("session")
    explicit = msg.get("t_on") is not None
    if msg.get("event_id") is not None:
        try:
            answer = await coordinator.client.live_event(msg["event_id"], session)
        except EnergyMapperApiError as err:
            # gone from the session: the explicit times (if any) are enough
            if err.code not in ("not_found", "session_ended") or not explicit:
                raise ReviewError(err.code, err.message) from err
        else:
            event, session = answer.get("event") or {}, answer.get("session")
    if event is None and not explicit:
        raise ReviewError("invalid_format", "expected a live event (event_id) or its times (t_on)")
    if event is not None and not explicit and float(event.get("delta_w") or 0) <= 0:
        raise ReviewError("invalid_event", "the switch-on of this event was not seen")
    if event is not None and event.get("state") == "cancelled":
        raise ReviewError("invalid_event", "this event was cancelled")
    ev = event or {}
    t_on = float(msg["t_on"] if explicit else ev["t_on"])
    # an event's stop, once it stopped; the explicit one wins (guided mode)
    stopped = None if ev.get("running", True) else ev.get("t_off")
    t_off = msg.get("t_off", stopped)
    power = msg.get("power_w") or ev.get("power_w") or abs(float(ev.get("delta_w") or 0))
    if not power:
        raise ReviewError("invalid_format", "expected the power of the use (power_w)")
    if t_off is not None and not t_on < float(t_off) <= t_on + 86400:
        raise ReviewError("invalid_format", "t_off: expected a time after t_on, within a day")
    now = dt_util.utcnow().timestamp()
    if t_on > now + 60:
        raise ReviewError("invalid_format", "t_on: expected a time in the past")
    example: dict[str, Any] = {
        "t_on": round(t_on, 1),
        "t_off": None if t_off is None else round(float(t_off), 1),
        "power_w": round(float(power)),
        "source": msg.get("source") or EXAMPLE_LIVE,
        "created": round(now, 1),
    }
    if t_off is None and event is not None and session is not None:
        # completed when the event stops (LiveHub -> async_live_events)
        example["live"] = {"id": int(ev["id"]), "session": int(session)}
    return Use(example, cycling=bool(ev.get("session")))


def _signature_answer(sig: dict[str, Any] | None, created: bool = False) -> dict[str, Any] | None:
    if not sig:
        return None
    return {
        "id": sig["id"],
        "kind": sig.get("kind"),
        "band": sig.get("band"),
        "power_w": sig.get("power_w"),
        "past_uses": int(sig.get("count") or 0),
        "created": created,
    }


def _appliance_answer(appliance: Appliance | None) -> dict[str, Any] | None:
    if appliance is None:
        return None
    return {
        "key": appliance.key,
        "name": appliance.name,
        "sources": [s.as_dict() for s in appliance.sources],
        "teacher": appliance.teacher.as_dict() if appliance.teacher else None,
        "examples": len(appliance.examples),
    }


def _name(msg: dict[str, Any]) -> str:
    name = (msg.get("name") or "").strip()
    if not name or len(name) > 100:
        raise ReviewError("invalid_format", "expected a name of 1 to 100 characters")
    return name


async def _registry_signature(coordinator: EnergyMapperCoordinator, sid: str) -> dict[str, Any]:
    registry = await coordinator.client.signatures()
    sig = next((s for s in registry.get("signatures") or [] if s.get("id") == sid), None)
    if sig is None:
        raise ReviewError("not_found", f"no signature {sid}")
    return sig


def _owner(coordinator: EnergyMapperCoordinator, sig: dict[str, Any]) -> Appliance | None:
    source = signature(sig)
    return next((a for a in coordinator.appliances_by_subentry().values() if a.has(source)), None)


async def async_label(coordinator: EnergyMapperCoordinator, msg: dict[str, Any]) -> dict[str, Any]:
    """Apply one label; returns ``{"effect", "appliance", "signature",
    "example", "open", "confirmed", "other_appliance"}``."""
    target = msg["target"]
    answer: dict[str, Any] = {
        "effect": None,
        "appliance": None,
        "signature": None,
        "example": None,
        "open": False,
        "confirmed": False,
        "other_appliance": None,
    }
    if target == "ignore":
        sid = msg.get("signature_id")
        if not sid:
            raise ReviewError("invalid_format", "ignore: expected signature_id")
        decided = await coordinator.async_review_decision(sid, "ignore")
        return {
            **answer,
            "effect": SIGNATURE_IGNORED,
            "signature": _signature_answer(decided.get("signature")),
        }

    use = await _use(coordinator, msg)
    example = use.example
    answer["open"] = use.open
    key: str | None = None

    if target == "signature":
        sid = msg.get("signature_id")
        if not sid:
            raise ReviewError("invalid_format", "signature: expected signature_id and name")
        name = _name(msg)
        sig = await _registry_signature(coordinator, sid)
        owner = _owner(coordinator, sig)
        example["signature_id"] = sid
        answer["signature"] = _signature_answer(sig)
        if owner is not None:
            # already one of an appliance's: a confirmation of that appliance
            await coordinator.async_add_example(owner.key, example)
            key, answer["effect"], answer["confirmed"] = owner.key, EXAMPLE_ADDED, True
        else:
            appliance = await coordinator.async_validate(
                sid, name, msg.get("entity_id"), msg.get("on_states"), examples=[example]
            )
            key, answer["effect"] = appliance.key, SIGNATURE_NAMED

    elif target == "appliance":
        key = msg.get("appliance")
        if not key or coordinator.appliance_of_key(key) is None:
            raise ReviewError("not_found", f"no appliance {key}")
        sid = msg.get("signature_id")
        answer["effect"] = EXAMPLE_ADDED
        if sid:
            sig = await _registry_signature(coordinator, sid)
            owner = _owner(coordinator, sig)
            example["signature_id"] = sid
            if owner is None and not sig.get("label"):
                await coordinator.async_merge_signature(sid, key, lambda a: _with(a, example))
                answer["effect"], answer["signature"] = SIGNATURE_MERGED, _signature_answer(sig)
            else:
                await coordinator.async_add_example(key, example)
                if owner is not None and owner.key == key:
                    answer["confirmed"] = True
                elif owner is not None:
                    answer["other_appliance"] = {"key": owner.key, "name": owner.name}
        else:
            await coordinator.async_add_example(key, example)
        if answer["effect"] == EXAMPLE_ADDED and not use.open:
            # an appliance still waiting for its signature gets it now
            created = await coordinator.async_track_in_curve(key, example, use.cycling)
            if created:
                answer["signature"] = _signature_answer(created["signature"], bool(created.get("created")))

    elif target == "new":
        name = _name(msg)
        track = msg.get("track") or "curve"
        entity_id = msg.get("entity_id")
        if track != "curve" and not entity_id:
            raise ReviewError("invalid_format", f"{track}: expected entity_id")
        if track == "curve":
            created = None
            if not use.open and example["power_w"] >= 50:
                created = await _expect(coordinator, name, example, use.cycling)
            if created is not None:
                sig = created["signature"]
                example["signature_id"] = sig["id"]
                appliance = await coordinator.async_validate(sig["id"], name, examples=[example])
                fresh = bool(created.get("created"))
                answer["effect"] = APPLIANCE_CREATED if fresh else SIGNATURE_NAMED
                answer["signature"] = _signature_answer(sig, fresh)
            else:
                # the stop is not known yet: the signature follows (stop or night)
                appliance = coordinator.async_add_appliance(name, [], [example])
                await coordinator.async_push()
                answer["effect"] = APPLIANCE_CREATED
            key = appliance.key
        elif track == "teacher":
            coordinator.check_power_sensor(entity_id)
            coordinator.check_teacher_free(entity_id)
            teacher = Teacher(entity_id, dt_util.utcnow().timestamp())
            appliance = coordinator.async_add_appliance(name, [], [example], teacher)
            await coordinator.async_push()
            key, answer["effect"] = appliance.key, APPLIANCE_CREATED
        else:
            source = coordinator.entity_source(entity_id, msg.get("on_states"))
            if (track == "sensor") != (source.type == "submeter"):
                expected = "a power sensor" if track == "sensor" else "a switch"
                raise ReviewError("invalid_format", f"{entity_id}: expected {expected}")
            coordinator.check_free(source)
            appliance = coordinator.async_add_appliance(name, [source], [example])
            await coordinator.async_push()
            key, answer["effect"] = appliance.key, APPLIANCE_CREATED
    else:  # the schema allows nothing else
        raise ReviewError("invalid_format", f"target: expected one of {', '.join(TARGETS)}")

    appliance = coordinator.appliance_of_key(key) if key else None
    answer["appliance"] = _appliance_answer(appliance)
    if appliance is not None:
        stored = next((e for e in appliance.examples if abs(e["t_on"] - example["t_on"]) <= 1.0), None)
        answer["example"] = stored or example
        answer["open"] = (stored or example).get("t_off") is None
    return answer


def _with(appliance: Appliance, example: dict[str, Any]) -> Appliance:
    return replace(appliance, examples=add_example(appliance.examples, example))


async def _expect(
    coordinator: EnergyMapperCoordinator, name: str, example: dict[str, Any], cycling: bool
) -> dict[str, Any] | None:
    """The signature of a whole use (``POST /api/signatures``): an unnamed
    one at that power, else a new expected one."""
    kind, band = signature_group(example["t_on"], example["t_off"], cycling)
    try:
        return await coordinator.client.expect_signature(kind, band, example["power_w"], name)
    except EnergyMapperApiError as err:
        if err.code == "conflict":
            raise ReviewError("conflict", err.message) from err
        if err.code == "invalid_signature":
            return None
        raise
