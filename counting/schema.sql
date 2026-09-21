-- Analysis records: one row per analyzed clip.
-- Public read, private edit: RLS lets anyone SELECT; only the secret/service key
-- (which bypasses RLS) can write. Apply with:
--   psql "$SUPABASE_DB_URL" -f counting/schema.sql

create table if not exists public.clips (
  name          text primary key,             -- clip filename, e.g. 2026-09-20_13-49-13_<id>.mp4
  recorded_at   timestamptz not null,         -- parsed from the filename; for day/time queries
  moving_count  integer not null default 0,
  total_frames  integer,
  fps           real,
  resolution    jsonb,                        -- [w, h]
  objects       jsonb not null default '[]',  -- [{box, track, frames_visible, travel}]
  updated_at    timestamptz not null default now()
);

create index if not exists clips_recorded_at_idx on public.clips (recorded_at);

alter table public.clips enable row level security;

-- public read
drop policy if exists "public read" on public.clips;
create policy "public read" on public.clips for select using (true);

-- no insert/update/delete policies => writes are denied to anon/authenticated;
-- the secret (service_role) key bypasses RLS and is the only writer.
