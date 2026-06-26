drop index if exists catalog.exact_product_groups_ean_idx;
drop index if exists catalog.gold_ingredients_ean_idx;

create or replace function catalog.catalog_pack_signature(
  product_name text,
  price numeric,
  base_price numeric,
  base_unit text
)
returns text
language sql
immutable
as $$
  with input as (
    select
      lower(coalesce(product_name, '')) as name_value,
      nullif(lower(btrim(coalesce(base_unit, ''))), '') as unit_value,
      case
        when price is not null and base_price is not null and base_price > 0
        then round(price / base_price, 2)
      end as qty_value
  ),
  parts as (
    select
      case
        when qty_value is not null
        then 'qty:' || qty_value::text || ':' || coalesce(unit_value, 'unit')
      end as qty_part,
      case
        when name_value ~ '\m[0-9]+\s*[- ]?pack\M'
        then 'pack:' || substring(name_value from '\m([0-9]+)\s*[- ]?pack\M')
      end as pack_part,
      case
        when name_value ~ '\m[0-9]+\s*x\s*[0-9]+'
        then 'multi:' || substring(name_value from '\m([0-9]+\s*x\s*[0-9]+([,.][0-9]+)?\s*(ml|cl|l|g|kg))')
      end as multi_part,
      case
        when name_value ~ '\m[0-9]+([,.][0-9]+)?\s*(ml|cl|l|g|kg)\M'
        then 'size:' || substring(name_value from '\m([0-9]+([,.][0-9]+)?\s*(ml|cl|l|g|kg))\M')
      end as size_part
    from input
  )
  select coalesce(
    nullif(concat_ws('|', qty_part, pack_part, multi_part, size_part), ''),
    'name:' || coalesce(catalog.catalog_norm(product_name), 'unknown')
  )
  from parts;
$$;

create or replace function catalog.refresh_exact_ean_groups()
returns table(step text, rows_affected integer)
language plpgsql
as $$
declare
  cleared_exact int := 0;
  cleared_substitute int := 0;
  exact_groups int := 0;
  exact_members int := 0;
  public_exact int := 0;
  substitute_groups int := 0;
  substitute_mappings int := 0;
  public_substitute int := 0;
