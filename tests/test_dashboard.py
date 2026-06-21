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
        self.assertEqual(dashboard._ps_quote("MatterLights' Dashboard"), "MatterLights'' Dashboard")

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