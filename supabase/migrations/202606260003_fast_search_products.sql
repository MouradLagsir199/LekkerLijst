create index if not exists gold_ingredient_aliases_alias_trgm_idx
  on catalog.gold_ingredient_aliases
  using gin (alias gin_trgm_ops);

create index if not exists gold_ingredients_canonical_name_trgm_idx
  on catalog.gold_ingredients
  using gin (canonical_name gin_trgm_ops);

create or replace function public.search_products(
  query_text text,
  store_filter text default null,
  match_count integer default 8
)
returns table (
  product_id uuid,
  store_id text,
  product_name text,
  brand text,
  category text,
  package_size_text text,
  current_price_cents integer,
  image_url text,
  product_url text,
  match_score numeric
)
language sql
stable
as $function$
  with norm as (
    select lower(trim(coalesce(query_text, ''))) as q
  ),
  tokens as (
    select word
    from unnest(string_to_array((select q from norm), ' ')) as word
    where length(word) >= 2
      and word not in (
        'de','het','een','van','met','en','of','in','op','voor','aan',
        'wit','rood','vers','biologisch','gezouten','klein','groot',
        'heel','fijn','jong','oud','licht','extra','zonder'
      )
  ),
  product_candidates as (
    select
      p.id as product_id,
      p.store_id,
      p.name as product_name,
      p.brand,
      p.category,
      p.package_size_text,
      p.current_price_cents,
      p.image_url,
      p.product_url,
      similarity(lower(p.name), (select q from norm))::numeric as match_score
    from public.products p
    where p.is_available = true
      and (store_filter is null or p.store_id = store_filter)
      and (select q from norm) <> ''
      and (
        p.name ilike '%' || (select q from norm) || '%'
        or lower(p.name) % (select q from norm)
        or exists (
          select 1
          from tokens t
          where p.name ilike '%' || t.word || '%'
        )
      )
    order by match_score desc, p.current_price_cents asc nulls last, p.name asc
    limit 1000
  ),
  alias_candidates as (
    select
      p.id as product_id,
      p.store_id,
      p.name as product_name,
      p.brand,
      p.category,
      p.package_size_text,
      p.current_price_cents,
      p.image_url,
      p.product_url,
      max(
        greatest(
          similarity(a.alias, (select q from norm)),
          similarity(gi.canonical_name, (select q from norm))
        )
        + case when gi.canonical_name = (select q from norm) then 0.5 else 0 end
      )::numeric as match_score
    from catalog.gold_ingredient_aliases a
    join catalog.gold_ingredients gi on gi.id = a.ingredient_id
    join public.products p on p.ingredient_id = gi.id
    where p.is_available = true
      and (store_filter is null or p.store_id = store_filter)
      and (select q from norm) <> ''
      and (
        a.alias ilike '%' || (select q from norm) || '%'
        or gi.canonical_name ilike '%' || (select q from norm) || '%'
        or a.alias % (select q from norm)
        or gi.canonical_name % (select q from norm)
        or exists (
          select 1
          from tokens t
          where a.alias ilike '%' || t.word || '%'
             or gi.canonical_name ilike '%' || t.word || '%'
        )
      )
    group by
      p.id, p.store_id, p.name, p.brand, p.category, p.package_size_text,
      p.current_price_cents, p.image_url, p.product_url
    order by match_score desc, p.current_price_cents asc nulls last, p.name asc
    limit 1000
  ),
  combined as (
    select * from product_candidates
    union all
    select * from alias_candidates
  ),
  best as (
    select distinct on (product_id)
      product_id,
      store_id,
      product_name,
      brand,
      category,
      package_size_text,
      current_price_cents,
      image_url,
      product_url,
      match_score
    from combined
    where match_score > 0.10
    order by product_id, match_score desc
  )
  select
    product_id,
    store_id,
    product_name,
    brand,
    category,
    package_size_text,
    current_price_cents,
    image_url,
    product_url,
    match_score
  from best
  order by match_score desc, current_price_cents asc nulls last, product_name asc
  limit least(greatest(match_count, 1), 20);
$function$;

grant execute on function public.search_products(text, text, integer) to anon, authenticated, service_role;
