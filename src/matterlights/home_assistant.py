from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from dataclasses import dataclass, field
import logging
import threading
import time

import requests

from matterlights.screen import RgbColor


LOGGER = logging.getLogger("matterlights.home_assistant")


@dataclass(frozen=True, slots=True)
class LightUpdate:
    entity_id: str
    color: RgbColor | None = None
    brightness: int | None = None
    color_temp_kelvin: int | None = None


@dataclass(frozen=True, slots=True)
class LightUpdateBatch:
    entity_ids: tuple[str, ...]
    color: RgbColor | None = None
    brightness: int | None = None
    color_temp_kelvin: int | None = None


@dataclass(slots=True)
class HomeAssistantClient:
    base_url: str
    token: str
    timeout_seconds: float = 3.0
    inter_light_delay_seconds: float = 0.4
    max_parallel_updates: int = 1
    _thread_local: threading.local = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._thread_local = threading.local()

    def _get_session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                }
            )
            self._thread_local.session = session
        return session

    def get_available_entity_ids(self, entity_ids: list[str]) -> set[str]:
        if not entity_ids:
            return set()

        try:
            with self._get_session().get(
                f"{self.base_url}/api/states",
                timeout=self.timeout_seconds,
            ) as response:
                response.raise_for_status()
                states = response.json()
        except requests.RequestException:
            LOGGER.exception("Failed to refresh Home Assistant light availability")
            return set(entity_ids)

        wanted_entity_ids = set(entity_ids)
        available_entity_ids: set[str] = set()
        for state in states:
            entity_id = state.get("entity_id")
            if entity_id not in wanted_entity_ids:
                continue
            if state.get("state") != "unavailable":
                available_entity_ids.add(entity_id)
        return available_entity_ids

    def set_lights(
        self,
        entity_ids: list[str],
        color: RgbColor,
        brightness: int,
        transition_seconds: float,
    ) -> list[str]:
        return self.apply_light_updates(
            [LightUpdate(entity_id=entity_id, color=color, brightness=brightness) for entity_id in entity_ids],
            transition_seconds,
        )

    def turn_off_lights(self, entity_ids: list[str], transition_seconds: float) -> list[str]:
        return self.apply_light_updates(
            [LightUpdate(entity_id=entity_id) for entity_id in entity_ids],
            transition_seconds,
        )

    def apply_light_updates(
        self,
        updates: list[LightUpdate],
        transition_seconds: float,
    ) -> list[str]:
        grouped_updates = _group_light_updates(updates)
        if len(grouped_updates) <= 1 or self.max_parallel_updates <= 1:
            failed_batches = self._apply_light_updates_sequential(grouped_updates, transition_seconds)
            return [entity_id for batch in failed_batches for entity_id in batch.entity_ids]

        failed_updates = self._apply_light_updates_parallel(
            grouped_updates,
            transition_seconds,
            min(len(grouped_updates), self.max_parallel_updates),
        )
        if not failed_updates:
            return []
        failed_batches = self._apply_light_updates_sequential(failed_updates, transition_seconds)
        return [entity_id for batch in failed_batches for entity_id in batch.entity_ids]


    def _apply_light_updates_sequential(
        self,
        updates: list[LightUpdateBatch],
        transition_seconds: float,
    ) -> list[LightUpdateBatch]:
        failed_updates: list[LightUpdateBatch] = []
        should_delay_before_next_request = False
        for update in updates:
            if should_delay_before_next_request and self.inter_light_delay_seconds > 0:
                time.sleep(self.inter_light_delay_seconds)

            if not self._apply_light_update(update, transition_seconds):
                failed_updates.append(update)
                should_delay_before_next_request = True
                continue

            should_delay_before_next_request = False

        return failed_updates


    def _apply_light_updates_parallel(
        self,
        updates: list[LightUpdateBatch],
        transition_seconds: float,
        worker_count: int,
    ) -> list[LightUpdateBatch]:
        failed_updates: list[LightUpdateBatch] = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_to_update = {
                executor.submit(self._apply_light_update, update, transition_seconds): update
                for update in updates
            }
            for future, update in future_to_update.items():
                if not future.result():
                    failed_updates.append(update)
        return failed_updates


    def _apply_light_update(self, update: LightUpdateBatch, transition_seconds: float) -> bool:
        if update.color is None and update.color_temp_kelvin is None:
            return self._post_light_service(update.entity_ids, "turn_off", transition_seconds, {})

        if update.color_temp_kelvin is not None:
            payload = {
                "color_temp_kelvin": update.color_temp_kelvin,
                "brightness": update.brightness if update.brightness is not None else 255,
            }
        else:
            payload = {
                "rgb_color": update.color.as_list(),
                "brightness": update.brightness if update.brightness is not None else update.color.max_channel(),
            }

        return self._post_light_service(update.entity_ids, "turn_on", transition_seconds, payload)


    def _post_light_service(
        self,
        entity_ids: tuple[str, ...],
        service: str,
        transition_seconds: float,
        payload: dict[str, object],
        *,
        log_errors: bool = True,
    ) -> bool:
        try:
            with self._get_session().post(
                f"{self.base_url}/api/services/light/{service}",
                json={
                    "entity_id": list(entity_ids),
                    "transition": transition_seconds,
                    **payload,
                },
                timeout=self.timeout_seconds,
            ) as response:
                response.raise_for_status()
            return True
        except requests.RequestException as exc:
            if log_errors:
                # Transient HA/Matter failures are expected and retried; a one-line
                # reason is enough — a full stack trace per bulb just floods the log.
                LOGGER.warning("Failed to %s %s: %s", service, ", ".join(entity_ids), _concise_request_error(exc))
            return False


def _concise_request_error(exc: requests.RequestException) -> str:
    response = getattr(exc, "response", None)
    if response is not None:
        return f"HTTP {response.status_code}"
    return type(exc).__name__


def _group_light_updates(updates: list[LightUpdate]) -> list[LightUpdateBatch]:
    grouped_entity_ids: dict[tuple[tuple[int, int, int] | None, int | None, int | None], list[str]] = defaultdict(list)
    for update in updates:
        grouped_entity_ids[_group_key(update)].append(update.entity_id)

    grouped_updates: list[LightUpdateBatch] = []
    for update in updates:
        entity_ids = grouped_entity_ids.pop(_group_key(update), None)
        if entity_ids is None:
            continue
        grouped_updates.append(
            LightUpdateBatch(
                entity_ids=tuple(entity_ids),
                color=update.color,
                brightness=update.brightness,
                color_temp_kelvin=update.color_temp_kelvin,
            )
        )
    return grouped_updates


def _group_key(update: LightUpdate) -> tuple[tuple[int, int, int] | None, int | None, int | None]:
    color_key = tuple(update.color.as_list()) if update.color is not None else None
    return (color_key, update.brightness, update.color_temp_kelvin)
