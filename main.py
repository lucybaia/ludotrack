"""Entrypoint: roda o client do Discord e o bot do Telegram no mesmo event loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import discord

from accounts import AccountService
from config import (
    ConfigError,
    load_app_config,
    load_discord_config,
    load_igdb_config,
    load_supabase_config,
    load_telegram_config,
    setup_logging,
)
from db import Database
from discord_presence import INTENTS_HELP, PresenceWatcher
from igdb_client import IGDBClient
from supabase_sync import SupabaseSync
from telegram_bot import TelegramBot

log = logging.getLogger("main")

HEARTBEAT_INTERVAL = 60  # segundos
# Sessão aberta sem heartbeat há mais que isso = o processo caiu durante o jogo.
STALE_SESSION_AFTER = timedelta(minutes=5)


class GameTracker:
    """Recebe as transições confirmadas do PresenceWatcher e grava as sessões no banco."""

    def __init__(self, db: Database, on_change: Callable[[], None] | None = None) -> None:
        self._db = db
        self._on_change = on_change

    async def on_game_start(self, discord_id: int, game: str, started_at: datetime) -> None:
        self._db.start_session(discord_id, game, started_at)
        self._changed()

    async def on_game_stop(self, discord_id: int, game: str, ended_at: datetime) -> None:
        session = self._db.end_open_session(discord_id, ended_at)
        if session is None:
            log.warning("Fim de %r (user=%s) recebido, mas não havia sessão aberta", game, discord_id)
            return
        if session.game != game:
            log.warning("user=%s: sessão aberta era %r, mas o Discord encerrou %r", discord_id, session.game, game)
        self._changed()

    def _changed(self) -> None:
        if self._on_change:
            self._on_change()


async def heartbeat(db: Database) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        db.touch_open_sessions(datetime.now(timezone.utc))


async def run() -> None:
    discord_cfg = load_discord_config()
    telegram_cfg = load_telegram_config()
    igdb_cfg = load_igdb_config()
    supabase_cfg = load_supabase_config()
    app_cfg = load_app_config()

    db = Database(app_cfg.db_path)

    # Recuperação após crash/desligamento: fecha sessões "esquecidas" no último heartbeat,
    # e sessões abertas de quem não está mais vinculado.
    now = datetime.now(timezone.utc)
    linked = db.linked_discord_ids()
    for session in db.close_stale_sessions(now - STALE_SESSION_AFTER) + db.close_sessions_except(linked):
        log.info("Sessão antiga de %r (user=%s) fechada em %s", session.game, session.discord_id, session.ended_at)
    open_games = {s.discord_id: s.game for s in db.get_open_sessions()}

    if igdb_cfg is None:
        log.warning("IGDB_CLIENT_ID/IGDB_CLIENT_SECRET não definidos: /nowplaying sem capa/metadados")
    igdb = IGDBClient(igdb_cfg.client_id, igdb_cfg.client_secret) if igdb_cfg else None

    if supabase_cfg is None:
        log.info("SUPABASE_URL/SUPABASE_KEY não definidos: sessões ficam só no SQLite local")
        sync = None
    else:
        sync = SupabaseSync(
            db,
            supabase_cfg.url,
            supabase_cfg.key,
            table=supabase_cfg.table,
            interval=supabase_cfg.sync_interval,
        )
    on_change = sync.notify if sync else None

    accounts = AccountService(db, on_change=on_change)
    watcher = PresenceWatcher(
        listener=GameTracker(db, on_change),
        link_handler=accounts.complete_link,
        stop_grace_seconds=app_cfg.debounce_seconds,
        ignored_activities=app_cfg.ignored_activities,
        legacy_guild_id=discord_cfg.legacy_guild_id,
    )
    for discord_id in linked:
        watcher.track(discord_id, current_game=open_games.get(discord_id))
    telegram = TelegramBot(
        telegram_cfg.bot_token, db, accounts, igdb, watcher, discord_invite_url=discord_cfg.invite_url
    )
    accounts.watcher = watcher
    accounts.notify_user = telegram.send_private

    # Docker (Dokploy) para o container com SIGTERM: cancela a tarefa principal para
    # o bloco finally fechar tudo direito. No Windows não há add_signal_handler.
    main_task = asyncio.current_task()
    assert main_task is not None
    with contextlib.suppress(NotImplementedError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main_task.cancel)

    try:
        await telegram.start()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(watcher.start(discord_cfg.bot_token), name="discord")
            tasks.create_task(heartbeat(db), name="heartbeat")
            if sync:
                tasks.create_task(sync.run(), name="supabase-sync")
    except* discord.PrivilegedIntentsRequired:
        log.critical(INTENTS_HELP)
    except* discord.LoginFailure:
        log.critical("DISCORD_BOT_TOKEN inválido.")
    finally:
        log.info("Encerrando...")
        await watcher.close()
        await telegram.stop()
        if igdb:
            await igdb.aclose()
        # Sessões abertas ficam abertas de propósito: ao reiniciar, são retomadas ou
        # fechadas no último heartbeat.
        db.touch_open_sessions(datetime.now(timezone.utc))
        if sync:
            # Última tentativa rápida; o que não for enviado fica pendente para a próxima execução.
            try:
                await asyncio.wait_for(sync.sync_once(), timeout=10)
            except TimeoutError:
                log.warning("Sincronização final com o Supabase excedeu o tempo; fica para a próxima")
            await sync.aclose()
        db.close()


def main() -> None:
    try:
        app_cfg = load_app_config()
    except ConfigError as exc:
        setup_logging()
        log.critical("%s", exc)
        raise SystemExit(1) from exc
    setup_logging(app_cfg.log_level)

    try:
        asyncio.run(run())
    except ConfigError as exc:
        log.critical("%s", exc)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        log.info("Encerrado pelo usuário.")
    except asyncio.CancelledError:
        log.info("Encerrado (SIGTERM).")


if __name__ == "__main__":
    main()
