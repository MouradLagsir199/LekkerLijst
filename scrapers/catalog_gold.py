"""One-time AI-assisted promotion from silver supermarket data to gold products.

This utility uses the OpenAI Batch API so a full supermarket catalog can be
categorized cheaply. It never sends recipe or user data: only public product
names, brands, categories, and package descriptions are included.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import requests

from catalog_pipeline import SupabaseApi, env_or_fail


OPENAI_URL = "https://api.openai.com/v1"
GROUP_SIZE = 80
GOLD_WRITE_BATCH_SIZE = 250

MAPPING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mappings"],
    "properties": {
        "mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["silverProductId", "canonicalName", "category", "aliases", "confidence"],
                "properties": {
                    "silverProductId": {"type": "string"},
                    "canonicalName": {"type": "string", "minLength": 1, "maxLength": 80},
                    "category": {"type": "string", "minLength": 1, "maxLength": 80},
                    "aliases": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 80}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        }
    },
}


def chunks(values: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def export_current_silver(output: Path, limit: int | None) -> None:
    api = SupabaseApi(env_or_fail("SUPABASE_URL"), env_or_fail("SUPABASE_SERVICE_ROLE_KEY"))
    rows = api.fetch_all(
        "silver_products",
        "id,store_id,external_product_id,name,brand,category,subcategory,image_url,product_url,package_size_text,unit_quantity,unit_type,current_price_cents,unit_price_cents,unit_price_unit,is_available,scraped_at",
        {"is_current": "eq.true"},
    )
    rows = [row for row in rows if row.get("is_available")]
    if limit is not None:
        rows = rows[:limit]
    write_json(output, rows)
    print(f"Exported {len(rows)} current silver products to {output}")


def system_prompt() -> str:
    return (
        "You map Dutch supermarket products to concise canonical Dutch recipe ingredients. "
        "Return only the requested JSON. Preserve silverProductId exactly. "
        "Use lower-case Dutch canonical names that users naturally write in recipes. "
        "Choose the broad recipe grouping when appropriate: Zaanse Hoeve margarine maps to boter, "
        "Duo Penotti maps to chocoladepasta, and a branded kipfilet maps to kipfilet. "
        "Do not return a brand as canonicalName. Use category for a practical Dutch food category. "
        "Aliases should be useful Dutch product/ingredient terms, not marketing copy."
    )


def make_request_body(group: list[dict[str, Any]], model: str) -> dict[str, Any]:
    compact = [
        {
            "silverProductId": row["id"],
            "name": row["name"],
            "brand": row.get("brand"),
            "category": row.get("category"),
            "subcategory": row.get("subcategory"),
            "package": row.get("package_size_text"),
        }
        for row in group
    ]
    return {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": json.dumps(compact, ensure_ascii=False)},
        ],
        "text": {"format": {"type": "json_schema", "name": "gold_product_mappings", "strict": True, "schema": MAPPING_SCHEMA}},
    }


def prepare_batch(
    input_path: Path,
    requests_path: Path,
    manifest_path: Path,
    model: str,
    segment: int = 1,
    segments: int = 1,
) -> None:
    rows = read_json(input_path)
    if not isinstance(rows, list) or not rows:
        raise SystemExit("Input must be a non-empty exported silver JSON array.")
    if not 1 <= segment <= segments:
        raise SystemExit("segment must be between 1 and segments.")

    all_groups = list(chunks(rows, GROUP_SIZE))
    groups_per_segment = (len(all_groups) + segments - 1) // segments
    first_group = (segment - 1) * groups_per_segment
    selected_groups = all_groups[first_group : first_group + groups_per_segment]
    if not selected_groups:
        raise SystemExit(f"Segment {segment} contains no catalog products.")

    manifest: dict[str, list[str]] = {}
    requests_path.parent.mkdir(parents=True, exist_ok=True)
    with requests_path.open("w", encoding="utf-8") as handle:
        for group_index, group in enumerate(selected_groups, start=first_group + 1):
            custom_id = f"catalog-segment-{segment:02d}-group-{group_index:05d}"
            manifest[custom_id] = [row["id"] for row in group]
            request = {"custom_id": custom_id, "method": "POST", "url": "/v1/responses", "body": make_request_body(group, model)}
            handle.write(json.dumps(request, ensure_ascii=False) + "\n")
    write_json(manifest_path, manifest)
    print(
        f"Prepared segment {segment}/{segments}: {len(manifest)} Batch API requests "
        f"for {sum(len(group) for group in selected_groups)} of {len(rows)} silver products."
    )


def openai_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {env_or_fail('OPENAI_API_KEY')}"}


def uses_openai_bridge() -> bool:
    return not bool(os.environ.get("OPENAI_API_KEY"))


def openai_bridge_url() -> str:
    return f"{env_or_fail('SUPABASE_URL').rstrip('/')}/functions/v1/catalog-openai-bridge"


def bridge_headers() -> dict[str, str]:
    service_role_key = env_or_fail("SUPABASE_SERVICE_ROLE_KEY")
    return {
        "Authorization": f"Bearer {service_role_key}",
        "apikey": service_role_key,
        "Content-Type": "application/json",
    }


def bridge_request(action: str, payload: dict[str, Any], timeout: int = 120) -> requests.Response:
    response = requests.post(
        openai_bridge_url(),
        headers=bridge_headers(),
        json={"action": action, **payload},
        timeout=timeout,
    )
    response.raise_for_status()
    return response


def post_openai_response(request_body: dict[str, Any]) -> dict[str, Any]:
    if uses_openai_bridge():
        return bridge_request("response", {"requestBody": request_body}).json()["response"]
    response = requests.post(
        f"{OPENAI_URL}/responses",
        headers={**openai_headers(), "Content-Type": "application/json"},
        json=request_body,
        timeout=120,
    )
    response.raise_for_status()
    return response.json()


def submit_batch(requests_path: Path, metadata_path: Path) -> None:
    if uses_openai_bridge():
        bridge_response = bridge_request(
            "submit",
            {"inputJsonl": requests_path.read_text(encoding="utf-8"), "filename": requests_path.name},
            timeout=180,
        ).json()
        batch = bridge_response["batch"]
        input_file_id = bridge_response["inputFileId"]
        write_json(metadata_path, {"batchId": batch["id"], "inputFileId": input_file_id, "submittedAt": int(time.time())})
        print(f"Submitted OpenAI batch {batch['id']} through the Supabase bridge.")
        return
    with requests_path.open("rb") as handle:
        file_response = requests.post(
            f"{OPENAI_URL}/files",
            headers=openai_headers(),
            data={"purpose": "batch"},
            files={"file": (requests_path.name, handle, "application/jsonl")},
            timeout=120,
        )
    file_response.raise_for_status()
    input_file_id = file_response.json()["id"]
    batch_response = requests.post(
        f"{OPENAI_URL}/batches",
        headers={**openai_headers(), "Content-Type": "application/json"},
        json={"input_file_id": input_file_id, "endpoint": "/v1/responses", "completion_window": "24h"},
        timeout=120,
    )
    batch_response.raise_for_status()
    batch = batch_response.json()
    write_json(metadata_path, {"batchId": batch["id"], "inputFileId": input_file_id, "submittedAt": int(time.time())})
    print(f"Submitted OpenAI batch {batch['id']}. Store {metadata_path} for status/download commands.")


def resolve_batch_id(batch_id: str | None, metadata_path: Path | None) -> str:
    if batch_id:
        return batch_id
    if metadata_path:
        return str(read_json(metadata_path)["batchId"])
    raise SystemExit("Provide --batch-id or --metadata.")


def get_batch(batch_id: str) -> dict[str, Any]:
    if uses_openai_bridge():
        return bridge_request("status", {"batchId": batch_id}).json()["batch"]
    response = requests.get(f"{OPENAI_URL}/batches/{batch_id}", headers=openai_headers(), timeout=60)
    response.raise_for_status()
    return response.json()


def batch_status(batch_id: str | None, metadata_path: Path | None) -> None:
    resolved_batch_id = resolve_batch_id(batch_id, metadata_path)
    batch = get_batch(resolved_batch_id)
    if metadata_path:
        metadata = read_json(metadata_path)
        write_json(metadata_path, {**metadata, "lastStatus": batch})
    print(json.dumps({"id": batch["id"], "status": batch["status"], "outputFileId": batch.get("output_file_id")}, indent=2))


def download_batch(batch_id: str | None, metadata_path: Path | None, output_path: Path) -> None:
    resolved_batch_id = resolve_batch_id(batch_id, metadata_path)
    batch = get_batch(resolved_batch_id)
    output_file_id = batch.get("output_file_id")
    if not output_file_id:
        raise SystemExit("Batch is not complete or status has not been refreshed. Run status first.")
    if uses_openai_bridge():
        response = bridge_request("download", {"batchId": resolved_batch_id}, timeout=180)
    else:
        response = requests.get(f"{OPENAI_URL}/files/{output_file_id}/content", headers=openai_headers(), timeout=120)
    response.raise_for_status()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(response.content)
    print(f"Downloaded Batch API output to {output_path}")


def output_text(response_body: dict[str, Any]) -> str | None:
    for output in response_body.get("output", []):
        for content in output.get("content", []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return content["text"]
    return None


def parse_batch(batch_output_path: Path, mappings_path: Path) -> None:
    mappings: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for line in batch_output_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        body = record.get("response", {}).get("body")
        if not isinstance(body, dict):
            failures.append(record)
            continue
        text = output_text(body)
        if not text:
            failures.append(record)
            continue
        try:
            parsed = json.loads(text)
            mappings.extend(parsed.get("mappings", []))
        except json.JSONDecodeError:
            failures.append(record)
    write_json(mappings_path, mappings)
    if failures:
        write_json(mappings_path.with_name(f"{mappings_path.stem}_failures.json"), failures)
    print(f"Parsed {len(mappings)} mappings; {len(failures)} Batch responses need inspection.")


def normalized_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def current_silver_rows() -> dict[str, dict[str, Any]]:
    api = SupabaseApi(env_or_fail("SUPABASE_URL"), env_or_fail("SUPABASE_SERVICE_ROLE_KEY"))
    rows = api.fetch_all(
        "silver_products",
        "id,store_id,external_product_id,name,brand,category,subcategory,image_url,product_url,package_size_text,unit_quantity,unit_type,current_price_cents,unit_price_cents,unit_price_unit,is_available,scraped_at",
        {"is_current": "eq.true"},
    )
    return {row["id"]: row for row in rows}


def valid_mappings(mappings: list[dict[str, Any]], source_rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one AI mapping per silver product before upserting the gold layer."""
    unique: dict[str, dict[str, Any]] = {}
    for mapping in mappings:
        silver_product_id = mapping.get("silverProductId")
        if (
            isinstance(silver_product_id, str)
            and silver_product_id in source_rows
            and isinstance(mapping.get("canonicalName"), str)
            and mapping["canonicalName"].strip()
        ):
            unique.setdefault(silver_product_id, mapping)
    return list(unique.values())


