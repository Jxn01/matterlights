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
"""

from __future__ import annotations

import logging
from typing import Protocol

from matterlights.ha_switches import SwitchMirrorThread

LOGGER = logging.getLogger("matterlights.owner")


class OwnerClient(Protocol):
    def get_state(self, entity_id: str) -> str | None: ...


class OwnerGate:
    def __init__(self, entity_id: str, value: str, client: OwnerClient, logger: logging.Logger = LOGGER) -> None:
        self._entity_id = entity_id
        self._value = value
        self._client = client
        self._logger = logger
        self._owned = False
        self._without_reading = False

    def owned(self) -> bool:
        return self._owned

    def tick(self) -> None:
        state = self._client.get_state(self._entity_id)
        if state is None:
            if not self._without_reading:
                self._without_reading = True
                self._logger.warning("No reading from %s; keeping the last answer", self._entity_id)
            return
        if self._without_reading:
            self._without_reading = False
            self._logger.info("%s is readable again", self._entity_id)
        owned = state == self._value
        if owned != self._owned:
            self._owned = owned
            self._logger.info("%s is %s: the lights are %s", self._entity_id, state, "ours" if owned else "not ours")


def start_owner_gate(settings, client: OwnerClient, logger: logging.Logger = LOGGER) -> tuple[OwnerGate, SwitchMirrorThread] | None:
    """Start watching the selector, or return None when no owner is configured."""

    if not settings.ha_owner_entity:
        return None
    gate = OwnerGate(settings.ha_owner_entity, settings.ha_owner_value, client, logger)
    thread = SwitchMirrorThread(gate, settings.ha_switch_poll_seconds, logger)
    thread.start()
    return gate, thread


def owned_elsewhere(settings, client: OwnerClient) -> bool:
    """One reading for the shutdown path. No reading is not "someone else's"."""

    if not settings.ha_owner_entity:
        return False
    state = client.get_state(settings.ha_owner_entity)
    return state is not None and state != settings.ha_owner_value
