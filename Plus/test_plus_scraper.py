import unittest
from unittest.mock import Mock, patch

import plus_scraper


class PlusVersionRefreshTests(unittest.TestCase):
    def test_refreshes_module_token_from_manifest(self) -> None:
        session = Mock()
        response = Mock()
        response.json.return_value = {"versionToken": "new-token"}
        session.get.return_value = response

        old_token = plus_scraper.MODULE_VERSION
        try:
            self.assertTrue(plus_scraper.refresh_module_version(session))
            self.assertEqual(plus_scraper.MODULE_VERSION, "new-token")
        finally:
            plus_scraper.MODULE_VERSION = old_token

    def test_retries_once_after_version_drift(self) -> None:
        session = Mock()
        first = Mock(status_code=200)
        first.json.return_value = {"versionInfo": {"hasModuleVersionChanged": True}}
        second = Mock(status_code=200)
        second.json.return_value = {"versionInfo": {}, "data": {"ok": True}}
        session.post.side_effect = [first, second]

        old_token = plus_scraper.MODULE_VERSION
        try:
            with patch.object(plus_scraper, "refresh_module_version", side_effect=lambda _: setattr(plus_scraper, "MODULE_VERSION", "new-token") or True):
                payload = {"versionInfo": {"moduleVersion": old_token}}
                result = plus_scraper.post(session, "https://example.test/action", payload, "https://example.test")
            self.assertEqual(result["data"], {"ok": True})
            self.assertEqual(payload["versionInfo"]["moduleVersion"], "new-token")
            self.assertEqual(session.post.call_count, 2)
        finally:
            plus_scraper.MODULE_VERSION = old_token


if __name__ == "__main__":
    unittest.main()
