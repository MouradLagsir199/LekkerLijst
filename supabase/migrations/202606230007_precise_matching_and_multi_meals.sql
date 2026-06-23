-- A meal slot is a grouping (breakfast, lunch or dinner), not a single item.
alter table public.meal_plan_items
drop constraint if exists meal_plan_items_meal_plan_id_planned_for_meal_slot_key;

create index if not exists meal_plan_items_plan_day_slot_idx
on public.meal_plan_items (meal_plan_id, planned_for, meal_slot, created_at);

-- Product fuzzy matching is useful for typos, but it must never be the only
-- signal. A result needs to share a meaningful whole token with the query.
create or replace function public.search_products(
  query_text text,
  store_filter text default null,
  match_count int default 8
)
returns table (
  product_id uuid,
  store_id text,
  product_name text,
  brand text,
  category text,
  package_size_text text,
  current_price_cents int,
  image_url text,
  product_url text,
  match_score numeric
)
language sql
stable
as $$
  with normalized_query as (
    select case lower(trim(query_text))
      when 'boneless skinless chicken breast' then 'kipfilet'
      when 'boneless chicken breast' then 'kipfilet'
      when 'skinless chicken breast' then 'kipfilet'
      when 'chicken breast' then 'kipfilet'
      when 'butter' then 'boter'
      else lower(trim(query_text))
    end as value
  ),
  query as (
    select
      value,
      array(
        select token
        from unnest(regexp_split_to_array(regexp_replace(value, '[^[:alnum:]]+', ' ', 'g'), '\s+')) as token
        where length(token) >= 2
          and token not in (
            'wit', 'witte', 'rood', 'rode', 'groen', 'groene', 'vers', 'verse',
            'biologisch', 'biologische', 'gezouten', 'ongezouten', 'klein', 'kleine',
            'groot', 'grote', 'heel', 'hele', 'fijn', 'fijngesneden'
          )
      ) as content_tokens
    from normalized_query
  ),
  canonical_scores as (
    select
      c.id,
      c.canonical_name,
      greatest(
        similarity(lower(c.canonical_name), query.value),
        coalesce(max(similarity(lower(alias.alias), query.value)), 0)
      ) as canonical_score,
      bool_or(lower(c.canonical_name) = query.value or lower(alias.alias) = query.value) as exact_canonical_match,
      coalesce(string_agg(lower(alias.alias), ' '), '') as aliases_text
    from public.canonical_ingredients c
    left join public.ingredient_aliases alias on alias.canonical_ingredient_id = c.id
    cross join query
    group by c.id, c.canonical_name, query.value
  ),
  scored as (
    select
      p.*,
      regexp_replace(
        lower(concat_ws(' ', p.name, p.brand, p.category, p.subcategory, canonical_scores.canonical_name, canonical_scores.aliases_text)),
        '[^[:alnum:]]+',
        ' ',
        'g'
      ) as searchable_text,
      greatest(
        similarity(lower(p.name), query.value),
        similarity(lower(coalesce(p.subcategory, '')), query.value),
        coalesce(canonical_scores.canonical_score, 0)
      ) +
      case when lower(p.name) like '%' || query.value || '%' then 0.40 else 0 end +
      case when lower(coalesce(p.subcategory, '')) like '%' || query.value || '%' then 0.35 else 0 end +
      case when canonical_scores.exact_canonical_match then 0.50 else 0 end +
      case when store_filter is not null and p.store_id = store_filter then 0.05 else 0 end as score
    from public.products p
    left join canonical_scores on canonical_scores.id = p.canonical_ingredient_id
    cross join query
    where p.is_available = true
      and query.value <> ''
      and (store_filter is null or p.store_id = store_filter)
  )
  select
    id as product_id,
    store_id,
    name as product_name,
    brand,
    category,
    package_size_text,
    current_price_cents,
    image_url,
    product_url,
    score::numeric as match_score
  from scored
  cross join query
  where score > 0.30
    and (
      cardinality(query.content_tokens) = 0
      or exists (
        select 1
        from unnest(query.content_tokens) as token
        where (' ' || searchable_text || ' ') like '% ' || token || ' %'
      )
    )
  order by score desc, current_price_cents asc, name asc
  limit least(greatest(match_count, 1), 20);
$$;
