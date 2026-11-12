"""Which matterlights may touch the bulbs, when more than one machine can.

The rig and the machine beside the TV each run a sync loop against the same six
bulbs, and two loops fighting over a bulb is a strobe. A selector in Home
Assistant names the owner. ``HA_OWNER_ENTITY`` is that selector and
``HA_OWNER_VALUE`` is the value that means "this instance". Anything else is
someone else's turn: the loop keeps running, stays off the bulbs, and does not
refresh the heartbeat, so Home Assistant's stale-heartbeat fallback keeps
watching whichever instance really has them.

Leaving a room dark is an act on the bulbs too, so the shutdown layers ask the
same question: a rig that does not own the lights does not switch them off when
it powers down.

An instance starts out not owning anything and holds its last answer when Home
Assistant cannot be read. A restart of Home Assistant must not start a second
driver, or stop the first.

Letting go is immediate and taking over waits for a second reading. Both
instances poll, so without that the new owner could act first and have its "off"
overtaken by the last colour of the old one, a lamp left lit with nobody to
notice (see ``light_worker``).

**Manual control** (``HA_MANUAL_ENTITY``, an on/off helper; the lights hub,
2026-11-12): while it is on, NO instance owns the bulbs -- whatever the selector
says -- so a colour set in Home Assistant stays. Every instance honours it
(``tv-hub``'s deploy copies the setting to the TV's), and Home Assistant's
stale-heartbeat automation is conditioned on it being off. A separate helper,
not a value of the selector: the selector also picks the soundbar's input.
An unreadable manual helper keeps its last answer, like the selector.
"""

from __future__ import annotations

import logging
from typing import Protocol

from matterlights.ha_switches import SwitchMirrorThread

LOGGER = logging.getLogger("matterlights.owner")


class OwnerClient(Protocol):
    def get_state(self, entity_id: str) -> str | None: ...


class OwnerGate:
    """``entity_id`` + ``value``: the selector and this instance's value (empty = always the owner by it).
    ``manual_entity``: the manual-control helper (empty = none)."""

    def __init__(
        self,
        entity_id: str,
        value: str,
        client: OwnerClient,
        logger: logging.Logger = LOGGER,
        manual_entity: str = "",
    ) -> None:
        self._entity_id = entity_id
        self._value = value
        self._manual_entity = manual_entity
        self._client = client
        self._logger = logger
        self._owned = False
        self._taking_over = False
        self._without_reading: set[str] = set()
        self._selector_ours = not entity_id      # the selector's answer, held through a missing reading
        self._selector_state: str | None = None
        self._manual = False                     # the manual helper's answer, held likewise

    def owned(self) -> bool:
        return self._owned

    def manual(self) -> bool:
        """Manual control is on (the last reading)."""
        return self._manual

    def holder(self) -> str | None:
        """The selector's last value -- who has the lights when this instance does not."""
        return self._selector_state

    def _read(self, entity_id: str) -> str | None:
        state = self._client.get_state(entity_id)
        if state is None:
            if entity_id not in self._without_reading:
                self._without_reading.add(entity_id)
                self._logger.warning("No reading from %s; keeping the last answer", entity_id)
        elif entity_id in self._without_reading:
            self._without_reading.discard(entity_id)
            self._logger.info("%s is readable again", entity_id)
        return state

    def tick(self) -> None:
        if self._entity_id:
            state = self._read(self._entity_id)
            if state is not None:
                self._selector_state = state
                self._selector_ours = state == self._value
        if self._manual_entity:
            state = self._read(self._manual_entity)
            if state is not None:
                manual = state == "on"
                if manual != self._manual:
                    self._manual = manual
                    self._logger.info("Manual control %s (%s)", "on" if manual else "off", self._manual_entity)
        owned = self._selector_ours and not self._manual
        if owned and not self._owned and not self._taking_over:
            self._taking_over = True
            return
        self._taking_over = False
        if owned != self._owned:
            self._owned = owned
            why = "manual control" if self._manual else f"{self._entity_id} is {self._selector_state}"
            self._logger.info("%s: the lights are %s", why, "ours" if owned else "not ours")


def start_owner_gate(settings, client: OwnerClient, logger: logging.Logger = LOGGER) -> tuple[OwnerGate, SwitchMirrorThread] | None:
    """Start watching the selector and the manual helper, or return None when neither is configured."""

    manual_entity = getattr(settings, "ha_manual_entity", "")
    if not settings.ha_owner_entity and not manual_entity:
        return None
    gate = OwnerGate(settings.ha_owner_entity, settings.ha_owner_value, client, logger, manual_entity)
    thread = SwitchMirrorThread(gate, settings.ha_switch_poll_seconds, logger)
    thread.start()
    return gate, thread


def owned_elsewhere(settings, client: OwnerClient) -> bool:
    """One reading for the shutdown path: the lights are someone else's -- another instance's, or the
    owner's own while manual control is on. No reading is not "someone else's"."""

    manual_entity = getattr(settings, "ha_manual_entity", "")
    if manual_entity and client.get_state(manual_entity) == "on":
        return True
    if not settings.ha_owner_entity:
        return False
    state = client.get_state(settings.ha_owner_entity)
    return state is not None and state != settings.ha_owner_value
