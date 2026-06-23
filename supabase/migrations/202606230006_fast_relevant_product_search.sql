create index if not exists ingredient_aliases_canonical_ingredient_idx
on public.ingredient_aliases (canonical_ingredient_id);

create index if not exists products_canonical_ingredient_idx
on public.products (canonical_ingredient_id)
where is_available = true;

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
  with query as (
    select case lower(trim(query_text))
      when 'boneless skinless chicken breast' then 'kipfilet'
      when 'boneless chicken breast' then 'kipfilet'
      when 'skinless chicken breast' then 'kipfilet'
      when 'chicken breast' then 'kipfilet'
      when 'butter' then 'boter'
      else lower(trim(query_text))
    end as value
  ),
  canonical_scores as (
    select
      c.id,
      greatest(
        similarity(lower(c.canonical_name), query.value),
        coalesce(max(similarity(lower(alias.alias), query.value)), 0)
      ) as canonical_score,
      bool_or(lower(c.canonical_name) = query.value or lower(alias.alias) = query.value) as exact_canonical_match
    from public.canonical_ingredients c
    left join public.ingredient_aliases alias on alias.canonical_ingredient_id = c.id
    cross join query
    group by c.id, c.canonical_name, query.value
  ),
  scored as (
    select
      p.*,
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
  where score > 0.30
  order by score desc, current_price_cents asc, name asc
  limit least(greatest(match_count, 1), 20);
$$;
