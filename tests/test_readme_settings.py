"""Every setting config.py reads is documented in the README's settings table.

The table used to list "the most important settings" -- 23 of the 36 the code
read. Documentation that is illustrative rather than exhaustive is how a
setting ends up known only to whoever wrote it.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
READ = re.compile(r'get_value\(\s*"([A-Z][A-Z0-9_]+)"')
ENVIRONMENT_ONLY = ("MATTERLIGHTS_ENV_FILE",)


def settings_read_by_the_code() -> set[str]:
    source = (REPO / "src" / "matterlights" / "config.py").read_text(encoding="utf-8")
    return set(READ.findall(source)) | set(ENVIRONMENT_ONLY)


def settings_documented() -> set[str]:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index("### Every setting"):]
    return set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", section, re.MULTILINE))


class ReadmeSettingsTest(unittest.TestCase):
    def test_every_setting_the_code_reads_is_documented(self) -> None:
        missing = settings_read_by_the_code() - settings_documented()
        self.assertEqual(missing, set(), f"add these to the README's settings table: {sorted(missing)}")

    def test_the_table_documents_nothing_the_code_no_longer_reads(self) -> None:
        stale = settings_documented() - settings_read_by_the_code()
        self.assertEqual(stale, set(), f"the code no longer reads: {sorted(stale)}")

    def test_the_scan_sees_the_settings_at_all(self) -> None:
        """Positive control: a broken regex would make both tests above vacuous."""

        read = settings_read_by_the_code()
        self.assertGreaterEqual(len(read), 30)
        self.assertIn("HA_TOKEN", read)
        self.assertIn("SAMPLE_STRIDE", settings_documented())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
