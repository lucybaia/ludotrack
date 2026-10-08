"""Entrypoint: roda o client do Discord e o bot do Telegram no mesmo event loop."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

from config import (
    ConfigError,
    load_app_config,
    load_discord_config,
    load_igdb_config,
    load_telegram_config,
    setup_logging,
)
from db import Database, Session
from discord_presence import INTENTS_HELP, PresenceWatcher
from igdb_client import IGDBClient
from telegram_bot import TelegramBot

log = logging.getLogger("main")

HEARTBEAT_INTERVAL = 60  # segundos
# Sessão aberta sem heartbeat há mais que isso = o processo caiu durante o jogo.
STALE_SESSION_AFTER = timedelta(minutes=5)


@dataclass(frozen=True)
class GameStarted:
    game: str
    started_at: datetime


@dataclass(frozen=True)
class GameStopped:
    session: Session


class GameTracker:
    """Recebe as transições do PresenceWatcher, grava no banco e enfileira notificações.

    As notificações passam por uma fila com um único consumidor para que a busca na
    IGDB (que pode demorar) não bloqueie o Discord e as mensagens saiam na ordem certa.
    """

    def __init__(self, db: Database, telegram: TelegramBot, igdb: IGDBClient | None) -> None:
        self._db = db
        self._telegram = telegram
        self._igdb = igdb
        self._queue: asyncio.Queue[GameStarted | GameStopped] = asyncio.Queue()

    async def on_game_start(self, game: str, started_at: datetime) -> None:
        self._db.start_session(game, started_at)
        await self._queue.put(GameStarted(game, started_at))

    async def on_game_stop(self, game: str, ended_at: datetime) -> None:
        session = self._db.end_open_session(ended_at)
        if session is None:
            log.warning("Fim de %r recebido, mas não havia sessão aberta no banco", game)
            return
        if session.game != game:
            log.warning("Sessão aberta era %r, mas o Discord encerrou %r", session.game, game)
        await self._queue.put(GameStopped(session))

    async def run_notifier(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                match event:
                    case GameStarted(game, started_at):
                        info = await self._igdb.search_game(game) if self._igdb else None
                        await self._telegram.send_game_started(game, started_at, info)
                    case GameStopped(session):
                        await self._telegram.send_game_stopped(session)
            except Exception:
                log.exception("Erro ao notificar evento %s", event)
            finally:
                self._queue.task_done()


async def heartbeat(db: Database) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        db.touch_open_session(datetime.now(timezone.utc))


async def run() -> None:
    discord_cfg = load_discord_config()
    telegram_cfg = load_telegram_config()
    igdb_cfg = load_igdb_config()
    app_cfg = load_app_config()

    db = Database(app_cfg.db_path)

    # Recuperação após crash/desligamento: fecha sessão "esquecida" no último heartbeat.
    now = datetime.now(timezone.utc)
    if stale := db.close_stale_session(now - STALE_SESSION_AFTER):
        log.info("Sessão antiga de %r fechada no último heartbeat (%s)", stale.game, stale.ended_at)
    open_session = db.get_open_session()
    if open_session:
        log.info("Retomando sessão em andamento: %r", open_session.game)

    if igdb_cfg is None:
        log.warning("IGDB_CLIENT_ID/IGDB_CLIENT_SECRET não definidos: mensagens sem capa/metadados")
    igdb = IGDBClient(igdb_cfg.client_id, igdb_cfg.client_secret) if igdb_cfg else None
    telegram = TelegramBot(telegram_cfg.bot_token, telegram_cfg.chat_id, db)
    tracker = GameTracker(db, telegram, igdb)
    watcher = PresenceWatcher(
        guild_id=discord_cfg.guild_id,
        user_id=discord_cfg.user_id,
        listener=tracker,
        debounce_seconds=app_cfg.debounce_seconds,
        ignored_activities=app_cfg.ignored_activities,
        initial_game=open_session.game if open_session else None,
    )

    try:
        await telegram.start()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(watcher.start(discord_cfg.bot_token), name="discord")
            tasks.create_task(tracker.run_notifier(), name="notifier")
            tasks.create_task(heartbeat(db), name="heartbeat")
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
        # A sessão aberta fica aberta de propósito: ao reiniciar, é retomada ou
        # fechada no último heartbeat.
        db.touch_open_session(datetime.now(timezone.utc))
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


if __name__ == "__main__":
    main()
