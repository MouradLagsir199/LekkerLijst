-- Prepare hosted Supabase for local-catalog product sync.
--
-- This migration is intentionally the safe half of the decouple:
--   * add/backfill public.products.external_id
--   * add a stable sync key on (store_id, external_id)
--   * remove the runtime FK from public.products to catalog.silver_products
--
-- Do NOT drop catalog here. Drop catalog only after:
--   1. local catalog build has been verified,
--   2. scrapers/sync_products.py has synced public.products from local to hosted,
--   3. app smoke tests pass against hosted.

ALTER TABLE public.products
  ADD COLUMN IF NOT EXISTS external_id text;

UPDATE public.products p
   SET external_id = sp.external_id
  FROM catalog.silver_products sp
 WHERE p.external_id IS NULL
   AND p.silver_product_id = sp.id;

UPDATE public.products
   SET external_id = COALESCE(external_id, silver_product_id::text, id::text)
 WHERE external_id IS NULL;

ALTER TABLE public.products
  ALTER COLUMN external_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS products_store_external_id_idx
  ON public.products (store_id, external_id);

ALTER TABLE public.products
  DROP CONSTRAINT IF EXISTS products_silver_product_id_fkey;

-- Final manual cutover, after local sync verification:
--
--   DROP FUNCTION IF EXISTS catalog.promote_to_public(text);
--   DROP FUNCTION IF EXISTS catalog.refresh_silver_products(text);
--   DROP FUNCTION IF EXISTS catalog.refresh_canonical_baseline();
--   DROP FUNCTION IF EXISTS catalog.apply_canonical_keys();
--   DROP FUNCTION IF EXISTS catalog.silver_qty_base(text);
--   DROP FUNCTION IF EXISTS catalog.silver_norm_unit(text);
--   DROP FUNCTION IF EXISTS catalog.silver_price_from_cents(text);
--   DROP FUNCTION IF EXISTS catalog.silver_to_numeric(text);
--   DROP FUNCTION IF EXISTS catalog.silver_clean_text(text);
--   DROP SCHEMA catalog CASCADE;
