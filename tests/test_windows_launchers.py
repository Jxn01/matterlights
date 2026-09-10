"""The Windows launchers must never open a console window.

On Windows 11 the default console is Windows Terminal, and Windows Terminal
ignores `-WindowStyle Hidden`: it draws the window itself, while every
process-level check reports no window at all. So `powershell -WindowStyle
Hidden -File ...` -- as a scheduled task, or through `Start-Process` -- put a
terminal on the taskbar. On the author's machine the live logon tasks were
switched to `pythonw.exe` by hand on 2026-07-19 while `install-autostart.ps1`
kept the old form, so re-running it would have silently brought the window
back. `pythonw` is a GUI-subsystem binary that never allocates a console, so
there is nothing left to hide.

These read the scripts as text, so they run everywhere. Comments are stripped
first: the scripts explain this very trap, quoting the old form, and only code
counts. With a PowerShell to hand -- `pwsh` on PATH, or `MATTERLIGHTS_PWSH`
pointing at one -- every script is parsed by it too.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PWSH = os.environ.get("MATTERLIGHTS_PWSH") or shutil.which("pwsh")

# On a command line, or split across an argument array: "-WindowStyle", "Hidden".
HIDDEN_WINDOW = re.compile(r"-WindowStyle[\s\"',]*Hidden", re.IGNORECASE)
POWERSHELL_HOST = re.compile(r"Start-Process\s+(-FilePath\s+)?[\"']?(powershell|pwsh)\b", re.IGNORECASE)
BLOCK_COMMENT = re.compile(r"<#.*?#>", re.DOTALL)
LINE_COMMENT = re.compile(r"(^|\s)#.*$", re.MULTILINE)


def _code(text: str) -> str:
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", text))


def _scripts() -> list[Path]:
    return sorted(SCRIPTS.glob("*.ps1"))


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


class NoConsoleWindowTest(unittest.TestCase):
    def test_no_script_hides_a_window_with_window_style(self) -> None:
        for path in _scripts():
            with self.subTest(script=path.name):
                self.assertIsNone(
                    HIDDEN_WINDOW.search(_code(path.read_text(encoding="utf-8"))),
                    "Windows Terminal ignores -WindowStyle Hidden; launch pythonw.exe instead",
                )

    def test_no_script_starts_a_powershell_host(self) -> None:
        for path in _scripts():
            with self.subTest(script=path.name):
                self.assertIsNone(POWERSHELL_HOST.search(_code(path.read_text(encoding="utf-8"))))

    def test_the_patterns_catch_the_forms_that_showed_a_window(self) -> None:
        """Taken from the scripts as they were; a guard that matches nothing passes forever."""

        self.assertTrue(HIDDEN_WINDOW.search('"-NoProfile -WindowStyle Hidden -File `"$syncScript`" -Foreground"'))
        self.assertTrue(HIDDEN_WINDOW.search('$launchArgs = @(\n    "-NoProfile",\n    "-WindowStyle",\n    "Hidden",\n)'))
        self.assertTrue(POWERSHELL_HOST.search('Start-Process -FilePath "powershell" -ArgumentList $launchArgs'))
        self.assertIsNone(POWERSHELL_HOST.search("Start-Process $url"))

    def test_only_code_counts(self) -> None:
        self.assertIsNone(HIDDEN_WINDOW.search(_code("# the old form, powershell -WindowStyle Hidden, showed one")))
        self.assertIsNone(HIDDEN_WINDOW.search(_code("<#\n  powershell -WindowStyle Hidden\n#>")))
        self.assertTrue(HIDDEN_WINDOW.search(_code('$flags = "-WindowStyle Hidden"  # still code')))

    def test_the_logon_tasks_run_pythonw_directly(self) -> None:
        text = _read("install-autostart.ps1")
        self.assertIn('".venv\\Scripts\\pythonw.exe"', text)
        self.assertRegex(text, r'-Execute \$pythonw -Argument "-m matterlights" ')
        self.assertRegex(text, r'-Execute \$pythonw -Argument "-m matterlights\.dashboard" ')

    def test_the_logon_tasks_have_no_time_limit(self) -> None:
        # Task Scheduler's default stops a task 72 hours after its trigger started it.
        self.assertRegex(_read("install-autostart.ps1"), r'\$settings\.ExecutionTimeLimit = "PT0S"')

    def test_a_refused_registration_stops_the_script(self) -> None:
        calls = re.findall(r"Register-ScheduledTask\b.*?\| Out-Null", _code(_read("install-autostart.ps1")), re.DOTALL)
        self.assertEqual(len(calls), 2)
        for call in calls:
            with self.subTest(call=call.split("\n")[1].strip()):
                self.assertIn("-ErrorAction Stop", call)

    def test_the_background_starts_use_pythonw(self) -> None:
        for name in ("start-sync.ps1", "start-dashboard.ps1", "start-zone-ui.ps1"):
            with self.subTest(script=name):
                self.assertRegex(_read(name), r"Start-Process -FilePath \$pythonwExe ")

    def test_every_script_is_ascii(self) -> None:
        """Windows PowerShell 5.1 reads a script without a BOM in the ANSI code page."""

        for path in _scripts():
            with self.subTest(script=path.name):
                path.read_bytes().decode("ascii")


@unittest.skipUnless(PWSH, "no PowerShell; set MATTERLIGHTS_PWSH to a pwsh binary")
class ParseTest(unittest.TestCase):
    def test_every_script_parses(self) -> None:
        for path in _scripts():
            with self.subTest(script=path.name):
                quoted = str(path).replace("'", "''")
                result = subprocess.run(
                    [
                        PWSH, "-NoProfile", "-NonInteractive", "-Command",
                        "$errors = $null; "
                        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{quoted}', [ref]$null, [ref]$errors); "
                        "$errors | ForEach-Object { $_.Message }",
                    ],
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
