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

    def test_listing_product_uses_regular_price_when_promotion_price_is_zero(self) -> None:
        product = plus_scraper.parse_listing_product(
            {
                "SKU": "565917",
                "EAN": "8712345678901",
                "Slug": "plus-asperges-565917",
                "Name": "PLUS Asperges wit",
                "Brand": "PLUS",
                "Product_Subtitle": "Per 500 g",
                "NewPrice": "0.0",
                "OriginalPrice": "5.99",
                "ImageURL": "https://example.test/asparagus.png",
                "IsAvailable": True,
                "Categories": {"List": [{"Name": "Groente"}]},
            }
        )

        self.assertEqual(product["product_id"], "565917")
        self.assertEqual(product["gtin"], "8712345678901")
        self.assertEqual(product["price"], "5.99")
        self.assertEqual(product["categories"], "Groente")


if __name__ == "__main__":
    unittest.main()
