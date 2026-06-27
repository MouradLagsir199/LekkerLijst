-- Return every available product in the chosen canonical group.
--
-- Earlier versions collapsed each canonical group to one cheapest row per store.
-- The app now needs all rows so users can choose between brand/package variants.

CREATE OR REPLACE FUNCTION public.product_group_offers(p_product_id uuid)
RETURNS TABLE (
  product_id uuid,
  store_id text,
  product_name text,
  brand text,
  package_size_text text,
  current_price_cents int,
  unit_price_cents int,
  unit_price_unit text,
  image_url text,
  product_url text,
  canonical_key text
)
LANGUAGE sql
STABLE
AS $$
  WITH self AS (
    SELECT canonical_key AS k FROM public.products WHERE id = p_product_id
  )
  SELECT
    p.id, p.store_id, p.name, p.brand, p.package_size_text,
    p.current_price_cents, p.unit_price_cents, p.unit_price_unit,
    p.image_url, p.product_url, p.canonical_key
  FROM public.products p, self
  WHERE p.is_available = true
    AND p.canonical_key = self.k
    AND coalesce(self.k, '') <> ''
    AND (p.current_price_cents IS NULL OR p.current_price_cents > 0)
  ORDER BY
    (p.current_price_cents IS NULL) ASC,
    p.current_price_cents ASC NULLS LAST,
    p.unit_price_cents ASC NULLS LAST,
    p.store_id,
    p.name;
$$;

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
  WITH params_raw AS (
    SELECT
           btrim(regexp_replace(public.fold_text(coalesce(query_text, '')), '[^[:alnum:] ]+', ' ', 'g')) AS qfold,
           regexp_split_to_array(
             btrim(regexp_replace(public.fold_text(coalesce(query_text, '')), '[^[:alnum:] ]+', ' ', 'g')),
             '\s+'
           ) AS qtokens,
           regexp_replace(
             regexp_replace(
               btrim(regexp_replace(public.fold_text(coalesce(query_text, '')), '[^[:alnum:] ]+', ' ', 'g')),
               '\s+', '_', 'g'
             ),
             '^_+|_+$', '', 'g'
           ) AS raw_qkey
  ),
  params AS (
    SELECT
      qfold,
      qtokens,
      CASE raw_qkey
        WHEN 'melk' THEN 'halfvolle_melk'
        WHEN 'yoghurt' THEN 'halfvolle_yoghurt'
        WHEN 'rijst' THEN 'zilvervliesrijst'
        WHEN 'koffie' THEN 'filterkoffie'
        WHEN 'kaas' THEN 'jonge_kaas'
        ELSE raw_qkey
      END AS qkey
    FROM params_raw
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
    SELECT
      p.id, p.store_id, p.name, p.brand, p.category, p.package_size_text,
      p.current_price_cents, p.unit_price_cents, p.unit_price_unit,
      p.image_url, p.product_url,
      b.grp_score AS match_score, p.canonical_key, p.canonical_name,
      CASE
        WHEN params.qfold <> '' AND public.fold_text(p.name) = params.qfold THEN 4
        WHEN params.qfold <> '' AND public.fold_text(p.name) LIKE '%' || params.qfold || '%' THEN 3
        WHEN coalesce(array_length(params.qtokens, 1), 0) > 0
         AND NOT EXISTS (
           SELECT 1
           FROM unnest(params.qtokens) token
           WHERE token <> ''
             AND public.fold_text(p.name) NOT LIKE '%' || token || '%'
         ) THEN 2
        WHEN EXISTS (
           SELECT 1
           FROM unnest(params.qtokens) token
           WHERE token <> ''
             AND public.fold_text(p.name) LIKE '%' || token || '%'
         ) THEN 1
        ELSE 0
      END AS offer_rank
    FROM public.products p
    JOIN best b ON b.canonical_key = p.canonical_key
    CROSS JOIN params
    WHERE p.is_available = true
      AND (p.current_price_cents IS NULL OR p.current_price_cents > 0)
  )
  SELECT
    o.id, o.store_id, o.name, o.brand, o.category, o.package_size_text,
    o.current_price_cents, o.unit_price_cents, o.unit_price_unit,
    o.image_url, o.product_url, o.match_score, o.canonical_key, o.canonical_name
  FROM offers o
  ORDER BY
    o.offer_rank DESC,
    (o.current_price_cents IS NULL) ASC,
    o.current_price_cents ASC NULLS LAST,
    o.unit_price_cents ASC NULLS LAST,
    o.store_id,
    o.name;
$$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    GRANT EXECUTE ON FUNCTION public.product_group_offers(uuid) TO authenticated;
    GRANT EXECUTE ON FUNCTION public.search_product_offers(text, integer) TO authenticated;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    GRANT EXECUTE ON FUNCTION public.product_group_offers(uuid) TO service_role;
    GRANT EXECUTE ON FUNCTION public.search_product_offers(text, integer) TO service_role;
  END IF;
END;
$$;
