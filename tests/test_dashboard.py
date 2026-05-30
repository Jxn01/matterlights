from __future__ import annotations

import json
import unittest

from matterlights import dashboard


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