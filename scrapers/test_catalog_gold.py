import json
import tempfile
import unittest
from pathlib import Path

from catalog_gold import parse_batch, prepare_batch


class CatalogGoldTests(unittest.TestCase):
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


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
