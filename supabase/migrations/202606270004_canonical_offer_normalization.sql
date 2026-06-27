-- Keep app-facing product grouping stable after AI recanon.
--
-- The AI pass correctly coarsened many keys, but some common beverage variants
-- remained fragmented, e.g. coca_cola_regular / cola_regular / cola_regulier.
-- Normalize those deterministic synonyms at the public product layer and make
-- search_product_offers prefer query-specific rows inside the chosen group.

WITH keyed AS (
  SELECT
    p.id,
    CASE
      WHEN k IN (
        'coca_cola', 'coca_cola_regular', 'cola_original_taste',
        'cola_regular', 'cola_regulier', 'cola_regular_frisdrank',
        'pepsi_cola'
      ) THEN 'cola'
      WHEN k IN (
        'coca_cola_zero', 'cola_zero_frisdrank', 'cola_zero_sugar',
        'cola_zero_suiker'
      ) THEN 'cola_zero'
      WHEN k IN ('cola_cherry', 'cola_kers_frisdrank') THEN 'cola_kers'
      WHEN k IN ('cola_zero_cherry', 'cola_zero_kers_frisdrank') THEN 'cola_zero_kers'
      WHEN k = 'cola_vanilla' THEN 'cola_vanille'
      ELSE k
    END AS normalized_key
  FROM public.products p
  CROSS JOIN LATERAL (
    SELECT regexp_replace(
             regexp_replace(public.fold_text(coalesce(p.canonical_key, '')), '[^[:alnum:]]+', '_', 'g'),
             '^_+|_+$',
             '',
             'g'
           ) AS k
  ) folded
  WHERE coalesce(p.canonical_key, '') <> ''
),
named AS (
  SELECT
    id,
    normalized_key,
    CASE normalized_key
      WHEN 'cola' THEN 'Cola'
      WHEN 'cola_zero' THEN 'Cola zero'
      WHEN 'cola_kers' THEN 'Cola kers'
      WHEN 'cola_zero_kers' THEN 'Cola zero kers'
      WHEN 'cola_vanille' THEN 'Cola vanille'
      ELSE NULL
    END AS normalized_name
  FROM keyed
)
UPDATE public.products p
SET canonical_key = named.normalized_key,
    canonical_name = COALESCE(named.normalized_name, NULLIF(btrim(p.canonical_name), '')),
    updated_at = now()
FROM named
WHERE named.id = p.id
  AND (
    p.canonical_key IS DISTINCT FROM named.normalized_key
    OR (
      named.normalized_name IS NOT NULL
      AND p.canonical_name IS DISTINCT FROM named.normalized_name
    )
  );

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
    SELECT DISTINCT ON (p.store_id)
      p.id, p.store_id, p.name, p.brand, p.category, p.package_size_text,
      p.current_price_cents, p.unit_price_cents, p.unit_price_unit,
      p.image_url, p.product_url,
      b.grp_score AS match_score, p.canonical_key, p.canonical_name
    FROM public.products p
    JOIN best b ON b.canonical_key = p.canonical_key
    CROSS JOIN params
    WHERE p.is_available = true
      AND (p.current_price_cents IS NULL OR p.current_price_cents > 0)
    ORDER BY
      p.store_id,
      (p.current_price_cents IS NULL) ASC,
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
      END DESC,
      p.current_price_cents ASC NULLS LAST,
      p.unit_price_cents ASC NULLS LAST,
      p.name
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
