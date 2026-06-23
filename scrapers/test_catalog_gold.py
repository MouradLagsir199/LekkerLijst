import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_gold import parse_batch, post_openai_response, prepare_batch, valid_mappings


class CatalogGoldTests(unittest.TestCase):
    def test_uses_the_service_role_bridge_without_a_local_openai_key(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with patch("catalog_gold.bridge_request") as bridge_request:
                bridge_request.return_value.json.return_value = {"response": {"id": "resp-test"}}

                response = post_openai_response({"model": "gpt-5.4-mini", "input": "test"})

        self.assertEqual(response["id"], "resp-test")
        bridge_request.assert_called_once_with(
            "response",
            {"requestBody": {"model": "gpt-5.4-mini", "input": "test"}},
        )

    def test_prepares_and_parses_structured_batch_data(self) -> None:
        products = [
            {
                "id": "silver-1",
                "store_id": "ah",
                "external_product_id": "1",
                "name": "Zaanse Hoeve margarine",
                "brand": "Zaanse Hoeve",
                "category": "Zuivel",
                "subcategory": "Boter en margarine",
                "package_size_text": "250 g",
            }
        ]
        mapping = {
            "mappings": [
                {
                    "silverProductId": "silver-1",
                    "canonicalName": "boter",
                    "category": "Zuivel",
                    "aliases": ["margarine", "roomboter"],
                    "confidence": 0.91,
                }
            ]
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "silver.json"
            request_path = root / "requests.jsonl"
            manifest_path = root / "manifest.json"
            output_path = root / "results.jsonl"
            mappings_path = root / "mappings.json"
            input_path.write_text(json.dumps(products), encoding="utf-8")

            prepare_batch(input_path, request_path, manifest_path, "gpt-5.4-mini")
            request = json.loads(request_path.read_text(encoding="utf-8").strip())
            self.assertEqual(request["body"]["model"], "gpt-5.4-mini")
            self.assertIn("silver-1", request["body"]["input"][1]["content"])

            output = {
                "custom_id": request["custom_id"],
                "response": {"body": {"output": [{"content": [{"type": "output_text", "text": json.dumps(mapping)}]}]}},
            }
            output_path.write_text(json.dumps(output) + "\n", encoding="utf-8")
            parse_batch(output_path, mappings_path)
            self.assertEqual(read_json(mappings_path)[0]["canonicalName"], "boter")

    def test_prepares_a_non_overlapping_catalog_segment(self) -> None:
        products = [
            {"id": f"silver-{index}", "name": f"Product {index}", "brand": None, "category": None, "subcategory": None, "package_size_text": None}
            for index in range(161)
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "silver.json"
            request_path = root / "requests.jsonl"
            manifest_path = root / "manifest.json"
            input_path.write_text(json.dumps(products), encoding="utf-8")

            prepare_batch(input_path, request_path, manifest_path, "gpt-5.4-mini", segment=2, segments=2)

            requests = [json.loads(line) for line in request_path.read_text(encoding="utf-8").splitlines()]
            product_ids = {
                product["silverProductId"]
                for request in requests
                for product in json.loads(request["body"]["input"][1]["content"])
            }
            self.assertEqual(product_ids, {"silver-160"})
            self.assertTrue(all(request["custom_id"].startswith("catalog-segment-02-") for request in requests))

    def test_discards_duplicate_and_unknown_silver_mappings(self) -> None:
        valid = valid_mappings(
            [
                {"silverProductId": "silver-1", "canonicalName": "boter"},
                {"silverProductId": "silver-1", "canonicalName": "margarine"},
                {"silverProductId": "unknown", "canonicalName": "olie"},
                {"silverProductId": "silver-2", "canonicalName": ""},
            ],
            {"silver-1": {"id": "silver-1"}, "silver-2": {"id": "silver-2"}},
        )
        self.assertEqual(valid, [{"silverProductId": "silver-1", "canonicalName": "boter"}])


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
