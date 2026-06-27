"""Sync locally built public.products into hosted Supabase.

Environment:
    LOCAL_CATALOG_DB_URL  local Postgres DSN
    SUPABASE_DB_URL       hosted Supabase Postgres DSN

The hosted table must have public.products.external_id and a unique key on
(store_id, external_id). See the decouple migration before running this.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable

import psycopg

PRODUCT_COLUMNS = [
    "store_id",
    "external_id",
    "silver_product_id",
    "name",
    "brand",
    "category",
    "subcategory",
    "package_size_text",
    "current_price_cents",
    "unit_price_cents",
    "unit_price_unit",
    "is_available",
    "image_url",
    "product_url",
    "ean",
    "canonical_key",
    "canonical_name",
    "is_organic",
]

BATCH_SIZE = 1000


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing {name}.")
    return value


def hosted_dsn() -> str:
    value = env("SUPABASE_DB_URL")
    if "sslmode=" not in value:
        value += ("&" if "?" in value else "?") + "sslmode=require"
    return value


def local_dsn() -> str:
    return env("LOCAL_CATALOG_DB_URL")


def batches(rows: Iterable[tuple], size: int = BATCH_SIZE):
    batch: list[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def fetch_local_products(conn: psycopg.Connection, *, store: str | None, limit: int | None):
    where = ["external_id IS NOT NULL", "name IS NOT NULL"]
    params: list[object] = []
    if store:
        where.append("store_id = %s")
        params.append(store)

    sql = f"""
        SELECT {", ".join(PRODUCT_COLUMNS)}
        FROM public.products
        WHERE {" AND ".join(where)}
        ORDER BY store_id, external_id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        while True:
            rows = cur.fetchmany(BATCH_SIZE)
            if not rows:
                break
            for row in rows:
                yield row


def ensure_hosted_shape(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'products'
              AND column_name = 'external_id'
            """
        )
        if cur.fetchone() is None:
            raise SystemExit(
                "Hosted public.products.external_id is missing. "
                "Apply the decouple preparation migration first."
            )


def sync_products(*, store: str | None, limit: int | None, dry_run: bool, deactivate_missing: bool) -> None:
    with psycopg.connect(local_dsn(), prepare_threshold=None) as local_conn:
        rows_iter = fetch_local_products(local_conn, store=store, limit=limit)

        if dry_run:
            count = sum(1 for _ in rows_iter)
            print(f"[dry-run] would sync {count} products" + (f" for store={store}" if store else ""))
            return

        with psycopg.connect(hosted_dsn(), autocommit=False, prepare_threshold=None) as hosted_conn:
            ensure_hosted_shape(hosted_conn)
            upserted = 0

            with hosted_conn.cursor() as cur:
                cur.execute("CREATE TEMP TABLE sync_product_keys (store_id text, external_id text) ON COMMIT DROP")

            placeholders = ", ".join(["%s"] * len(PRODUCT_COLUMNS))
            update_cols = [c for c in PRODUCT_COLUMNS if c not in ("store_id", "external_id")]
            upsert_sql = f"""
                INSERT INTO public.products ({", ".join(PRODUCT_COLUMNS)}, synced_at, updated_at)
                VALUES ({placeholders}, now(), now())
                ON CONFLICT (store_id, external_id) DO UPDATE SET
                  {", ".join(f"{col} = EXCLUDED.{col}" for col in update_cols)},
                  synced_at = now(),
                  updated_at = now()
            """
            key_sql = "INSERT INTO sync_product_keys (store_id, external_id) VALUES (%s, %s)"

            for batch in batches(rows_iter):
                with hosted_conn.cursor() as cur:
                    cur.executemany(upsert_sql, batch)
                    cur.executemany(key_sql, [(row[0], row[1]) for row in batch])
                hosted_conn.commit()
                upserted += len(batch)
                print(f"synced {upserted} products", flush=True)

            deactivated = 0
            if deactivate_missing and limit is None:
                with hosted_conn.cursor() as cur:
                    if store:
                        cur.execute(
                            """
                            UPDATE public.products p
                               SET is_available = false,
                                   synced_at = now(),
                                   updated_at = now()
                             WHERE p.store_id = %s
                               AND NOT EXISTS (
                                 SELECT 1
                                 FROM sync_product_keys k
                                 WHERE k.store_id = p.store_id
                                   AND k.external_id = p.external_id
                               )
                            """,
                            (store,),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE public.products p
                               SET is_available = false,
                                   synced_at = now(),
                                   updated_at = now()
                             WHERE NOT EXISTS (
                               SELECT 1
                               FROM sync_product_keys k
                               WHERE k.store_id = p.store_id
                                 AND k.external_id = p.external_id
                             )
                            """
                        )
                    deactivated = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                hosted_conn.commit()

            print(f"done: upserted={upserted}, deactivated_missing={deactivated}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync local public.products to hosted Supabase.")
    parser.add_argument("--store", choices=("ah", "jumbo", "dirk", "plus", "spar", "aldi"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-deactivate-missing",
        action="store_true",
        help="Do not mark hosted rows absent from the local build as unavailable.",
    )
    args = parser.parse_args()

    try:
        sync_products(
            store=args.store,
            limit=args.limit,
            dry_run=args.dry_run,
            deactivate_missing=not args.no_deactivate_missing,
        )
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
