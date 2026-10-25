-- Первичная настройка проекта Supabase для реально-временной записи
-- сообщений и медиа Telegram. Выполните этот SQL ОДИН РАЗ в SQL-редакторе:
-- Dashboard → SQL Editor → New query → Run. Повторное выполнение безопасно
-- (все операторы идемпотентны).

-- ---------------------------------------------------------------------------
-- 1. Таблица сообщений
-- ---------------------------------------------------------------------------
create table if not exists public.messages (
  id          bigint generated always as identity primary key,
  created_at  timestamptz not null default now(),
  channel     text not null default 'Личка',
  sender      text not null default '',
  username    text not null default '',
  chat_id     text not null default '',
  message     text not null default '',
  reply       text not null default '',
  media_url   text not null default '',
  media_type  text not null default ''
);

-- Если таблица уже существовала без колонки media_type — добавляем её.
alter table public.messages add column if not exists media_type text not null default '';

-- ---------------------------------------------------------------------------
-- 2. Таблица журнала (аудит операций)
-- ---------------------------------------------------------------------------
create table if not exists public.journal (
  id          bigint generated always as identity primary key,
  created_at  timestamptz not null default now(),
  event       text not null default '',
  status      text not null default '',
  detail      text not null default '',
  channel     text not null default '',
  chat_id     text not null default ''
);

-- ---------------------------------------------------------------------------
-- 3. Row Level Security: разрешаем запись/чтение для anon и authenticated
--    (работает и с anon-ключом, и с service_role). Для service_role RLS
--    обходится автоматически.
-- ---------------------------------------------------------------------------
alter table public.messages enable row level security;
alter table public.journal enable row level security;

drop policy if exists "allow insert for all" on public.messages;
create policy "allow insert for all" on public.messages
  for insert to anon, authenticated
  with check (true);

drop policy if exists "allow read for all" on public.messages;
create policy "allow read for all" on public.messages
  for select to anon, authenticated
  using (true);

drop policy if exists "allow insert for all" on public.journal;
create policy "allow insert for all" on public.journal
  for insert to anon, authenticated
  with check (true);

drop policy if exists "allow read for all" on public.journal;
create policy "allow read for all" on public.journal
  for select to anon, authenticated
  using (true);

-- ---------------------------------------------------------------------------
-- 4. Storage: ковш для медиа + политики (готов под anon-ключ, service_role
--    работает в любом случае).
-- ---------------------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('messages', 'messages', true)
on conflict (id) do nothing;

drop policy if exists "allow insert media" on storage.objects;
create policy "allow insert media" on storage.objects
  for insert to anon, authenticated
  with check (bucket_id = 'messages');

drop policy if exists "allow update media" on storage.objects;
create policy "allow update media" on storage.objects
  for update to anon, authenticated
  using (bucket_id = 'messages')
  with check (bucket_id = 'messages');

drop policy if exists "allow read media" on storage.objects;
create policy "allow read media" on storage.objects
  for select to anon, authenticated
  using (bucket_id = 'messages');
