from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from matterlights.config import load_settings


class LoadSettingsTests(unittest.TestCase):
    def test_resolves_relative_paths_from_env_file_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            env_path = base_dir / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "HA_TOKEN=test-token",
                        "HA_LIGHT_ENTITIES=light.one,light.two",
                        "LIGHT_ZONE_LAYOUT=top-left,top-right",
                        "LIGHT_ZONE_FILE=config/zones.json",
                        "PREVIEW_OVERRIDE_FILE=config/preview.json",
                        "LOG_PATH=logs/matterlights.log",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                settings = load_settings()

            self.assertEqual(settings.light_zone_file, base_dir / "config" / "zones.json")
            self.assertEqual(settings.preview_override_file, base_dir / "config" / "preview.json")
            self.assertEqual(settings.log_path, base_dir / "logs" / "matterlights.log")

    def _settings_with(self, *lines: str):
        with TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(["HA_TOKEN=test-token", "HA_LIGHT_ENTITIES=light.one", *lines]),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                return load_settings(), Path(temp_dir)

    def test_rgb_publish_socket_accepts_udp_for_windows(self) -> None:
        from matterlights.rgb_target import UdpTarget

        settings, _ = self._settings_with("RGB_PUBLISH_SOCKET=udp://127.0.0.1:45731")
        self.assertEqual(settings.rgb_publish_socket, UdpTarget("127.0.0.1", 45731))

    def test_a_malformed_udp_target_is_refused_when_settings_load(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._settings_with("RGB_PUBLISH_SOCKET=udp://127.0.0.1")
        self.assertIn("udp://127.0.0.1:45731", str(caught.exception))

    def test_a_plain_rgb_socket_path_still_resolves_from_the_env_file(self) -> None:
        settings, base_dir = self._settings_with("RGB_PUBLISH_SOCKET=run/rgb.sock")
        self.assertEqual(settings.rgb_publish_socket, base_dir / "run" / "rgb.sock")

    def test_control_state_and_display_sleep_defaults(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            env_path = base_dir / ".env"
            env_path.write_text(
                "\n".join(["HA_TOKEN=test-token", "HA_LIGHT_ENTITIES=light.one"]),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                settings = load_settings()

            self.assertEqual(settings.control_state_file, base_dir / ".matterlights-control.json")
            self.assertTrue(settings.respect_display_sleep)

    def test_pattern_fades_are_capped_off_by_default(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(["HA_TOKEN=test-token", "HA_LIGHT_ENTITIES=light.one"]),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                settings = load_settings()
            self.assertEqual(settings.max_pattern_transition_seconds, 0.0)

    def test_rejects_negative_pattern_transition_cap(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "HA_TOKEN=test-token",
                        "HA_LIGHT_ENTITIES=light.one",
                        "MAX_PATTERN_TRANSITION_SECONDS=-1",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                with self.assertRaisesRegex(ValueError, "MAX_PATTERN_TRANSITION_SECONDS"):
                    load_settings()

    def test_respect_display_sleep_parses_false(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "HA_TOKEN=test-token",
                        "HA_LIGHT_ENTITIES=light.one",
                        "RESPECT_DISPLAY_SLEEP=false",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                settings = load_settings()

            self.assertFalse(settings.respect_display_sleep)

    def test_rejects_invalid_dashboard_port(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "HA_TOKEN=test-token",
                        "HA_LIGHT_ENTITIES=light.one",
                        "DASHBOARD_PORT=70000",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                with self.assertRaisesRegex(ValueError, "DASHBOARD_PORT"):
                    load_settings()

    def test_rejects_invalid_color_sync_mode(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "HA_TOKEN=test-token",
                        "HA_LIGHT_ENTITIES=light.one",
                        "COLOR_SYNC_MODE=fast-and-furious",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"MATTERLIGHTS_ENV_FILE": str(env_path)}, clear=False):
                with self.assertRaisesRegex(ValueError, "COLOR_SYNC_MODE"):
                    load_settings()