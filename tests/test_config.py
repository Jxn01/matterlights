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