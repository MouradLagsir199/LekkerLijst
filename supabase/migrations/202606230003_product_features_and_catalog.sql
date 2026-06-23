alter table public.recipes
add column if not exists image_url text,
add column if not exists tags text[] not null default '{}';

create index if not exists recipes_tags_idx on public.recipes using gin (tags);

create table if not exists public.meal_plans (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  week_start date not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, week_start)
);

create table if not exists public.meal_plan_items (
  id uuid primary key default gen_random_uuid(),
  meal_plan_id uuid not null references public.meal_plans(id) on delete cascade,
  planned_for date not null,
  meal_slot text not null default 'dinner' check (meal_slot in ('breakfast', 'lunch', 'dinner')),
  recipe_id uuid references public.recipes(id) on delete set null,
  custom_title text,
  note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (recipe_id is not null or custom_title is not null),
  unique (meal_plan_id, planned_for, meal_slot)
);

drop trigger if exists meal_plans_set_updated_at on public.meal_plans;
create trigger meal_plans_set_updated_at
before update on public.meal_plans
for each row execute function public.set_updated_at();

drop trigger if exists meal_plan_items_set_updated_at on public.meal_plan_items;
create trigger meal_plan_items_set_updated_at
before update on public.meal_plan_items
for each row execute function public.set_updated_at();

alter table public.meal_plans enable row level security;
alter table public.meal_plan_items enable row level security;

create policy "meal_plans_select_own" on public.meal_plans for select to authenticated using (auth.uid() = user_id);
create policy "meal_plans_insert_own" on public.meal_plans for insert to authenticated with check (auth.uid() = user_id);
create policy "meal_plans_update_own" on public.meal_plans for update to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "meal_plans_delete_own" on public.meal_plans for delete to authenticated using (auth.uid() = user_id);

create policy "meal_plan_items_select_own" on public.meal_plan_items for select to authenticated using (
  exists (select 1 from public.meal_plans mp where mp.id = meal_plan_id and mp.user_id = auth.uid())
);
create policy "meal_plan_items_insert_own" on public.meal_plan_items for insert to authenticated with check (
  exists (select 1 from public.meal_plans mp where mp.id = meal_plan_id and mp.user_id = auth.uid())
);
create policy "meal_plan_items_update_own" on public.meal_plan_items for update to authenticated using (
  exists (select 1 from public.meal_plans mp where mp.id = meal_plan_id and mp.user_id = auth.uid())
) with check (
  exists (select 1 from public.meal_plans mp where mp.id = meal_plan_id and mp.user_id = auth.uid())
);
create policy "meal_plan_items_delete_own" on public.meal_plan_items for delete to authenticated using (
  exists (select 1 from public.meal_plans mp where mp.id = meal_plan_id and mp.user_id = auth.uid())
);

create table if not exists public.scrape_runs (
  id uuid primary key default gen_random_uuid(),
  store_id text not null references public.stores(id),
  source text not null default 'github_actions',
  status text not null default 'running' check (status in ('running', 'completed', 'failed')),
  artifact_path text,
  started_at timestamptz not null default now(),
  completed_at timestamptz,
  raw_row_count integer not null default 0,
  silver_row_count integer not null default 0,
  error_message text,
  created_at timestamptz not null default now()
);

create table if not exists public.bronze_artifacts (
  id uuid primary key default gen_random_uuid(),
  scrape_run_id uuid not null references public.scrape_runs(id) on delete cascade,
  storage_path text not null,
  content_type text not null default 'text/csv',
  byte_size bigint,
  sha256 text,
  created_at timestamptz not null default now(),
  unique (scrape_run_id, storage_path)
);

create table if not exists public.bronze_products (
  id uuid primary key default gen_random_uuid(),
  scrape_run_id uuid not null references public.scrape_runs(id) on delete cascade,
  store_id text not null references public.stores(id),
  external_product_id text not null,
  raw_product jsonb not null,
  source_row_number integer,
  scraped_at timestamptz,
  created_at timestamptz not null default now(),
  unique (scrape_run_id, external_product_id)
);

create index if not exists bronze_products_store_external_idx on public.bronze_products (store_id, external_product_id);

create table if not exists public.silver_products (
  id uuid primary key default gen_random_uuid(),
  bronze_product_id uuid not null references public.bronze_products(id) on delete cascade,
  scrape_run_id uuid not null references public.scrape_runs(id) on delete cascade,
  store_id text not null references public.stores(id),
  external_product_id text not null,
  name text not null,
  brand text,
  category text,
  subcategory text,
  image_url text,
  product_url text,
  package_size_text text,
  unit_quantity numeric,
  unit_type text,
  current_price_cents integer not null check (current_price_cents >= 0),
  unit_price_cents integer,
  unit_price_unit text,
  is_available boolean not null default true,
  promotion jsonb,
  attributes jsonb not null default '{}',
  scraped_at timestamptz not null default now(),
  is_current boolean not null default true,
  created_at timestamptz not null default now(),
  unique (scrape_run_id, external_product_id)
);

create unique index if not exists silver_products_current_store_external_idx
on public.silver_products (store_id, external_product_id)
where is_current;

create table if not exists public.canonical_ingredients (
  id uuid primary key default gen_random_uuid(),
  canonical_name text not null unique,
  category text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.ingredient_aliases (
  id uuid primary key default gen_random_uuid(),
  canonical_ingredient_id uuid not null references public.canonical_ingredients(id) on delete cascade,
  alias text not null unique,
  source text not null default 'ai' check (source in ('ai', 'admin', 'seed')),
  confidence numeric not null default 1 check (confidence >= 0 and confidence <= 1),
  created_at timestamptz not null default now()
);

alter table public.products
add column if not exists canonical_ingredient_id uuid references public.canonical_ingredients(id) on delete set null,
add column if not exists silver_product_id uuid references public.silver_products(id) on delete set null;

create table if not exists public.product_canonical_ingredients (
  product_id uuid primary key references public.products(id) on delete cascade,
  canonical_ingredient_id uuid not null references public.canonical_ingredients(id) on delete cascade,
  confidence numeric not null check (confidence >= 0 and confidence <= 1),
  mapping_source text not null default 'ai' check (mapping_source in ('ai', 'admin', 'seed')),
  review_status text not null default 'pending' check (review_status in ('pending', 'approved', 'rejected')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

drop trigger if exists canonical_ingredients_set_updated_at on public.canonical_ingredients;
create trigger canonical_ingredients_set_updated_at
before update on public.canonical_ingredients
for each row execute function public.set_updated_at();

drop trigger if exists product_canonical_ingredients_set_updated_at on public.product_canonical_ingredients;
create trigger product_canonical_ingredients_set_updated_at
before update on public.product_canonical_ingredients
for each row execute function public.set_updated_at();

alter table public.scrape_runs enable row level security;
alter table public.bronze_artifacts enable row level security;
alter table public.bronze_products enable row level security;
alter table public.silver_products enable row level security;
alter table public.canonical_ingredients enable row level security;
alter table public.ingredient_aliases enable row level security;
alter table public.product_canonical_ingredients enable row level security;

create policy "canonical_ingredients_select_authenticated" on public.canonical_ingredients for select to authenticated using (true);
create policy "ingredient_aliases_select_authenticated" on public.ingredient_aliases for select to authenticated using (true);

insert into storage.buckets (id, name, public)
values ('catalog-bronze', 'catalog-bronze', false)
on conflict (id) do nothing;

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
  order by current_price_cents asc, score desc, name asc
  limit least(greatest(match_count, 1), 20);
$$;