begin
  update public.products pp
     set exact_product_group_id = null,
         updated_at = now()
    from catalog.exact_product_groups eg
   where pp.exact_product_group_id = eg.id
     and eg.source = 'ean'
     and eg.group_key like 'ean:%';
  get diagnostics cleared_exact = row_count;

  update public.products pp
     set ingredient_id = null,
         updated_at = now()
    from catalog.gold_ingredients gi
   where pp.ingredient_id = gi.id
     and gi.source = 'ean'
     and gi.group_key like 'ean:%';
  get diagnostics cleared_substitute = row_count;

  delete from catalog.exact_product_groups
   where source = 'ean'
     and group_key like 'ean:%';

  delete from catalog.gold_ingredients
   where source = 'ean'
     and group_key like 'ean:%';

  with source_rows as (
    select
      sp.id,
      btrim(sp.ean) as ean,
      sp.store,
      sp.name,
      catalog.catalog_pack_signature(sp.name, sp.price, sp.base_price, sp.base_price_unit) as pack_sig,
      coalesce(catalog.catalog_variant_signature(sp.name), '') as variant_sig
    from catalog.silver_products sp
    where sp.ean is not null
      and btrim(sp.ean) <> ''
      and lower(btrim(sp.ean)) not in ('null', 'none', 'n/a')
      and sp.name is not null
  ),
  grouped as (
    select
      'ean:' || ean || ':pack:' || pack_sig || ':variant:' || variant_sig as group_key,
      ean,
      pack_sig,
      variant_sig,
      min(name) as canonical_name,
      count(*) as member_count
    from source_rows
    group by ean, pack_sig, variant_sig
    having count(*) > 1
  ),
  ins as (
    insert into catalog.exact_product_groups (group_key, canonical_name, ean, source, confidence)
    select group_key, canonical_name, ean, 'ean', 1.0
    from grouped
    on conflict (group_key) do update set
      canonical_name = excluded.canonical_name,
      ean = excluded.ean,
      source = 'ean',
      confidence = 1.0,
      updated_at = now()
    returning 1
  )
  select count(*) into exact_groups from ins;

  with source_rows as (
    select
      sp.id,
      btrim(sp.ean) as ean,
      catalog.catalog_pack_signature(sp.name, sp.price, sp.base_price, sp.base_price_unit) as pack_sig,
      coalesce(catalog.catalog_variant_signature(sp.name), '') as variant_sig
    from catalog.silver_products sp
    where sp.ean is not null
      and btrim(sp.ean) <> ''
      and lower(btrim(sp.ean)) not in ('null', 'none', 'n/a')
      and sp.name is not null
  ),
  keyed as (
    select
      id,
      'ean:' || ean || ':pack:' || pack_sig || ':variant:' || variant_sig as group_key
    from source_rows
  ),
  ins as (
    insert into catalog.exact_product_group_members (
      exact_product_group_id,
      silver_product_id,
      source,
      confidence
    )
    select eg.id, k.id, 'ean', 1.0
    from keyed k
    join catalog.exact_product_groups eg on eg.group_key = k.group_key
    on conflict (silver_product_id) do update set
      exact_product_group_id = excluded.exact_product_group_id,
      source = 'ean',
      confidence = 1.0
    where catalog.exact_product_group_members.source = 'ean'
    returning 1
  )
  select count(*) into exact_members from ins;

  update public.products pp
     set exact_product_group_id = egm.exact_product_group_id,
         updated_at = now()
    from catalog.exact_product_group_members egm
   where egm.silver_product_id = pp.silver_product_id
     and (pp.exact_product_group_id is null or exists (
       select 1
       from catalog.exact_product_groups old_eg
       where old_eg.id = pp.exact_product_group_id
         and old_eg.source = 'ean'
     ))
     and pp.exact_product_group_id is distinct from egm.exact_product_group_id;
  get diagnostics public_exact = row_count;

  with source_rows as (
    select
      sp.id,
      btrim(sp.ean) as ean,
      sp.store,
      sp.name,
      catalog.catalog_pack_signature(sp.name, sp.price, sp.base_price, sp.base_price_unit) as pack_sig,
      coalesce(catalog.catalog_variant_signature(sp.name), '') as variant_sig
    from catalog.silver_products sp
    where sp.ean is not null
      and btrim(sp.ean) <> ''
      and lower(btrim(sp.ean)) not in ('null', 'none', 'n/a')
      and sp.name is not null
  ),
  grouped as (
    select
      'ean:' || ean || ':pack:' || pack_sig || ':variant:' || variant_sig as group_key,
      ean,
      pack_sig,
      variant_sig,
      min(name) as canonical_name,
      count(*) as member_count
    from source_rows
    group by ean, pack_sig, variant_sig
    having count(*) > 1
  ),
  ins as (
    insert into catalog.gold_ingredients (
      group_key,
      group_kind,
      canonical_name,
      ean,
      source,
      confidence,
      updated_at
    )
    select group_key, 'substitute', canonical_name, ean, 'ean', 1.0, now()
    from grouped
    on conflict (group_key) do update set
      canonical_name = excluded.canonical_name,
      ean = excluded.ean,
      source = 'ean',
      confidence = 1.0,
      updated_at = now()
    returning 1
  )
  select count(*) into substitute_groups from ins;

  insert into catalog.gold_ingredient_aliases (ingredient_id, alias, language, confidence)
  select distinct gi.id, sp.name, 'nl', 1.0
  from catalog.silver_products sp
  join catalog.gold_ingredients gi
    on gi.group_key =
      'ean:' || btrim(sp.ean) ||
      ':pack:' || catalog.catalog_pack_signature(sp.name, sp.price, sp.base_price, sp.base_price_unit) ||
      ':variant:' || coalesce(catalog.catalog_variant_signature(sp.name), '')
  where sp.ean is not null
    and btrim(sp.ean) <> ''
    and lower(btrim(sp.ean)) not in ('null', 'none', 'n/a')
    and sp.name is not null
  on conflict (ingredient_id, alias) do nothing;

  with source_rows as (
    select
      sp.id,
      btrim(sp.ean) as ean,
      sp.name,
      sp.price,
      sp.base_price,
      sp.base_price_unit
    from catalog.silver_products sp
    where sp.ean is not null
      and btrim(sp.ean) <> ''
      and lower(btrim(sp.ean)) not in ('null', 'none', 'n/a')
      and sp.name is not null
  ),
  keyed as (
    select
      id,
      'ean:' || ean ||
        ':pack:' || catalog.catalog_pack_signature(name, price, base_price, base_price_unit) ||
        ':variant:' || coalesce(catalog.catalog_variant_signature(name), '') as group_key
    from source_rows
  ),
  ins as (
    insert into catalog.gold_product_mappings (
      silver_product_id,
      ingredient_id,
      confidence,
      mapping_source,
      review_status
    )
    select k.id, gi.id, 1.0, 'rule', 'approved'
    from keyed k
    join catalog.gold_ingredients gi on gi.group_key = k.group_key
    on conflict (silver_product_id, ingredient_id) do update set
      confidence = 1.0,
      mapping_source = 'rule',
      review_status = 'approved'
    returning 1
  )
  select count(*) into substitute_mappings from ins;

  update public.products pp
     set ingredient_id = gm.ingredient_id,
         updated_at = now()
    from catalog.gold_product_mappings gm
    join catalog.gold_ingredients gi on gi.id = gm.ingredient_id
   where gm.silver_product_id = pp.silver_product_id
     and gm.review_status = 'approved'
     and gi.source = 'ean'
     and (pp.ingredient_id is null or exists (
       select 1
       from catalog.gold_ingredients old_gi
       where old_gi.id = pp.ingredient_id
         and old_gi.source = 'ean'
     ))
     and pp.ingredient_id is distinct from gm.ingredient_id;
  get diagnostics public_substitute = row_count;

  return query values
    ('cleared_exact_products'::text, cleared_exact),
    ('cleared_substitute_products'::text, cleared_substitute),
    ('exact_groups'::text, exact_groups),
    ('exact_members'::text, exact_members),
    ('public_exact_products'::text, public_exact),
    ('substitute_groups'::text, substitute_groups),
    ('substitute_mappings'::text, substitute_mappings),
    ('public_substitute_products'::text, public_substitute);
end;
$$;

grant execute on function catalog.catalog_pack_signature(text, numeric, numeric, text) to service_role;
grant execute on function catalog.refresh_exact_ean_groups() to service_role;
