-- Rode no Supabase: Dashboard > SQL Editor > New query > cole e execute.
-- Pode rodar de novo com segurança: também atualiza tabelas criadas por versões anteriores.

create table if not exists public.game_sessions (
    id               uuid primary key,          -- uuid gerado localmente pelo bot
    discord_user_id  bigint,                    -- dono da sessão
    game             text        not null,
    started_at       timestamptz not null,
    ended_at         timestamptz,               -- null = sessão em andamento
    duration_seconds integer,
    last_seen_at     timestamptz not null,      -- último heartbeat (atualiza ~1x/min durante o jogo)
    synced_at        timestamptz not null default now()
);

-- Versão de um usuário só não tinha esta coluna.
alter table public.game_sessions add column if not exists discord_user_id bigint;

create index if not exists game_sessions_started_at_idx
    on public.game_sessions (started_at desc);
create index if not exists game_sessions_user_started_idx
    on public.game_sessions (discord_user_id, started_at desc);

-- RLS ligado e sem policies: só a chave secreta (usada pelo bot) acessa a tabela.
-- Se quiser ler de um site/dashboard público, crie uma policy de SELECT específica.
alter table public.game_sessions enable row level security;
