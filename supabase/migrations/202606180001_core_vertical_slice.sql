create extension if not exists pgcrypto;
create extension if not exists pg_trgm;

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  display_name text,
  preferred_store text not null default 'ah' check (preferred_store in ('ah', 'jumbo', 'dirk', 'plus')),
  language text not null default 'nl' check (language in ('nl', 'en')),
  household_size int not null default 1 check (household_size > 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.recipes (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  title text not null,
  description text,
  servings int,
  prep_time_minutes int,
  cook_time_minutes int,
  total_time_minutes int,
  source_url text,
  source_platform text,
  confidence_score numeric not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.recipe_ingredients (
  id uuid primary key default gen_random_uuid(),
  recipe_id uuid not null references public.recipes(id) on delete cascade,
  raw_text text not null,
  quantity numeric,
  unit text,
  ingredient_name text not null,
  normalized_ingredient_name text,
  dutch_ingredient_name text,
  preparation text,
  optional boolean not null default false,
  sort_order int not null default 0
);

create table if not exists public.recipe_instructions (
  id uuid primary key default gen_random_uuid(),
  recipe_id uuid not null references public.recipes(id) on delete cascade,
  step_number int not null,
  instruction text not null
);

create table if not exists public.stores (
  id text primary key check (id in ('ah', 'jumbo', 'dirk', 'plus')),
  name text not null,
  country text not null default 'NL'
);

create table if not exists public.products (
  id uuid primary key default gen_random_uuid(),
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
  current_price_cents int not null check (current_price_cents >= 0),
  unit_price_cents int,
  unit_price_unit text,
  is_available boolean not null default true,
  last_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (store_id, external_product_id)
);

create index if not exists products_name_trgm_idx on public.products using gin (name gin_trgm_ops);
create index if not exists products_store_available_idx on public.products (store_id, is_available);

create table if not exists public.recipe_product_matches (
  id uuid primary key default gen_random_uuid(),
  recipe_ingredient_id uuid not null references public.recipe_ingredients(id) on delete cascade,
  product_id uuid not null references public.products(id),
  match_score numeric not null default 0,
  match_reason text,
  selected boolean not null default false,
  created_at timestamptz not null default now()
);

create table if not exists public.shopping_lists (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  recipe_id uuid references public.recipes(id) on delete set null,
  title text not null,
  store_id text references public.stores(id),
  estimated_total_cents int not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.shopping_list_items (
  id uuid primary key default gen_random_uuid(),
  shopping_list_id uuid not null references public.shopping_lists(id) on delete cascade,
  ingredient_name text not null,
  normalized_ingredient_name text,
  quantity numeric,
  unit text,
  selected_product_id uuid references public.products(id),
  store_id text references public.stores(id),
  estimated_price_cents int,
  category text,
  checked boolean not null default false,
  sort_order int not null default 0,
  created_at timestamptz not null default now()
);

create table if not exists public.import_cache (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  source_hash text not null,
  raw_text_hash text not null,
  parsed_recipe_json jsonb not null,
  created_at timestamptz not null default now(),
  unique (user_id, raw_text_hash)
);

create table if not exists public.ai_usage_logs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete set null,
  task_type text not null,
  model text not null,
  input_tokens int,
  output_tokens int,
  estimated_cost_cents numeric,
  created_at timestamptz not null default now()
);

insert into public.stores (id, name, country) values
  ('ah', 'Albert Heijn', 'NL'),
  ('jumbo', 'Jumbo', 'NL'),
  ('dirk', 'Dirk', 'NL'),
  ('plus', 'PLUS', 'NL')
on conflict (id) do update set name = excluded.name;

insert into public.products (
  store_id,
  external_product_id,
  name,
  brand,
  category,
  subcategory,
  package_size_text,
  unit_quantity,
  unit_type,
  current_price_cents,
  unit_price_cents,
  unit_price_unit,
  product_url,
  is_available
) values
  ('ah', 'seed-ah-melk-1l', 'Zaanse Hoeve Halfvolle melk', 'Zaanse Hoeve', 'Zuivel', 'Melk', '1 l', 1, 'l', 119, 119, 'l', 'https://www.ah.nl/zoeken?query=halfvolle%20melk', true),
  ('jumbo', 'seed-jumbo-melk-1l', 'Jumbo Halfvolle melk', 'Jumbo', 'Zuivel', 'Melk', '1 l', 1, 'l', 125, 125, 'l', 'https://www.jumbo.com/zoeken?searchType=keyword&searchTerms=halfvolle%20melk', true),
  ('dirk', 'seed-dirk-melk-1l', 'Dirk Halfvolle melk', 'Dirk', 'Zuivel', 'Melk', '1 l', 1, 'l', 115, 115, 'l', 'https://www.dirk.nl/zoeken/halfvolle%20melk', true),
  ('plus', 'seed-plus-melk-1l', 'PLUS Halfvolle melk', 'PLUS', 'Zuivel', 'Melk', '1 l', 1, 'l', 129, 129, 'l', 'https://www.plus.nl/zoekresultaten?keyword=halfvolle%20melk', true),
  ('ah', 'seed-ah-bloem-1kg', 'AH Patentbloem', 'AH', 'Bakken', 'Bloem', '1 kg', 1, 'kg', 89, 89, 'kg', 'https://www.ah.nl/zoeken?query=patentbloem', true),
  ('jumbo', 'seed-jumbo-bloem-1kg', 'Jumbo Patentbloem', 'Jumbo', 'Bakken', 'Bloem', '1 kg', 1, 'kg', 95, 95, 'kg', 'https://www.jumbo.com/zoeken?searchType=keyword&searchTerms=patentbloem', true),
  ('dirk', 'seed-dirk-bloem-1kg', 'Koopmans Patentbloem', 'Koopmans', 'Bakken', 'Bloem', '1 kg', 1, 'kg', 129, 129, 'kg', 'https://www.dirk.nl/zoeken/patentbloem', true),
  ('plus', 'seed-plus-bloem-1kg', 'PLUS Patentbloem', 'PLUS', 'Bakken', 'Bloem', '1 kg', 1, 'kg', 99, 99, 'kg', 'https://www.plus.nl/zoekresultaten?keyword=patentbloem', true),
  ('ah', 'seed-ah-eieren-6', 'AH Vrije uitloop eieren maat M', 'AH', 'Zuivel', 'Eieren', '6 stuks', 6, 'piece', 229, 38, 'stuk', 'https://www.ah.nl/zoeken?query=eieren', true),
  ('jumbo', 'seed-jumbo-eieren-6', 'Jumbo Scharreleieren M', 'Jumbo', 'Zuivel', 'Eieren', '6 stuks', 6, 'piece', 219, 37, 'stuk', 'https://www.jumbo.com/zoeken?searchType=keyword&searchTerms=eieren', true),
  ('dirk', 'seed-dirk-eieren-6', 'Dirk Scharreleieren M', 'Dirk', 'Zuivel', 'Eieren', '6 stuks', 6, 'piece', 209, 35, 'stuk', 'https://www.dirk.nl/zoeken/eieren', true),
  ('plus', 'seed-plus-eieren-6', 'PLUS Vrije uitloop eieren M', 'PLUS', 'Zuivel', 'Eieren', '6 stuks', 6, 'piece', 239, 40, 'stuk', 'https://www.plus.nl/zoekresultaten?keyword=eieren', true),
  ('ah', 'seed-ah-suiker-1kg', 'Van Gilse Kristalsuiker', 'Van Gilse', 'Bakken', 'Suiker', '1 kg', 1, 'kg', 159, 159, 'kg', 'https://www.ah.nl/zoeken?query=kristalsuiker', true),
  ('jumbo', 'seed-jumbo-suiker-1kg', 'Jumbo Kristalsuiker', 'Jumbo', 'Bakken', 'Suiker', '1 kg', 1, 'kg', 149, 149, 'kg', 'https://www.jumbo.com/zoeken?searchType=keyword&searchTerms=kristalsuiker', true),
  ('ah', 'seed-ah-boter-250g', 'AH Roomboter ongezouten', 'AH', 'Zuivel', 'Boter', '250 g', 250, 'g', 249, 996, 'kg', 'https://www.ah.nl/zoeken?query=ongezouten%20roomboter', true),
  ('jumbo', 'seed-jumbo-boter-250g', 'Jumbo Roomboter ongezouten', 'Jumbo', 'Zuivel', 'Boter', '250 g', 250, 'g', 239, 956, 'kg', 'https://www.jumbo.com/zoeken?searchType=keyword&searchTerms=ongezouten%20roomboter', true),
  ('ah', 'seed-ah-slagroom-250ml', 'AH Verse slagroom', 'AH', 'Zuivel', 'Room', '250 ml', 250, 'ml', 139, 556, 'l', 'https://www.ah.nl/zoeken?query=slagroom', true),
  ('jumbo', 'seed-jumbo-slagroom-250ml', 'Jumbo Verse slagroom', 'Jumbo', 'Zuivel', 'Room', '250 ml', 250, 'ml', 135, 540, 'l', 'https://www.jumbo.com/zoeken?searchType=keyword&searchTerms=slagroom', true),
  ('ah', 'seed-ah-pasta-500g', 'AH Spaghetti', 'AH', 'Pasta', 'Pasta', '500 g', 500, 'g', 99, 198, 'kg', 'https://www.ah.nl/zoeken?query=spaghetti', true),
  ('jumbo', 'seed-jumbo-pasta-500g', 'Jumbo Spaghetti', 'Jumbo', 'Pasta', 'Pasta', '500 g', 500, 'g', 95, 190, 'kg', 'https://www.jumbo.com/zoeken?searchType=keyword&searchTerms=spaghetti', true),
  ('ah', 'seed-ah-tomaten-500g', 'AH Romaatjes', 'AH', 'Groente', 'Tomaten', '500 g', 500, 'g', 229, 458, 'kg', 'https://www.ah.nl/zoeken?query=tomaten', true),
  ('jumbo', 'seed-jumbo-tomaten-500g', 'Jumbo Snoeptomaten', 'Jumbo', 'Groente', 'Tomaten', '500 g', 500, 'g', 219, 438, 'kg', 'https://www.jumbo.com/zoeken?searchType=keyword&searchTerms=tomaten', true),
  ('ah', 'seed-ah-knoflook', 'AH Knoflook', 'AH', 'Groente', 'Knoflook', '2 stuks', 2, 'piece', 89, 45, 'stuk', 'https://www.ah.nl/zoeken?query=knoflook', true),
  ('jumbo', 'seed-jumbo-knoflook', 'Jumbo Knoflook', 'Jumbo', 'Groente', 'Knoflook', '2 stuks', 2, 'piece', 85, 43, 'stuk', 'https://www.jumbo.com/zoeken?searchType=keyword&searchTerms=knoflook', true),
  ('ah', 'seed-ah-ui-1kg', 'AH Uien', 'AH', 'Groente', 'Ui', '1 kg', 1, 'kg', 149, 149, 'kg', 'https://www.ah.nl/zoeken?query=uien', true),
  ('jumbo', 'seed-jumbo-ui-1kg', 'Jumbo Uien', 'Jumbo', 'Groente', 'Ui', '1 kg', 1, 'kg', 139, 139, 'kg', 'https://www.jumbo.com/zoeken?searchType=keyword&searchTerms=uien', true),
  ('ah', 'seed-ah-olijfolie-500ml', 'AH Olijfolie mild', 'AH', 'Olie', 'Olijfolie', '500 ml', 500, 'ml', 449, 898, 'l', 'https://www.ah.nl/zoeken?query=olijfolie', true),
  ('jumbo', 'seed-jumbo-olijfolie-500ml', 'Jumbo Olijfolie mild', 'Jumbo', 'Olie', 'Olijfolie', '500 ml', 500, 'ml', 429, 858, 'l', 'https://www.jumbo.com/zoeken?searchType=keyword&searchTerms=olijfolie', true)
on conflict (store_id, external_product_id) do update set
  name = excluded.name,
  brand = excluded.brand,
  category = excluded.category,
  subcategory = excluded.subcategory,
  package_size_text = excluded.package_size_text,
  current_price_cents = excluded.current_price_cents,
  unit_price_cents = excluded.unit_price_cents,
  unit_price_unit = excluded.unit_price_unit,
  product_url = excluded.product_url,
  is_available = excluded.is_available,
  updated_at = now();

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
    (
      greatest(similarity(lower(p.name), lower(query_text)), similarity(lower(coalesce(p.subcategory, '')), lower(query_text))) +
      case when lower(p.name) like '%' || lower(query_text) || '%' then 0.4 else 0 end +
      case when store_filter is not null and p.store_id = store_filter then 0.15 else 0 end
    )::numeric as match_score
  from public.products p
  where
    p.is_available = true
    and (store_filter is null or p.store_id = store_filter)
    and (
      similarity(lower(p.name), lower(query_text)) > 0.08
      or similarity(lower(coalesce(p.subcategory, '')), lower(query_text)) > 0.08
      or lower(p.name) like '%' || lower(query_text) || '%'
      or lower(coalesce(p.category, '')) like '%' || lower(query_text) || '%'
    )
  order by match_score desc, p.current_price_cents asc
  limit least(greatest(match_count, 1), 20);
$$;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists profiles_set_updated_at on public.profiles;
create trigger profiles_set_updated_at
before update on public.profiles
for each row execute function public.set_updated_at();

drop trigger if exists recipes_set_updated_at on public.recipes;
create trigger recipes_set_updated_at
before update on public.recipes
for each row execute function public.set_updated_at();

drop trigger if exists shopping_lists_set_updated_at on public.shopping_lists;
create trigger shopping_lists_set_updated_at
before update on public.shopping_lists
for each row execute function public.set_updated_at();

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, email)
  values (new.id, new.email)
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
after insert on auth.users
for each row execute function public.handle_new_user();

alter table public.profiles enable row level security;
alter table public.recipes enable row level security;
alter table public.recipe_ingredients enable row level security;
alter table public.recipe_instructions enable row level security;
alter table public.stores enable row level security;
alter table public.products enable row level security;
alter table public.recipe_product_matches enable row level security;
alter table public.shopping_lists enable row level security;
alter table public.shopping_list_items enable row level security;
alter table public.import_cache enable row level security;
alter table public.ai_usage_logs enable row level security;

create policy "profiles_select_own" on public.profiles for select to authenticated using (auth.uid() = id);
create policy "profiles_update_own" on public.profiles for update to authenticated using (auth.uid() = id) with check (auth.uid() = id);
create policy "profiles_insert_own" on public.profiles for insert to authenticated with check (auth.uid() = id);

create policy "recipes_select_own" on public.recipes for select to authenticated using (auth.uid() = user_id);
create policy "recipes_insert_own" on public.recipes for insert to authenticated with check (auth.uid() = user_id);
create policy "recipes_update_own" on public.recipes for update to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "recipes_delete_own" on public.recipes for delete to authenticated using (auth.uid() = user_id);

create policy "ingredients_select_own_recipe" on public.recipe_ingredients for select to authenticated using (
  exists (select 1 from public.recipes r where r.id = recipe_id and r.user_id = auth.uid())
);
create policy "ingredients_insert_own_recipe" on public.recipe_ingredients for insert to authenticated with check (
  exists (select 1 from public.recipes r where r.id = recipe_id and r.user_id = auth.uid())
);
create policy "ingredients_update_own_recipe" on public.recipe_ingredients for update to authenticated using (
  exists (select 1 from public.recipes r where r.id = recipe_id and r.user_id = auth.uid())
) with check (
  exists (select 1 from public.recipes r where r.id = recipe_id and r.user_id = auth.uid())
);
create policy "ingredients_delete_own_recipe" on public.recipe_ingredients for delete to authenticated using (
  exists (select 1 from public.recipes r where r.id = recipe_id and r.user_id = auth.uid())
);

create policy "instructions_select_own_recipe" on public.recipe_instructions for select to authenticated using (
  exists (select 1 from public.recipes r where r.id = recipe_id and r.user_id = auth.uid())
);
create policy "instructions_insert_own_recipe" on public.recipe_instructions for insert to authenticated with check (
  exists (select 1 from public.recipes r where r.id = recipe_id and r.user_id = auth.uid())
);
create policy "instructions_update_own_recipe" on public.recipe_instructions for update to authenticated using (
  exists (select 1 from public.recipes r where r.id = recipe_id and r.user_id = auth.uid())
) with check (
  exists (select 1 from public.recipes r where r.id = recipe_id and r.user_id = auth.uid())
);
create policy "instructions_delete_own_recipe" on public.recipe_instructions for delete to authenticated using (
  exists (select 1 from public.recipes r where r.id = recipe_id and r.user_id = auth.uid())
);

create policy "stores_select_authenticated" on public.stores for select to authenticated using (true);
create policy "products_select_authenticated" on public.products for select to authenticated using (true);

create policy "matches_select_own_recipe" on public.recipe_product_matches for select to authenticated using (
  exists (
    select 1
    from public.recipe_ingredients ri
    join public.recipes r on r.id = ri.recipe_id
    where ri.id = recipe_ingredient_id and r.user_id = auth.uid()
  )
);
create policy "matches_insert_own_recipe" on public.recipe_product_matches for insert to authenticated with check (
  exists (
    select 1
    from public.recipe_ingredients ri
    join public.recipes r on r.id = ri.recipe_id
    where ri.id = recipe_ingredient_id and r.user_id = auth.uid()
  )
);
create policy "matches_update_own_recipe" on public.recipe_product_matches for update to authenticated using (
  exists (
    select 1
    from public.recipe_ingredients ri
    join public.recipes r on r.id = ri.recipe_id
    where ri.id = recipe_ingredient_id and r.user_id = auth.uid()
  )
) with check (
  exists (
    select 1
    from public.recipe_ingredients ri
    join public.recipes r on r.id = ri.recipe_id
    where ri.id = recipe_ingredient_id and r.user_id = auth.uid()
  )
);

create policy "shopping_lists_select_own" on public.shopping_lists for select to authenticated using (auth.uid() = user_id);
create policy "shopping_lists_insert_own" on public.shopping_lists for insert to authenticated with check (auth.uid() = user_id);
create policy "shopping_lists_update_own" on public.shopping_lists for update to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "shopping_lists_delete_own" on public.shopping_lists for delete to authenticated using (auth.uid() = user_id);

create policy "shopping_items_select_own_list" on public.shopping_list_items for select to authenticated using (
  exists (select 1 from public.shopping_lists sl where sl.id = shopping_list_id and sl.user_id = auth.uid())
);
create policy "shopping_items_insert_own_list" on public.shopping_list_items for insert to authenticated with check (
  exists (select 1 from public.shopping_lists sl where sl.id = shopping_list_id and sl.user_id = auth.uid())
);
create policy "shopping_items_update_own_list" on public.shopping_list_items for update to authenticated using (
  exists (select 1 from public.shopping_lists sl where sl.id = shopping_list_id and sl.user_id = auth.uid())
) with check (
  exists (select 1 from public.shopping_lists sl where sl.id = shopping_list_id and sl.user_id = auth.uid())
);
create policy "shopping_items_delete_own_list" on public.shopping_list_items for delete to authenticated using (
  exists (select 1 from public.shopping_lists sl where sl.id = shopping_list_id and sl.user_id = auth.uid())
);

create policy "import_cache_select_own" on public.import_cache for select to authenticated using (auth.uid() = user_id);
create policy "import_cache_insert_own" on public.import_cache for insert to authenticated with check (auth.uid() = user_id);

create policy "ai_usage_logs_select_own" on public.ai_usage_logs for select to authenticated using (auth.uid() = user_id);
