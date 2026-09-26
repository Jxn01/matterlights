from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from matterlights.config import load_settings, overlay_path, platform_name


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

            self.assertEqual(settings.control_state_file, base_dir / f".matterlights-control.{platform_name()}.json")
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

class PerOsConfigTests(unittest.TestCase):
    """One checkout can serve two OSes (a dual boot sharing one NTFS tree), so
    what differs per OS lives in .env.<os> and .matterlights-<kind>.<os>.json."""

    BASE = ["HA_TOKEN=test-token", "HA_LIGHT_ENTITIES=light.one"]

    def load(self, env_lines, overlays=None, environ=None, files=()):
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            (base / ".env").write_text("\n".join(self.BASE + list(env_lines)), encoding="utf-8")
            for os_name, lines in (overlays or {}).items():
                (base / f".env.{os_name}").write_text("\n".join(lines), encoding="utf-8")
            for name in files:
                (base / name).write_text("{}", encoding="utf-8")
            env = {"MATTERLIGHTS_ENV_FILE": str(base / ".env"), **(environ or {})}
            with patch.dict(os.environ, env, clear=False):
                for key in ("SYNC_INTERVAL_SECONDS", "SAMPLE_STRIDE", "LOG_PATH"):
                    if key not in env:
                        os.environ.pop(key, None)
                return load_settings(), base

    def test_platform_names(self) -> None:
        self.assertEqual(platform_name("linux"), "linux")
        self.assertEqual(platform_name("win32"), "windows")
        self.assertEqual(platform_name("darwin"), "darwin")

    def test_the_overlay_sits_beside_the_env_file(self) -> None:
        self.assertEqual(overlay_path(Path("/x/.env"), "win32"), Path("/x/.env.windows"))
        self.assertEqual(overlay_path(Path("/x/.env"), "linux"), Path("/x/.env.linux"))

    def test_this_oses_overlay_overrides_env(self) -> None:
        settings, _ = self.load(["SYNC_INTERVAL_SECONDS=0.2"], {platform_name(): ["SYNC_INTERVAL_SECONDS=0.05"]})
        self.assertEqual(settings.sync_interval_seconds, 0.05)

    def test_the_other_oses_overlay_is_ignored(self) -> None:
        other = "windows" if platform_name() == "linux" else "linux"
        settings, _ = self.load(["SAMPLE_STRIDE=25"], {other: ["SAMPLE_STRIDE=121"]})
        self.assertEqual(settings.sample_stride, 25)

    def test_the_environment_beats_the_overlay(self) -> None:
        settings, _ = self.load([], {platform_name(): ["SAMPLE_STRIDE=121"]}, environ={"SAMPLE_STRIDE": "7"})
        self.assertEqual(settings.sample_stride, 7)

    def test_an_empty_overlay_value_restores_the_default(self) -> None:
        """A shared .env may carry one OS's LOG_PATH; `LOG_PATH=` in the other's overlay undoes it."""

        from matterlights import paths

        settings, _ = self.load(["LOG_PATH=C:/Users/someone/matterlights.log"], {platform_name(): ["LOG_PATH="]})
        self.assertEqual(settings.log_path, paths.default_log_path())

    def test_state_files_default_to_this_oses_own_names(self) -> None:
        settings, base = self.load([])
        own = platform_name()
        self.assertEqual(settings.light_zone_file, base / f".matterlights-zones.{own}.json")
        self.assertEqual(settings.preview_override_file, base / f".matterlights-preview.{own}.json")
        self.assertEqual(settings.control_state_file, base / f".matterlights-control.{own}.json")

    def test_a_legacy_state_file_is_used_until_this_os_has_its_own(self) -> None:
        settings, base = self.load([], files=[".matterlights-zones.json"])
        self.assertEqual(settings.light_zone_file, base / ".matterlights-zones.json")

    def test_this_oses_state_file_wins_over_a_legacy_one(self) -> None:
        own = f".matterlights-zones.{platform_name()}.json"
        settings, base = self.load([], files=[".matterlights-zones.json", own])
        self.assertEqual(settings.light_zone_file, base / own)


@unittest.skipUnless(shutil.which("git"), "needs git")
class PerOsFilesAreIgnoredTests(unittest.TestCase):
    """The overlays hold HA_TOKEN-adjacent config and the state files hold local state."""

    REPO = Path(__file__).resolve().parents[1]

    def ignored(self, name: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(self.REPO), "check-ignore", "-q", "--no-index", name],
            capture_output=True, encoding="utf-8",
        )
        if result.returncode not in (0, 1):
            self.skipTest("not a git checkout")
        return result.returncode == 0

    def test_per_os_files_are_ignored(self) -> None:
        for name in (".env.linux", ".env.windows", ".matterlights-zones.linux.json",
                     ".matterlights-control.windows.json", ".matterlights-preview.json"):
            with self.subTest(name=name):
                self.assertTrue(self.ignored(name))

    def test_the_example_is_not_ignored(self) -> None:
        """Positive control: the check can say "not ignored"."""

        self.assertFalse(self.ignored(".env.example"))
