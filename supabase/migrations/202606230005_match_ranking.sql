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
    select lower(trim(query_text)) as value
  ),
  scored as (
    select
      p.*,
      greatest(
        similarity(lower(p.name), query.value),
        similarity(lower(coalesce(p.subcategory, '')), query.value),
        similarity(lower(coalesce(c.canonical_name, '')), query.value),
        coalesce((
          select max(similarity(lower(alias.alias), query.value))
          from public.ingredient_aliases alias
          where alias.canonical_ingredient_id = c.id
        ), 0)
      ) +
      case when lower(p.name) like '%' || query.value || '%' then 0.40 else 0 end +
      case when lower(coalesce(p.subcategory, '')) like '%' || query.value || '%' then 0.35 else 0 end +
      case when lower(coalesce(c.canonical_name, '')) = query.value then 0.50 else 0 end +
      case when store_filter is not null and p.store_id = store_filter then 0.05 else 0 end as score
    from public.products p
    left join public.canonical_ingredients c on c.id = p.canonical_ingredient_id
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
  where score > 0.12
  order by score desc, current_price_cents asc, name asc
  limit least(greatest(match_count, 1), 20);
$$;
