-- Analysis records: one row per analyzed clip, stored in Supabase.
-- Public read, private edit -- enforced by table GRANTs (no RLS): anon can only
-- SELECT; the secret/service_role key is the only writer.

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

grant select on public.clips to anon, authenticated;
grant select, insert, update, delete on public.clips to service_role;
