"""Seed a fresh local catalog DB from hosted public.products.

Hosted Supabase no longer keeps the private catalog schema. The app-facing
public.products table still contains the canonical grouping columns, though, so
fresh local builds can recover prior AI/manual grouping decisions from there.

Environment:
    LOCAL_CATALOG_DB_URL  local Postgres DSN
    SUPABASE_DB_URL       hosted Supabase Postgres DSN
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from scrapers.sync_products import PRODUCT_COLUMNS, batches, hosted_dsn, local_dsn

BATCH_SIZE = 1000


def seed_canonical(local_conn: psycopg.Connection, hosted_conn: psycopg.Connection) -> int:
    sql = """
        SELECT DISTINCT ON (name_search)
          name_search,
          canonical_key,
          canonical_name,
          is_organic
        FROM public.products
        WHERE coalesce(name_search, '') <> ''
          AND coalesce(canonical_key, '') <> ''
        ORDER BY name_search, updated_at DESC NULLS LAST
    """
    rows = hosted_conn.execute(sql).fetchall()
    if not rows:
        return 0

    upsert = """
        INSERT INTO catalog.name_canonical
          (name_search, canonical_key, display_name, is_organic, confidence, source, model, tagged_at)
        VALUES (%s, %s, %s, %s, 0.9, 'ai_batch', 'hosted_public_products_seed', now())
        ON CONFLICT (name_search) DO UPDATE SET
          canonical_key = EXCLUDED.canonical_key,
          display_name  = EXCLUDED.display_name,
          is_organic    = EXCLUDED.is_organic,
          confidence    = GREATEST(COALESCE(catalog.name_canonical.confidence, 0), EXCLUDED.confidence),
          source        = EXCLUDED.source,
          model         = EXCLUDED.model,
          tagged_at     = now()
        WHERE catalog.name_canonical.source IS DISTINCT FROM 'manual'
    """
    seeded = 0
    with local_conn.cursor() as cur:
        for batch in batches(rows, BATCH_SIZE):
            cur.executemany(upsert, batch)
            seeded += len(batch)
    local_conn.commit()
    return seeded


def seed_products(local_conn: psycopg.Connection, hosted_conn: psycopg.Connection) -> int:
    cols = ", ".join(PRODUCT_COLUMNS)
    select_sql = f"""
        SELECT {cols}
        FROM public.products
        WHERE external_id IS NOT NULL
          AND name IS NOT NULL
        ORDER BY store_id, external_id
    """
    placeholders = ", ".join(["%s"] * len(PRODUCT_COLUMNS))
    update_cols = [c for c in PRODUCT_COLUMNS if c not in ("store_id", "external_id")]
    upsert_sql = f"""
        INSERT INTO public.products ({cols}, synced_at, updated_at)
        VALUES ({placeholders}, now(), now())
        ON CONFLICT (store_id, external_id) DO UPDATE SET
          {", ".join(f"{col} = EXCLUDED.{col}" for col in update_cols)},
          synced_at = now(),
          updated_at = now()
    """

    copied = 0
    with hosted_conn.cursor() as hcur:
        hcur.execute(select_sql)
        while True:
            rows = hcur.fetchmany(BATCH_SIZE)
            if not rows:
                break
            with local_conn.cursor() as lcur:
                lcur.executemany(upsert_sql, rows)
            copied += len(rows)
            local_conn.commit()
            print(f"seeded {copied} hosted products", flush=True)
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed local catalog tables from hosted public.products.")
    parser.add_argument(
        "--products",
        action="store_true",
        help="Also copy hosted public.products into local public.products. "
             "Use for canonical maintenance jobs, not fresh scrape rebuilds.",
    )
    args = parser.parse_args()

    try:
        with psycopg.connect(local_dsn(), prepare_threshold=None) as local_conn:
            with psycopg.connect(hosted_dsn(), prepare_threshold=None) as hosted_conn:
                canonical = seed_canonical(local_conn, hosted_conn)
                print(f"seeded {canonical} canonical mappings")
                if args.products:
                    products = seed_products(local_conn, hosted_conn)
                    print(f"seeded {products} hosted products")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