def apply_gold(input_path: Path | None, mappings_path: Path) -> None:
    source_rows = {row["id"]: row for row in read_json(input_path)} if input_path else current_silver_rows()
    mappings = read_json(mappings_path)
    if not isinstance(mappings, list):
        raise SystemExit("Mappings must be a JSON array created by parse-batch.")
    valid = valid_mappings(mappings, source_rows)
    if not valid:
        raise SystemExit("No valid mappings match the exported silver input.")
    duplicate_count = len(mappings) - len(valid)
    if duplicate_count:
        print(f"Ignored {duplicate_count} duplicate or invalid AI mappings before promotion.")

    api = SupabaseApi(env_or_fail("SUPABASE_URL"), env_or_fail("SUPABASE_SERVICE_ROLE_KEY"))
    canonical_rows: dict[str, dict[str, Any]] = {}
    for mapping in valid:
        canonical_name = normalized_name(mapping["canonicalName"])
        canonical_rows[canonical_name] = {"canonical_name": canonical_name, "category": mapping.get("category") or None}
    canonical_ids: dict[str, str] = {}
    for group in chunks(list(canonical_rows.values()), GOLD_WRITE_BATCH_SIZE):
        canonical_response = api.insert("canonical_ingredients", group, on_conflict="canonical_name")
        canonical_ids.update({row["canonical_name"]: row["id"] for row in canonical_response})

    gold_rows: list[dict[str, Any]] = []
    aliases_by_canonical: defaultdict[str, set[str]] = defaultdict(set)
    mapping_meta: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for mapping in valid:
        source = source_rows[mapping["silverProductId"]]
        canonical_name = normalized_name(mapping["canonicalName"])
        canonical_id = canonical_ids.get(canonical_name)
        if not canonical_id:
            raise RuntimeError(f"Canonical upsert returned no id for {canonical_name}")
        gold_rows.append(
            {
                "store_id": source["store_id"],
                "external_product_id": source["external_product_id"],
                "name": source["name"],
                "brand": source.get("brand"),
                "category": source.get("category"),
                "subcategory": source.get("subcategory"),
                "image_url": source.get("image_url"),
                "product_url": source.get("product_url"),
                "package_size_text": source.get("package_size_text"),
                "unit_quantity": source.get("unit_quantity"),
                "unit_type": source.get("unit_type"),
                "current_price_cents": source["current_price_cents"],
                "unit_price_cents": source.get("unit_price_cents"),
                "unit_price_unit": source.get("unit_price_unit"),
                "is_available": source.get("is_available", True),
                "last_seen_at": source.get("scraped_at"),
                "canonical_ingredient_id": canonical_id,
                "silver_product_id": source["id"],
            }
        )
        aliases_by_canonical[canonical_id].update(normalized_name(alias) for alias in mapping.get("aliases", []) if alias.strip())
        aliases_by_canonical[canonical_id].add(canonical_name)
        mapping_meta.append((mapping, source, canonical_id))

    product_ids: dict[tuple[str, str], str] = {}
    for group in chunks(gold_rows, GOLD_WRITE_BATCH_SIZE):
        product_response = api.insert("products", group, on_conflict="store_id,external_product_id")
        product_ids.update({(row["store_id"], row["external_product_id"]): row["id"] for row in product_response})
    product_mappings = []
    for mapping, source, canonical_id in mapping_meta:
        product_id = product_ids.get((source["store_id"], source["external_product_id"]))
        if not product_id:
            raise RuntimeError(f"Gold upsert returned no product id for {source['external_product_id']}")
        confidence = float(mapping.get("confidence", 0))
        product_mappings.append(
            {
                "product_id": product_id,
                "canonical_ingredient_id": canonical_id,
                "confidence": confidence,
                "mapping_source": "ai",
                "review_status": "approved" if confidence >= 0.85 else "pending",
            }
        )
    for group in chunks(product_mappings, GOLD_WRITE_BATCH_SIZE):
        api.insert("product_canonical_ingredients", group, on_conflict="product_id")

    alias_rows = [
        {"canonical_ingredient_id": canonical_id, "alias": alias, "source": "ai", "confidence": 0.9}
        for canonical_id, aliases in aliases_by_canonical.items()
        for alias in aliases
    ]
    for group in chunks(alias_rows, GOLD_WRITE_BATCH_SIZE):
        api.insert("ingredient_aliases", group, on_conflict="alias", resolution="ignore-duplicates")
    print(f"Promoted {len(gold_rows)} products into gold with {len(canonical_ids)} canonical ingredients.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare, run, and apply a one-time OpenAI Batch catalog categorization.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--out", type=Path, required=True)
    export_parser.add_argument("--limit", type=int, default=None)

    prepare_parser = subparsers.add_parser("prepare-batch")
    prepare_parser.add_argument("--input", type=Path, required=True)
    prepare_parser.add_argument("--requests", type=Path, required=True)
    prepare_parser.add_argument("--manifest", type=Path, required=True)
    prepare_parser.add_argument("--model", default=os.environ.get("OPENAI_CATALOG_MODEL", "gpt-5.4-mini"))
    prepare_parser.add_argument("--segment", type=int, default=1)
    prepare_parser.add_argument("--segments", type=int, default=1)

    submit_parser = subparsers.add_parser("submit-batch")
    submit_parser.add_argument("--requests", type=Path, required=True)
    submit_parser.add_argument("--metadata", type=Path, required=True)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--batch-id")
    status_parser.add_argument("--metadata", type=Path)

    download_parser = subparsers.add_parser("download-batch")
    download_parser.add_argument("--batch-id")
    download_parser.add_argument("--metadata", type=Path)
    download_parser.add_argument("--out", type=Path, required=True)

    parse_parser = subparsers.add_parser("parse-batch")
    parse_parser.add_argument("--input", type=Path, required=True)
    parse_parser.add_argument("--out", type=Path, required=True)

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--silver", type=Path)
    apply_parser.add_argument("--from-current", action="store_true")
    apply_parser.add_argument("--mappings", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "export":
        export_current_silver(args.out, args.limit)
    elif args.command == "prepare-batch":
        prepare_batch(args.input, args.requests, args.manifest, args.model, args.segment, args.segments)
    elif args.command == "submit-batch":
        submit_batch(args.requests, args.metadata)
    elif args.command == "status":
        batch_status(args.batch_id, args.metadata)
    elif args.command == "download-batch":
        download_batch(args.batch_id, args.metadata, args.out)
    elif args.command == "parse-batch":
        parse_batch(args.input, args.out)
    elif args.command == "apply":
        if not args.silver and not args.from_current:
            raise SystemExit("Provide --silver or --from-current.")
        apply_gold(args.silver, args.mappings)


if __name__ == "__main__":
    main()
