"""Load supermarket scraper output through bronze and silver Supabase tables.

The existing store scrapers stay the source of truth for extraction. This module
only standardizes their CSV output, preserves each original row in bronze, and
writes one consistent current product shape to silver.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests


STORE_IDS = {"ah", "jumbo", "dirk", "plus"}
BATCH_SIZE = 150


@dataclass(frozen=True)
class NormalizedProduct:
    external_product_id: str
    name: str
    brand: str | None
    category: str | None
    subcategory: str | None
    image_url: str | None
    product_url: str | None
    package_size_text: str | None
    unit_quantity: float | None
    unit_type: str | None
    current_price_cents: int
    unit_price_cents: int | None
    unit_price_unit: str | None
    is_available: bool
    promotion: dict[str, Any]
    attributes: dict[str, Any]
    scraped_at: str
    source_row_number: int
    raw_product: dict[str, Any]


class SupabaseApi:
    def __init__(self, url: str, service_key: str) -> None:
        self.url = url.rstrip("/")
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> requests.Response:
        response = requests.request(
            method,
            f"{self.url}/{path.lstrip('/')}",
            params=params,
            json=json_body,
            data=content,
            headers={**self.headers, **(headers or {})},
            timeout=90,
        )
        if not response.ok:
            detail = response.text[:1_000]
            raise RuntimeError(f"Supabase {method} {path} failed ({response.status_code}): {detail}")
        return response

    def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        *,
        on_conflict: str | None = None,
        resolution: str = "merge-duplicates",
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        params = {"on_conflict": on_conflict} if on_conflict else None
        response = self._request(
            "POST",
            f"rest/v1/{table}",
            params=params,
            json_body=rows,
            headers={"Content-Type": "application/json", "Prefer": f"return=representation,resolution={resolution}"},
        )
        return response.json()

    def update(self, table: str, values: dict[str, Any], filters: dict[str, str]) -> None:
        self._request(
            "PATCH",
            f"rest/v1/{table}",
            params=filters,
            json_body=values,
            headers={"Content-Type": "application/json", "Prefer": "return=minimal"},
        )

    def upload(self, storage_path: str, content: bytes, content_type: str) -> None:
        self._request(
            "POST",
            f"storage/v1/object/catalog-bronze/{quote(storage_path, safe='/')}",
            headers={"Content-Type": content_type, "x-upsert": "true"},
            content=content,
        )

    def fetch_all(self, table: str, select: str, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        page_size = 1_000
        while True:
            params = {"select": select, "limit": str(page_size), "offset": str(offset), **(filters or {})}
            response = self._request("GET", f"rest/v1/{table}", params=params)
            page = response.json()
            rows.extend(page)
            if len(page) < page_size:
                return rows
            offset += page_size


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return re.sub(r"\s+", " ", text)


def find_value(row: dict[str, Any], *names: str) -> str | None:
    normalized = {re.sub(r"[^a-z0-9]", "", key.lower()): value for key, value in row.items()}
    for name in names:
        value = normalized.get(re.sub(r"[^a-z0-9]", "", name.lower()))
        cleaned = clean_text(value)
        if cleaned:
            return cleaned
    return None


def parse_money_cents(value: Any) -> int | None:
    text = clean_text(value)
    if not text:
        return None
    match = re.search(r"-?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?", text)
    if not match:
        return None
    numeric = match.group(0)
    if "," in numeric and "." in numeric:
        decimal_separator = "," if numeric.rfind(",") > numeric.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        numeric = numeric.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif "," in numeric:
        numeric = numeric.replace(",", ".")
    elif "." in numeric:
        fractional = numeric.rsplit(".", 1)[1]
        numeric = numeric if len(fractional) <= 2 else numeric.replace(".", "")
    try:
        amount = Decimal(numeric)
    except InvalidOperation:
        return None
    if amount < 0:
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_number(value: Any) -> float | None:
    text = clean_text(value)
    if not text:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def parse_package(value: str | None) -> tuple[float | None, str | None]:
    if not value:
        return None, None
    multipack = re.search(r"(\d+)\s*x\s*(\d+(?:[.,]\d+)?)\s*(kg|g|mg|l|ml|cl|stuks?|stuk)", value.lower())
    if multipack:
        multiplier = float(multipack.group(1))
        quantity = multiplier * float(multipack.group(2).replace(",", "."))
        unit = multipack.group(3)
        aliases = {"stuk": "piece", "stuks": "piece"}
        return quantity, aliases.get(unit, unit)
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|g|mg|l|ml|cl|stuks?|stuk|pack|pak)", value.lower())
    if not match:
        return None, None
    quantity = float(match.group(1).replace(",", "."))
    unit = match.group(2)
    aliases = {"stuk": "piece", "stuks": "piece", "pak": "pack", "pack": "pack"}
    return quantity, aliases.get(unit, unit)


def parse_available(value: Any) -> bool:
    text = (clean_text(value) or "").lower()
    if not text:
        return True
    return text not in {"false", "0", "nee", "no", "outofstock", "unavailable", "niet beschikbaar"}


def maybe_json_text(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("[") or value.startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(parsed, list):
            return " > ".join(str(item) for item in parsed if clean_text(item)) or None
        if isinstance(parsed, dict):
            return " > ".join(str(item) for item in parsed.values() if clean_text(item)) or None
    return value


def iso_timestamp(value: str | None) -> str:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


def stable_external_id(row: dict[str, Any], name: str, product_url: str | None) -> str:
    direct = find_value(row, "webshopId", "product_id", "article_number", "sku", "twid", "id", "gtin", "ean")
    if direct:
        return direct
    digest = hashlib.sha256(f"{name}|{product_url or ''}".encode("utf-8")).hexdigest()[:24]
    return f"derived-{digest}"


def normalize_row(store_id: str, row: dict[str, Any], row_number: int) -> NormalizedProduct | None:
    name = find_value(row, "title", "product_name", "name", "regulated_name")
    price_raw = find_value(row, "currentPrice", "price", "current_price", "effective_price", "normal_price")
    price_cents = parse_money_cents(price_raw)
    if not name or price_cents is None:
        return None

    package_size = find_value(row, "salesUnitSize", "package", "packaging", "sales_unit", "package_size", "subtitle")
    unit_quantity = parse_number(find_value(row, "unit_quantity"))
    unit_type = find_value(row, "unit_type")
    if unit_quantity is None or unit_type is None:
        parsed_quantity, parsed_type = parse_package(package_size)
        unit_quantity = unit_quantity if unit_quantity is not None else parsed_quantity
        unit_type = unit_type or parsed_type

    category = maybe_json_text(find_value(row, "mainCategory", "department", "category", "categories", "root_categories_json"))
    subcategory = maybe_json_text(find_value(row, "subCategory", "webgroup", "subcategory", "subtitle", "category_paths_json"))
    image_url = find_value(row, "imageUrl800", "imageUrl", "primary_image_url", "image_url")
    product_url = find_value(row, "url", "product_url", "canonical_url")
    base_unit_price = parse_money_cents(find_value(row, "unit_price", "base_unit_price", "baseUnitPrice"))
    base_unit = find_value(row, "unit_price_unit", "base_unit_price_unit", "unitPriceDescription")
    promotion = {
        key: value
        for key, value in row.items()
        if value not in (None, "") and any(token in key.lower() for token in ("bonus", "promo", "offer", "discount"))
    }
    attributes = {
        key: value
        for key, value in row.items()
        if value not in (None, "")
        and key
        not in {
            "title", "product_name", "name", "price", "currentPrice", "current_price", "url", "product_url", "canonical_url"
        }
    }

    return NormalizedProduct(
        external_product_id=stable_external_id(row, name, product_url),
        name=name,
        brand=find_value(row, "brand"),
        category=category,
        subcategory=subcategory,
        image_url=image_url,
        product_url=product_url,
        package_size_text=package_size,
        unit_quantity=unit_quantity,
        unit_type=unit_type,
        current_price_cents=price_cents,
        unit_price_cents=base_unit_price,
        unit_price_unit=base_unit,
        is_available=parse_available(find_value(row, "is_available", "availability", "price_status")),
        promotion=promotion,
        attributes=attributes,
        scraped_at=iso_timestamp(find_value(row, "scraped_at")),
        source_row_number=row_number,
        raw_product=row,
    )


def read_normalized_csv(store_id: str, csv_path: Path, limit: int | None = None) -> tuple[list[NormalizedProduct], int]:
    products: list[NormalizedProduct] = []
    seen: set[str] = set()
    skipped = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            product = normalize_row(store_id, row, row_number)
            if not product:
                skipped += 1
                continue
            if product.external_product_id in seen:
                continue
            seen.add(product.external_product_id)
            products.append(product)
            if limit is not None and len(products) >= limit:
                break
    return products, skipped


def chunks(values: list[Any], size: int = BATCH_SIZE) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def env_or_fail(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def ingest(store_id: str, csv_path: Path, *, limit: int | None, dry_run: bool) -> None:
    if store_id not in STORE_IDS:
        raise SystemExit(f"Unsupported store {store_id}. Choose one of: {', '.join(sorted(STORE_IDS))}")
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    products, skipped = read_normalized_csv(store_id, csv_path, limit)
    print(f"Normalized {len(products)} {store_id.upper()} products; skipped {skipped} rows without a name or price.")
    if not products:
        raise SystemExit("Refusing to create an empty scrape run.")
    if dry_run:
        print(json.dumps(asdict(products[0]), ensure_ascii=False, indent=2))
        return

    api = SupabaseApi(env_or_fail("SUPABASE_URL"), env_or_fail("SUPABASE_SERVICE_ROLE_KEY"))
    run = api.insert("scrape_runs", [{"store_id": store_id, "source": "github_actions", "status": "running"}])[0]
    run_id = run["id"]
    artifact_bytes = csv_path.read_bytes()
    digest = hashlib.sha256(artifact_bytes).hexdigest()
    artifact_path = f"{store_id}/{run_id}/{csv_path.name}"

    try:
        api.upload(artifact_path, artifact_bytes, "text/csv")
        api.insert(
            "bronze_artifacts",
            [{
                "scrape_run_id": run_id,
                "storage_path": artifact_path,
                "content_type": "text/csv",
                "byte_size": len(artifact_bytes),
                "sha256": digest,
            }],
        )
        api.update("scrape_runs", {"artifact_path": artifact_path}, {"id": f"eq.{run_id}"})

        # Silver holds only the current store catalog. Bronze remains immutable per run.
        api.update("silver_products", {"is_current": False}, {"store_id": f"eq.{store_id}", "is_current": "eq.true"})
        inserted_silver = 0
        for group in chunks(products):
            bronze_rows = [
                {
                    "scrape_run_id": run_id,
                    "store_id": store_id,
                    "external_product_id": product.external_product_id,
                    "raw_product": product.raw_product,
                    "source_row_number": product.source_row_number,
                    "scraped_at": product.scraped_at,
                }
                for product in group
            ]
            bronze_response = api.insert("bronze_products", bronze_rows, on_conflict="scrape_run_id,external_product_id")
            bronze_ids = {row["external_product_id"]: row["id"] for row in bronze_response}
            silver_rows = []
            for product in group:
                bronze_id = bronze_ids.get(product.external_product_id)
                if not bronze_id:
                    raise RuntimeError(f"Bronze insert returned no ID for {product.external_product_id}")
                silver_rows.append(
                    {
                        "bronze_product_id": bronze_id,
                        "scrape_run_id": run_id,
                        "store_id": store_id,
                        "external_product_id": product.external_product_id,
                        "name": product.name,
                        "brand": product.brand,
                        "category": product.category,
                        "subcategory": product.subcategory,
                        "image_url": product.image_url,
                        "product_url": product.product_url,
                        "package_size_text": product.package_size_text,
                        "unit_quantity": product.unit_quantity,
                        "unit_type": product.unit_type,
                        "current_price_cents": product.current_price_cents,
                        "unit_price_cents": product.unit_price_cents,
                        "unit_price_unit": product.unit_price_unit,
                        "is_available": product.is_available,
                        "promotion": product.promotion,
                        "attributes": product.attributes,
                        "scraped_at": product.scraped_at,
                        "is_current": True,
                    }
                )
            api.insert("silver_products", silver_rows, on_conflict="scrape_run_id,external_product_id")
            inserted_silver += len(silver_rows)

        api.update(
            "scrape_runs",
            {
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "raw_row_count": len(products),
                "silver_row_count": inserted_silver,
            },
            {"id": f"eq.{run_id}"},
        )
        print(f"Completed run {run_id}: bronze={len(products)}, silver={inserted_silver}, artifact={artifact_path}")
    except Exception as error:
        api.update(
            "scrape_runs",
            {"status": "failed", "completed_at": datetime.now(timezone.utc).isoformat(), "error_message": str(error)[:2_000]},
            {"id": f"eq.{run_id}"},
        )
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize existing supermarket scraper output into Supabase bronze and silver tables.")
    parser.add_argument("command", choices=("ingest", "preview"))
    parser.add_argument("--store", required=True, choices=sorted(STORE_IDS))
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=None, help="Limit normalized products for a smoke run")
    parser.add_argument("--dry-run", action="store_true", help="Normalize and print a sample without contacting Supabase")
    args = parser.parse_args()
    ingest(args.store, args.csv, limit=args.limit, dry_run=args.dry_run or args.command == "preview")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
