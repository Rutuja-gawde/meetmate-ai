-- Run once in Supabase: SQL Editor > New query > paste > Run.
-- Only the backend (service-role key) reads/writes these tables. Row level security is
-- ON with no public policies, so the website's anon key can never read them directly.

create table if not exists public.meetings (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  filename    text not null,
  result      jsonb not null,
  created_at  timestamptz not null default now()
);
create index if not exists meetings_user_created on public.meetings (user_id, created_at desc);

-- Secrets (Google refresh token, Notion token, Slack webhook), encrypted by the backend.
create table if not exists public.connections (
  user_id         uuid not null references auth.users(id) on delete cascade,
  provider        text not null check (provider in ('google', 'notion', 'slack')),
  data_encrypted  text not null,
  updated_at      timestamptz not null default now(),
  primary key (user_id, provider)
);

alter table public.meetings    enable row level security;
alter table public.connections enable row level security;
