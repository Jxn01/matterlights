from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from matterlights import dashboard


class ControlEndpointTests(unittest.TestCase):
    def _env(self, temp_dir: str) -> Path:
        env_path = Path(temp_dir) / ".env"
        env_path.write_text(
            "\n".join(
                [
                    "HA_URL=http://127.0.0.1:8123",
                    "HA_TOKEN=test-token",
                    "HA_LIGHT_ENTITIES=light.a,light.b",
                    f"CONTROL_STATE_FILE={temp_dir}/.matterlights-control.json",
                ]
            ),
            encoding="utf-8",
        )
        return env_path

    def test_post_valid_pattern_persists_and_get_reflects_it(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_path = self._env(temp_dir)
            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                client = dashboard.APP.test_client()
                body = {
                    "mode": "custom",
                    "custom": {
                        "type": "pattern",
                        "brightness": 180,
                        "solid": {"color": [10, 20, 30]},
                        "pattern": {
                            "steps": [
                                {"color": [255, 0, 0], "hold": 3, "transition": 0},
                                {"color": [0, 0, 255], "hold": 4, "transition": 1},
                                {"color": [255, 255, 0], "hold": 0, "transition": 2},
                            ]
                        },
                    },
                }
                post = client.post("/api/control", json=body)
                self.assertEqual(post.status_code, 200)
                self.assertEqual(post.get_json()["mode"], "custom")

                get = client.get("/api/control")
                payload = get.get_json()
                self.assertEqual(payload["mode"], "custom")
                self.assertEqual(payload["cycleSeconds"], 10.0)
                self.assertEqual(len(payload["custom"]["pattern"]["steps"]), 3)

    def test_screens_endpoint_lists_attached_monitors(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_path = self._env(temp_dir)
            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                payload = dashboard.APP.test_client().get("/api/screens").get_json()

        self.assertGreaterEqual(len(payload["monitors"]), 1)
        # Index 0 is mss's virtual bounding box; the rest are physical screens.
        self.assertEqual(payload["monitors"][0]["index"], 0)
        self.assertEqual(payload["screenCount"], len(payload["monitors"]) - 1)
        for key in ("index", "left", "top", "width", "height"):
            self.assertIn(key, payload["monitors"][0])

    def test_selecting_a_screen_persists_and_reports_effective_target(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_path = self._env(temp_dir)
            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                client = dashboard.APP.test_client()
                body = {"mode": "autonomous", "captureTarget": "1", "custom": {"type": "solid"}}
                self.assertEqual(client.post("/api/control", json=body).status_code, 200)

                payload = client.get("/api/control").get_json()
                self.assertEqual(payload["captureTarget"], "1")
                self.assertEqual(payload["effectiveCaptureTarget"], "1")

    def test_master_switch_persists_through_the_control_endpoint(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_path = self._env(temp_dir)
            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                client = dashboard.APP.test_client()
                body = {"mode": "autonomous", "lightsOn": False, "custom": {"type": "solid"}}
                self.assertEqual(client.post("/api/control", json=body).status_code, 200)
                payload = client.get("/api/control").get_json()
                self.assertFalse(payload["lightsOn"])

                body["lightsOn"] = True
                client.post("/api/control", json=body)
                self.assertTrue(client.get("/api/control").get_json()["lightsOn"])

    def test_selecting_a_missing_screen_returns_400(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_path = self._env(temp_dir)
            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                client = dashboard.APP.test_client()
                body = {"mode": "autonomous", "captureTarget": "99", "custom": {"type": "solid"}}
                response = client.post("/api/control", json=body)

        self.assertEqual(response.status_code, 400)
        self.assertIn("not attached", response.get_json()["message"])

    def test_post_invalid_payload_returns_400(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_path = self._env(temp_dir)
            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                client = dashboard.APP.test_client()
                response = client.post("/api/control", json={"mode": "fast-and-furious"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("mode", response.get_json()["message"])


class DashboardHelpersTests(unittest.TestCase):
    def test_ps_quote_escapes_single_quotes_for_powershell(self) -> None:
        # _ps_quote moved to service_control/windows.py with the rest of the
        # Task Scheduler code. It is pure string work, so it still imports here.
        from matterlights.service_control.windows import _ps_quote

        self.assertEqual(_ps_quote("MatterLights' Dashboard"), "MatterLights'' Dashboard")

    def test_api_errors_return_json_payload(self) -> None:
        with dashboard.APP.test_request_context("/api/status"):
            response, status_code = dashboard.handle_api_error(RuntimeError("boom"))

        self.assertEqual(status_code, 500)
        payload = json.loads(response.get_data(as_text=True))
        self.assertEqual(payload["message"], "boom")
        self.assertFalse(payload["ok"])

    def test_page_html_contains_key_runtime_surfaces(self) -> None:
        page = dashboard._page_html()
        self.assertIn("MatterLights Control", page)
        self.assertIn("Open Zone Designer", page)
        self.assertIn("Recent Log", page)

class RgbToggleRenderingTests(unittest.TestCase):
    """The RGB control appears only when the extension is configured.

    The requirement is "otherwise don't display it", so this checks the markup
    is genuinely absent rather than merely hidden -- a display:none button is
    still in the DOM, still focusable, and still a thing to wonder about on a
    machine that has no RGB extension at all.
    """

    def test_absent_when_the_extension_is_disabled(self) -> None:
        page = dashboard._page_html(False)
        self.assertNotIn('id="rgbToggle"', page)
        self.assertIn('id="powerToggle"', page, "the lights switch always renders")

    def test_present_when_the_extension_is_enabled(self) -> None:
        page = dashboard._page_html(True)
        self.assertIn('id="rgbToggle"', page)

    def test_no_placeholder_survives_either_way(self) -> None:
        """A missed substitution would ship the literal token to the browser."""

        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                page = dashboard._page_html(enabled)
                self.assertNotIn("__RGB_TOGGLE__", page)
                self.assertNotIn("__RGB_ENABLED__", page)
                self.assertNotIn("__TITLE__", page)

    def test_the_javascript_flag_matches_the_config(self) -> None:
        self.assertIn("RGB_ENABLED = false", dashboard._page_html(False))
        self.assertIn("RGB_ENABLED = true", dashboard._page_html(True))

    def test_the_toggle_posts_through_the_same_control_endpoint(self) -> None:
        """One control file for both switches; they cannot disagree."""

        page = dashboard._page_html(True)
        self.assertIn("rgbOn: control.rgbOn", page, "sent in the control body")
        self.assertIn("rgbOn: payload.rgbOn !== false", page, "read back from the server")

    def test_the_index_route_reads_the_setting(self) -> None:
        with patch.object(dashboard, "load_settings") as load:
            load.return_value.rgb_extension_enabled = True
            with patch.object(dashboard, "_page_html") as page:
                dashboard.index()
        page.assert_called_once_with(True)


class RgbControlStateTests(unittest.TestCase):
    def test_rgb_on_round_trips_through_the_control_file(self) -> None:
        from matterlights.playback import (
            ControlState,
            control_state_from_payload,
            control_state_to_payload,
        )

        payload = control_state_to_payload(ControlState())
        self.assertIn("rgbOn", payload)
        self.assertIs(control_state_from_payload({**payload, "rgbOn": False}).rgb_on, False)
        self.assertIs(control_state_from_payload({**payload, "rgbOn": True}).rgb_on, True)

    def test_a_control_file_written_before_this_field_defaults_on(self) -> None:
        """Otherwise an upgrade would silently leave the RGB dark forever."""

        from matterlights.playback import control_state_from_payload, control_state_to_payload, ControlState

        payload = control_state_to_payload(ControlState())
        del payload["rgbOn"]
        self.assertIs(control_state_from_payload(payload).rgb_on, True)
