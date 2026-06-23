import unittest

from catalog_pipeline import normalize_row, parse_money_cents


class CatalogPipelineTests(unittest.TestCase):
    def test_normalizes_ah_shape(self) -> None:
        product = normalize_row(
            "ah",
            {
                "webshopId": "123",
                "title": "Zaanse Hoeve Halfvolle melk",
                "brand": "Zaanse Hoeve",
                "currentPrice": "1,19",
                "salesUnitSize": "1 l",
                "mainCategory": "Zuivel",
                "subCategory": "Melk",
                "imageUrl": "https://example.test/melk.jpg",
                "url": "https://example.test/melk",
                "is_available": "true",
            },
            2,
        )

        assert product is not None
        self.assertEqual(product.external_product_id, "123")
        self.assertEqual(product.current_price_cents, 119)
        self.assertEqual(product.unit_quantity, 1)
        self.assertEqual(product.unit_type, "l")

    def test_normalizes_dirk_and_multipack_shapes(self) -> None:
        product = normalize_row(
            "dirk",
            {
                "product_id": "456",
                "product_name": "Voorbeeld melk multipack",
                "effective_price": "5.39",
                "packaging": "3 x 1 l",
                "department": "Zuivel",
                "webgroup": "Melk",
            },
            3,
        )

        assert product is not None
        self.assertEqual(product.unit_quantity, 3)
        self.assertEqual(product.unit_type, "l")
        self.assertEqual(product.category, "Zuivel")
        self.assertEqual(product.subcategory, "Melk")

    def test_parses_common_price_formats(self) -> None:
        self.assertEqual(parse_money_cents("€ 2,49"), 249)
        self.assertEqual(parse_money_cents("1.20"), 120)
        self.assertEqual(parse_money_cents("€ 1.299,95"), 129995)
        self.assertIsNone(parse_money_cents("onbekend"))

    def test_keeps_ah_product_when_current_price_is_missing(self) -> None:
        product = normalize_row(
            "ah",
            {
                "webshopId": "169813",
                "title": "AH Bosui",
                "priceBeforeBonus": "0.99",
                "salesUnitSize": "per bosje",
                "mainCategory": "Groente, aardappelen",
                "subCategory": "Ui",
            },
            2,
        )

        assert product is not None
        self.assertEqual(product.external_product_id, "169813")
        self.assertEqual(product.current_price_cents, 99)


if __name__ == "__main__":
    unittest.main()
