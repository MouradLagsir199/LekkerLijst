-- Promote catalog.silver_products into public.products.
--
-- This is intentionally idempotent: it can be run after every scrape/silver
-- refresh without clobbering catalog grouping decisions made later.

CREATE UNIQUE INDEX IF NOT EXISTS products_silver_product_id_idx
  ON public.products (silver_product_id)
  WHERE silver_product_id IS NOT NULL;

CREATE OR REPLACE FUNCTION catalog.promote_to_public(store_filter text DEFAULT NULL)
RETURNS TABLE(store_id text, rows_upserted integer)
LANGUAGE plpgsql
AS $$
BEGIN
  RETURN QUERY
  WITH upserted AS (
    INSERT INTO public.products (
      store_id,
      silver_product_id,
      name,
      current_price_cents,
      unit_price_cents,
      unit_price_unit,
      is_available,
      image_url,
      synced_at
    )
    SELECT
      sp.store,
      sp.id,
      sp.name,
      CASE WHEN sp.price IS NOT NULL THEN round(sp.price * 100)::int END,
      CASE WHEN sp.base_price IS NOT NULL THEN round(sp.base_price * 100)::int END,
      sp.base_price_unit,
      true,
      sp.image_url,
      now()
    FROM catalog.silver_products sp
    WHERE sp.name IS NOT NULL
      AND (store_filter IS NULL OR sp.store = store_filter)
    ON CONFLICT (silver_product_id) WHERE silver_product_id IS NOT NULL DO UPDATE SET
      store_id             = EXCLUDED.store_id,
      name                 = EXCLUDED.name,
      current_price_cents  = EXCLUDED.current_price_cents,
      unit_price_cents     = EXCLUDED.unit_price_cents,
      unit_price_unit      = EXCLUDED.unit_price_unit,
      is_available         = EXCLUDED.is_available,
      image_url            = EXCLUDED.image_url,
      synced_at            = EXCLUDED.synced_at,
      updated_at           = now()
      -- ingredient_id and exact_product_group_id are intentionally preserved.
    RETURNING public.products.store_id
  )
  SELECT u.store_id, count(*)::integer
  FROM upserted u
  GROUP BY u.store_id
  ORDER BY u.store_id;
END;
$$;

GRANT EXECUTE ON FUNCTION catalog.promote_to_public(text) TO service_role;
