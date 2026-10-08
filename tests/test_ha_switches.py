"""The Lights/RGB switches mirrored to Home Assistant, both ways (ha_switches)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import requests

from matterlights import playback
from matterlights.config import load_settings
from matterlights.ha_switches import SwitchBinding, SwitchMirror, start_switch_mirror, switch_bindings
from matterlights.home_assistant import HomeAssistantClient
from matterlights.playback import (
    ControlState,
    load_control_state,
    save_control_state,
    update_control_switches,
)

LIGHTS = "input_boolean.lights"
RGB = "input_boolean.rgb"
QUIET = logging.getLogger("test-ha-switches")
QUIET.addHandler(logging.NullHandler())
QUIET.propagate = False


class FakeHomeAssistant:
    def __init__(self, **states: bool | None) -> None:
        self.states: dict[str, bool | None] = {LIGHTS: states.get("lights"), RGB: states.get("rgb")}
        self.pushes: list[tuple[str, bool]] = []
        self.accept_pushes = True

    def get_switch_state(self, entity_id: str) -> bool | None:
        return self.states.get(entity_id)

    def set_switch_state(self, entity_id: str, on: bool) -> bool:
        self.pushes.append((entity_id, on))
        if self.accept_pushes:
            self.states[entity_id] = on
        return self.accept_pushes


class MirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.path = Path(self._temp.name) / "control.json"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _mirror(self, ha: FakeHomeAssistant) -> SwitchMirror:
        bindings = [SwitchBinding("lights_on", LIGHTS), SwitchBinding("rgb_on", RGB)]
        return SwitchMirror(bindings, self.path, ha, QUIET)

    def _local(self) -> ControlState:
        return load_control_state(self.path)

    def test_first_reading_adopts_home_assistant(self) -> None:
        """The helper is shared by both OS halves; the control file is per OS."""

        save_control_state(self.path, ControlState(lights_on=True, rgb_on=True))
        ha = FakeHomeAssistant(lights=False, rgb=True)
        self._mirror(ha).tick()

        self.assertFalse(self._local().lights_on)
        self.assertTrue(self._local().rgb_on)
        self.assertEqual(ha.pushes, [], "first contact must not push the local value over Home Assistant's")

    def test_adopting_keeps_every_other_field(self) -> None:
        save_control_state(self.path, ControlState(mode=playback.MODE_CUSTOM, capture_target="2"))
        self._mirror(FakeHomeAssistant(lights=False, rgb=False)).tick()

        local = self._local()
        self.assertEqual((local.mode, local.capture_target), (playback.MODE_CUSTOM, "2"))

    def test_no_reading_is_not_off(self) -> None:
        """Unreachable, missing, unknown, unavailable: leave the switch alone."""

        save_control_state(self.path, ControlState(lights_on=True))
        ha = FakeHomeAssistant(lights=None, rgb=None)
        mirror = self._mirror(ha)
        mirror.tick()

        self.assertTrue(self._local().lights_on)
        self.assertEqual(ha.pushes, [])
        self.assertIsNone(mirror.agreed("lights_on"), "no reading must not count as first contact")

        ha.states[LIGHTS] = False
        mirror.tick()
        self.assertFalse(self._local().lights_on, "the first real reading still wins")

    def test_a_dashboard_click_is_pushed_to_home_assistant(self) -> None:
        save_control_state(self.path, ControlState(lights_on=True, rgb_on=True))
        ha = FakeHomeAssistant(lights=True, rgb=True)
        mirror = self._mirror(ha)
        mirror.tick()

        update_control_switches(self.path, {"rgb_on": False})
        mirror.tick()

        self.assertEqual(ha.pushes, [(RGB, False)])
        self.assertFalse(ha.states[RGB])

    def test_a_home_assistant_change_is_written_locally_and_not_echoed(self) -> None:
        save_control_state(self.path, ControlState(lights_on=True, rgb_on=True))
        ha = FakeHomeAssistant(lights=True, rgb=True)
        mirror = self._mirror(ha)
        mirror.tick()

        ha.states[LIGHTS] = False
        mirror.tick()
        mirror.tick()

        self.assertFalse(self._local().lights_on)
        self.assertEqual(ha.pushes, [], "a change that came from Home Assistant must not be sent back")

    def test_a_pushed_click_settles_without_further_traffic(self) -> None:
        save_control_state(self.path, ControlState(lights_on=True))
        ha = FakeHomeAssistant(lights=True, rgb=True)
        mirror = self._mirror(ha)
        mirror.tick()
        update_control_switches(self.path, {"lights_on": False})
        for _ in range(3):
            mirror.tick()

        self.assertEqual(ha.pushes, [(LIGHTS, False)])

    def test_a_failed_push_is_retried(self) -> None:
        save_control_state(self.path, ControlState(lights_on=True))
        ha = FakeHomeAssistant(lights=True, rgb=True)
        mirror = self._mirror(ha)
        mirror.tick()
        update_control_switches(self.path, {"lights_on": False})

        ha.accept_pushes = False
        mirror.tick()
        ha.accept_pushes = True
        mirror.tick()

        self.assertEqual(ha.pushes, [(LIGHTS, False), (LIGHTS, False)])
        self.assertFalse(ha.states[LIGHTS])
        self.assertFalse(self._local().lights_on, "a failed push must not revert the click")

    def test_each_switch_follows_whichever_side_moved_it_in_the_same_tick(self) -> None:
        save_control_state(self.path, ControlState(lights_on=True, rgb_on=True))
        ha = FakeHomeAssistant(lights=True, rgb=True)
        mirror = self._mirror(ha)
        mirror.tick()

        update_control_switches(self.path, {"rgb_on": False})  # clicked at the PC
        ha.states[LIGHTS] = False  # flipped in Home Assistant
        mirror.tick()

        self.assertEqual(ha.pushes, [(RGB, False)])
        self.assertFalse(self._local().lights_on)
        self.assertFalse(self._local().rgb_on)

    def test_both_sides_moving_to_the_same_value_needs_no_traffic(self) -> None:
        save_control_state(self.path, ControlState(lights_on=True))
        ha = FakeHomeAssistant(lights=True, rgb=True)
        mirror = self._mirror(ha)
        mirror.tick()

        update_control_switches(self.path, {"lights_on": False})
        ha.states[LIGHTS] = False
        mirror.tick()

        self.assertEqual(ha.pushes, [])
        self.assertIs(mirror.agreed("lights_on"), False)

    def test_an_unbound_switch_is_never_touched(self) -> None:
        save_control_state(self.path, ControlState(lights_on=True, rgb_on=True))
        ha = FakeHomeAssistant(lights=False, rgb=False)
        SwitchMirror([SwitchBinding("lights_on", LIGHTS)], self.path, ha, QUIET).tick()

        self.assertFalse(self._local().lights_on)
        self.assertTrue(self._local().rgb_on)

    def test_a_missing_control_file_is_created_on_first_contact(self) -> None:
        self._mirror(FakeHomeAssistant(lights=False, rgb=True)).tick()
        self.assertTrue(self.path.exists())
        self.assertFalse(self._local().lights_on)


class BindingTests(unittest.TestCase):
    def test_empty_entities_are_not_bound(self) -> None:
        self.assertEqual(switch_bindings("", ""), [])
        self.assertEqual(switch_bindings("", RGB), [SwitchBinding("rgb_on", RGB)])
        self.assertEqual(
            switch_bindings(LIGHTS, RGB),
            [SwitchBinding("lights_on", LIGHTS), SwitchBinding("rgb_on", RGB)],
        )

    def test_nothing_starts_unless_configured(self) -> None:
        settings = SimpleNamespace(
            ha_lights_switch_entity="", ha_rgb_switch_entity="",
            control_state_file=Path("x.json"), ha_switch_poll_seconds=1.0,
        )
        self.assertIsNone(start_switch_mirror(settings, FakeHomeAssistant(), QUIET))
        settings.ha_lights_switch_entity = LIGHTS
        settings.control_state_file = None
        self.assertIsNone(start_switch_mirror(settings, FakeHomeAssistant(), QUIET))


class ControlFileTests(unittest.TestCase):
    def test_update_switches_preserves_everything_else(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "control.json"
            save_control_state(path, ControlState(mode=playback.MODE_CUSTOM, capture_target="1"))
            update_control_switches(path, {"lights_on": False})

            state = load_control_state(path)
            self.assertEqual((state.mode, state.capture_target, state.lights_on, state.rgb_on),
                             (playback.MODE_CUSTOM, "1", False, True))

    def test_update_switches_rejects_anything_else(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                update_control_switches(Path(temp_dir) / "c.json", {"mode": "custom"})

    def test_save_leaves_no_temporary_files_behind(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "control.json"
            for _ in range(3):
                save_control_state(path, ControlState())
            self.assertEqual(sorted(p.name for p in Path(temp_dir).iterdir()), ["control.json"])

    def test_save_retries_while_windows_holds_the_file_open(self) -> None:
        real_replace = os.replace
        attempts = []

        def flaky_replace(source, target):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError("in use")
            real_replace(source, target)

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "control.json"
            with patch.object(playback.os, "replace", side_effect=flaky_replace), \
                    patch.object(playback.time, "sleep"):
                save_control_state(path, ControlState(lights_on=False))
            self.assertEqual(len(attempts), 3)
            self.assertFalse(load_control_state(path).lights_on)

    def test_a_failed_save_removes_its_temporary_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "control.json"
            with patch.object(playback.os, "replace", side_effect=PermissionError("in use")), \
                    patch.object(playback.time, "sleep"):
                with self.assertRaises(PermissionError):
                    save_control_state(path, ControlState())
            self.assertEqual(list(Path(temp_dir).iterdir()), [])


class _Response:
    def __init__(self, status: int, body: object = None) -> None:
        self.status_code = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def json(self):
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class ClientTests(unittest.TestCase):
    def _client_returning(self, **kwargs) -> tuple[HomeAssistantClient, MagicMock]:
        client = HomeAssistantClient("http://ha.invalid", "token", timeout_seconds=3.0)
        session = MagicMock(**kwargs)
        patcher = patch.object(HomeAssistantClient, "_get_session", return_value=session)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client, session

    def test_reads_on_and_off(self) -> None:
        for state, expected in (("on", True), ("off", False)):
            client, _ = self._client_returning(**{"get.return_value": _Response(200, {"state": state})})
            self.assertIs(client.get_switch_state(LIGHTS), expected)

    def test_no_reading_is_none_never_false(self) -> None:
        cases = [
            _Response(200, {"state": "unknown"}),
            _Response(200, {"state": "unavailable"}),
            _Response(404, {"message": "Entity not found."}),
            _Response(500, None),
            _Response(200, ["not", "a", "dict"]),
        ]
        for response in cases:
            client, _ = self._client_returning(**{"get.return_value": response})
            self.assertIsNone(client.get_switch_state(LIGHTS), response._body)
        client, _ = self._client_returning(**{"get.side_effect": requests.ConnectionError("down")})
        self.assertIsNone(client.get_switch_state(LIGHTS))

    def test_switching_uses_the_generic_service(self) -> None:
        client, session = self._client_returning(**{"post.return_value": _Response(200, [])})
        self.assertTrue(client.set_switch_state(LIGHTS, False))
        url = session.post.call_args.args[0]
        self.assertTrue(url.endswith("/api/services/homeassistant/turn_off"), url)
        self.assertEqual(session.post.call_args.kwargs["json"], {"entity_id": LIGHTS})

    def test_a_refused_switch_reports_failure(self) -> None:
        client, _ = self._client_returning(**{"post.return_value": _Response(401, None)})
        with self.assertLogs("matterlights.home_assistant", level="WARNING"):
            self.assertFalse(client.set_switch_state(LIGHTS, True))


class SettingsTests(unittest.TestCase):
    def _load(self, *lines: str):
        with TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("\n".join(["HA_TOKEN=t", "HA_LIGHT_ENTITIES=light.a", *lines]), encoding="utf-8")
            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                return load_settings()

    def test_off_by_default(self) -> None:
        settings = self._load()
        self.assertEqual((settings.ha_lights_switch_entity, settings.ha_rgb_switch_entity), ("", ""))
        self.assertEqual(settings.ha_switch_poll_seconds, 1.0)

    def test_reads_the_entities_and_interval(self) -> None:
        settings = self._load(f"HA_LIGHTS_SWITCH_ENTITY={LIGHTS}", f"HA_RGB_SWITCH_ENTITY={RGB}",
                              "HA_SWITCH_POLL_SECONDS=2.5")
        self.assertEqual((settings.ha_lights_switch_entity, settings.ha_rgb_switch_entity), (LIGHTS, RGB))
        self.assertEqual(settings.ha_switch_poll_seconds, 2.5)

    def test_rejects_a_bad_entity_or_interval(self) -> None:
        with self.assertRaises(ValueError):
            self._load("HA_LIGHTS_SWITCH_ENTITY=not_an_entity")
        with self.assertRaises(ValueError):
            self._load("HA_SWITCH_POLL_SECONDS=0")


if __name__ == "__main__":
    unittest.main()
