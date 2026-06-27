-- Prefer an exact canonical key match for query -> offer-group lookup.
--
-- Before this, a broad query like "cola" could choose the "cola_zero" group
-- because the underlying text search ranked a zero/no-sugar product at 1.0.
-- The offer RPC is meant to choose one coherent product group, so exact
-- canonical keys such as cola, cola_zero, and cola_light should win when the
-- user query maps directly to them.

CREATE OR REPLACE FUNCTION public.search_product_offers(query_text text, match_count integer DEFAULT 8)
RETURNS TABLE (
  product_id uuid,
  store_id text,
  product_name text,
  brand text,
  category text,
  package_size_text text,
  current_price_cents integer,
  unit_price_cents integer,
  unit_price_unit text,
  image_url text,
  product_url text,
  match_score numeric,
  canonical_key text,
  display_name text
)
LANGUAGE sql
STABLE
AS $$
  WITH params AS (
    SELECT regexp_replace(
             regexp_replace(
               btrim(regexp_replace(public.fold_text(coalesce(query_text, '')), '[^[:alnum:] ]+', ' ', 'g')),
               '\s+', '_', 'g'
             ),
             '^_+|_+$', '', 'g'
           ) AS qkey
  ),
  exact AS (
    SELECT
      p.canonical_key,
      1.0::numeric AS grp_score,
      count(*) AS hits,
      count(DISTINCT p.store_id) AS stores
    FROM public.products p, params
    WHERE p.is_available = true
      AND coalesce(p.canonical_key, '') <> ''
      AND p.canonical_key = params.qkey
    GROUP BY p.canonical_key
  ),
  ranked AS (
    SELECT s.match_score, p.canonical_key
    FROM public.search_products(query_text, NULL, greatest(coalesce(match_count, 8), 12)) s
    JOIN public.products p ON p.id = s.product_id
    WHERE coalesce(p.canonical_key, '') <> ''
  ),
  cand AS (
    SELECT
      r.canonical_key,
      max(r.match_score) AS grp_score,
      count(*) AS hits,
      (SELECT count(DISTINCT pp.store_id)
       FROM public.products pp
       WHERE pp.canonical_key = r.canonical_key AND pp.is_available) AS stores
    FROM ranked r
    GROUP BY r.canonical_key
  ),
  best AS (
    SELECT canonical_key, grp_score
    FROM (
      SELECT 0 AS priority, canonical_key, grp_score, hits, stores
      FROM exact
      UNION ALL
      SELECT 1 AS priority, canonical_key, grp_score, hits, stores
      FROM cand
      WHERE NOT EXISTS (SELECT 1 FROM exact)
        AND grp_score >= (SELECT max(grp_score) FROM cand) - 0.06
    ) choice
    ORDER BY priority, stores DESC, grp_score DESC, hits DESC, canonical_key
    LIMIT 1
  ),
  offers AS (
    SELECT DISTINCT ON (p.store_id)
      p.id, p.store_id, p.name, p.brand, p.category, p.package_size_text,
      p.current_price_cents, p.unit_price_cents, p.unit_price_unit,
      p.image_url, p.product_url,
      b.grp_score AS match_score, p.canonical_key, p.canonical_name
    FROM public.products p
    JOIN best b ON b.canonical_key = p.canonical_key
    WHERE p.is_available = true
    ORDER BY p.store_id, p.current_price_cents ASC NULLS LAST, p.name
  )
  SELECT
    o.id, o.store_id, o.name, o.brand, o.category, o.package_size_text,
    o.current_price_cents, o.unit_price_cents, o.unit_price_unit,
    o.image_url, o.product_url, o.match_score, o.canonical_key, o.canonical_name
  FROM offers o
  ORDER BY o.current_price_cents ASC NULLS LAST, o.store_id;
$$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    GRANT EXECUTE ON FUNCTION public.search_product_offers(text, integer) TO authenticated;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    GRANT EXECUTE ON FUNCTION public.search_product_offers(text, integer) TO service_role;
  END IF;
END;
$$;
