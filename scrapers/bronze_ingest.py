"""Load a scraper's JSONL artifact into ``catalog.bronze_products``.

The ``catalog`` schema is deliberately NOT exposed through PostgREST, so this
writes over a direct Postgres connection (the professional path for a data
pipeline). Set the connection string in the environment:

    SUPABASE_DB_URL=postgresql://postgres:<pwd>@db.<ref>.supabase.co:5432/postgres
    # or the pooler URI from Supabase -> Project Settings -> Database

Usage:
    python -m scrapers.bronze_ingest --store dirk --input Output/dirk_bronze.jsonl
    python -m scrapers.bronze_ingest --store dirk --input Output/dirk_bronze.jsonl --dry-run

Each bronze row is content-addressed (see common.compute_row_hash), so loading
the same catalog twice inserts nothing new (ON CONFLICT (row_hash) DO NOTHING).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from .common import compute_row_hash, env, now_iso, read_jsonl

STORES = ("ah", "jumbo", "dirk", "plus", "spar", "aldi")
BATCH_SIZE = 500

INSERT_SQL = """
INSERT INTO catalog.bronze_products (scrape_run_id, store, raw_data, row_hash, scraped_at)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (row_hash) DO NOTHING
"""


def connection_string() -> str:
    url = env("SUPABASE_DB_URL")
    if not url:
        raise SystemExit(
            "Missing SUPABASE_DB_URL. Set it to the Supabase Postgres connection "
            "string (Project Settings -> Database -> Connection string)."
        )
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def scraped_at_of(record: dict, fallback: str) -> str:
    value = record.get("scraped_at")
    return value if isinstance(value, str) and value else fallback


def ingest(store: str, input_path: Path, *, limit: int | None, dry_run: bool) -> None:
    if store not in STORES:
        raise SystemExit(f"Unsupported store {store!r}. Choose one of: {', '.join(STORES)}")
    if not input_path.exists():
        raise SystemExit(f"Input JSONL not found: {input_path}")

    run_started = now_iso()
    records = list(read_jsonl(input_path))
    if limit is not None:
        records = records[:limit]
    if not records:
        raise SystemExit("Refusing to open a scrape run for an empty input file.")

    print(f"Read {len(records)} raw {store.upper()} products from {input_path}")

    if dry_run:
        sample = records[0]
        digest = compute_row_hash(store, sample)
        print(f"[dry-run] would open a scrape_run for store={store}")
        print(f"[dry-run] first row_hash={digest}")
        print(f"[dry-run] first record keys: {sorted(sample.keys())}")
        return

    # Supabase poolers can be sensitive to server-side prepared statements,
    # especially if someone uses the transaction pooler URL by accident.
    with psycopg.connect(connection_string(), autocommit=False, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO catalog.scrape_runs (store, status) VALUES (%s, 'running') RETURNING id",
                (store,),
            )
            run_id = cur.fetchone()[0]
        conn.commit()
        print(f"Opened scrape_run {run_id} (store={store})")

        inserted_total = 0
        seen_in_file: set[str] = set()
        try:
            batch: list[tuple] = []
            for record in records:
                row_hash = compute_row_hash(store, record)
                if row_hash in seen_in_file:
                    continue  # exact duplicate within this artifact
                seen_in_file.add(row_hash)
                batch.append(
                    (run_id, store, Jsonb(record), row_hash, scraped_at_of(record, run_started))
                )
                if len(batch) >= BATCH_SIZE:
                    inserted_total += _flush(conn, batch)
                    batch.clear()
            if batch:
                inserted_total += _flush(conn, batch)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE catalog.scrape_runs
                       SET status = 'completed', row_count = %s, completed_at = now()
                     WHERE id = %s
                    """,
                    (len(seen_in_file), run_id),
                )
            conn.commit()
            deduped = len(seen_in_file) - inserted_total
            print(
                f"Completed scrape_run {run_id}: "
                f"{len(seen_in_file)} unique scraped, {inserted_total} new bronze rows, "
                f"{deduped} already present (content-dedup)."
            )
        except Exception as error:  # noqa: BLE001 - record failure then re-raise
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE catalog.scrape_runs
                       SET status = 'failed', error_message = %s, completed_at = now()
                     WHERE id = %s
                    """,
                    (str(error)[:2000], run_id),
                )
            conn.commit()
            raise


def _flush(conn: "psycopg.Connection", batch: list[tuple]) -> int:
    """Insert one batch, return the number of rows actually inserted (post-dedup)."""
    with conn.cursor() as cur:
        cur.executemany(INSERT_SQL, batch)
        # rowcount after executemany reflects total affected across the batch.
        affected = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.commit()
    return affected


def main() -> None:
    parser = argparse.ArgumentParser(description="Load scraper JSONL into catalog.bronze_products.")
    parser.add_argument("--store", required=True, choices=STORES)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=None, help="Only load the first N records")
    parser.add_argument("--dry-run", action="store_true", help="Parse + hash without touching the DB")
    args = parser.parse_args()
    ingest(args.store, args.input, limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
