"""
Enrich an existing AH CSV with detail-only fields.

This does not scrape category pages again. It reads webshopId values from an
existing CSV, fetches the AH product detail endpoint, and writes a new CSV with
nutriscore, kenmerken, allergen buckets, and image-size URLs filled.
"""

import argparse
import asyncio
import csv
import time
from pathlib import Path

import httpx
import pandas as pd

import ah_scraper


ENRICH_COLUMNS = [
    "nutriscore",
    "kenmerken",
    "glutenvrij",
    "lactosevrij",
    "allergens_contains",
    "allergens_may_contain",
    "allergens_free_from",
    "imageUrl",
    "imageUrl800",
    "imageUrl400",
    "imageUrl200",
    "imageUrl48",
]


def default_output_path(input_csv: Path) -> Path:
    return input_csv.with_name(f"{input_csv.stem}_enriched{input_csv.suffix}")


def write_checkpoint(df: pd.DataFrame, output_csv: Path) -> None:
    df.to_csv(output_csv, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)


def merge_enrichment(df: pd.DataFrame, webshop_id: str, detail_row: dict) -> None:
    mask = df["webshopId"].astype(str) == str(webshop_id)
    for column in ENRICH_COLUMNS:
        if column not in df.columns:
            df[column] = None
        df[column] = df[column].astype("object")
        value = detail_row.get(column)
        if value not in (None, ""):
            df.loc[mask, column] = value


async def enrich_csv(
    input_csv: Path,
    output_csv: Path,
    concurrency: int,
    batch_size: int,
    limit: int | None = None,
) -> pd.DataFrame:
    df = pd.read_csv(input_csv, dtype={"webshopId": "string", "gtin": "string"})
    if "webshopId" not in df.columns:
        raise ValueError("Input CSV must contain a webshopId column")

    for column in ENRICH_COLUMNS:
        if column not in df.columns:
            df[column] = None

    ids = [
        str(webshop_id)
        for webshop_id in df["webshopId"].dropna().astype(str).drop_duplicates().tolist()
        if webshop_id.strip()
    ]
    if limit is not None:
        ids = ids[:limit]

    print(f"Loaded {len(df)} rows from {input_csv}")
    print(f"Enriching {len(ids)} unique webshopIds (concurrency {concurrency})")

    sem = asyncio.Semaphore(concurrency)
    enriched = 0
    failed = 0

    async with httpx.AsyncClient(http2=True) as client:
        print("Getting anonymous token...")
        token = await ah_scraper.get_anonymous_token(client)
        print(f"Token OK (len {len(token)})")

        async def fetch_one(webshop_id: str) -> tuple[str, dict | None]:
            data = await ah_scraper.fetch_product_detail(client, token, sem, webshop_id)
            if not data:
                return webshop_id, None
            return webshop_id, ah_scraper.normalize_detail_product(data)

        for start in range(0, len(ids), batch_size):
            batch = ids[start:start + batch_size]
            results = await asyncio.gather(
                *(fetch_one(webshop_id) for webshop_id in batch),
                return_exceptions=True,
            )

            for result in results:
                if isinstance(result, Exception):
                    failed += 1
                    print(f"  detail task failed: {result}")
                    continue
                webshop_id, detail_row = result
                if not detail_row:
                    failed += 1
                    continue
                merge_enrichment(df, webshop_id, detail_row)
                enriched += 1

            write_checkpoint(df, output_csv)
            print(
                f"  Enriched {min(start + len(batch), len(ids))}/{len(ids)} ids; "
                f"{enriched} ok, {failed} failed -> {output_csv}"
            )

    write_checkpoint(df, output_csv)
    return df


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich an existing AH CSV with detail-only nutriscore/kenmerken/allergen/image fields."
    )
    parser.add_argument("input_csv", help="Existing AH CSV with a webshopId column")
    parser.add_argument("--out", default=None, help="Output CSV path")
    parser.add_argument("--concurrency", type=int, default=2, help="Simultaneous detail requests")
    parser.add_argument("--batch-size", type=int, default=500, help="Rows to fetch before each checkpoint save")
    parser.add_argument("--limit", type=int, default=None, help="Limit unique webshopIds for a smoke test")
    args = parser.parse_args()

    input_csv = Path(args.input_csv)
    output_csv = Path(args.out) if args.out else default_output_path(input_csv)

    if input_csv.resolve() == output_csv.resolve():
        raise SystemExit("Refusing to overwrite the input CSV. Use a different --out path.")

    t0 = time.perf_counter()
    df = asyncio.run(
        enrich_csv(
            input_csv=input_csv,
            output_csv=output_csv,
            concurrency=args.concurrency,
            batch_size=args.batch_size,
            limit=args.limit,
        )
    )
    elapsed = time.perf_counter() - t0
    filled_nutri = df["nutriscore"].notna().sum() if "nutriscore" in df.columns else 0
    print(f"\nDone in {elapsed:.1f}s - wrote {len(df)} rows -> {output_csv.resolve()}")
    print(f"Rows with nutriscore: {filled_nutri}")


if __name__ == "__main__":
    main()
