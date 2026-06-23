import unittest

from catalog_match_eval import candidate_matches_expected, select_lowest_priced_relevant


class CatalogMatchEvaluationTests(unittest.TestCase):
    def test_keeps_the_lowest_price_inside_the_relevance_band(self) -> None:
        selected = select_lowest_priced_relevant(
            [
                {"product_name": "AH Kipfilet", "current_price_cents": 349, "match_score": 1.05},
                {"product_name": "Dirk Kipfilet", "current_price_cents": 299, "match_score": 0.96},
                {"product_name": "Kippenbouillon", "current_price_cents": 89, "match_score": 0.42},
            ]
        )
        self.assertEqual(selected["product_name"], "Dirk Kipfilet")

    def test_checks_the_selected_candidate_against_culinary_terms(self) -> None:
        self.assertTrue(candidate_matches_expected({"product_name": "Zaanse Hoeve margarine"}, ["boter", "margarine"]))
        self.assertFalse(candidate_matches_expected({"product_name": "Chocolademelk"}, ["boter", "margarine"]))


if __name__ == "__main__":
    unittest.main()
