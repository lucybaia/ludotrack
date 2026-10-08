"""Client do Discord que observa a presence de UM usuário e detecta sessões de jogo.

Pode rodar sozinho para testar a detecção (só loga, sem Telegram/IGDB/banco):

    python discord_presence.py
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Protocol

import discord

log = logging.getLogger(__name__)

INTENTS_HELP = (
    "O Discord recusou os intents privilegiados. Abra https://discord.com/developers/applications, "
    "selecione a aplicação > Bot > Privileged Gateway Intents e habilite "
    "'PRESENCE INTENT' e 'SERVER MEMBERS INTENT'."
)


class SessionListener(Protocol):
    """Recebe as transições de sessão já confirmadas (pós-debounce)."""

    async def on_game_start(self, game: str, started_at: datetime) -> None: ...

    async def on_game_stop(self, game: str, ended_at: datetime) -> None: ...


class LoggingListener:
    """Listener usado no modo standalone: apenas loga as transições."""

    async def on_game_start(self, game: str, started_at: datetime) -> None:
        log.info("SESSÃO INICIADA game=%r started_at=%s", game, started_at.isoformat())

    async def on_game_stop(self, game: str, ended_at: datetime) -> None:
        log.info("SESSÃO ENCERRADA game=%r ended_at=%s", game, ended_at.isoformat())


def extract_game(
    activities: Iterable[discord.BaseActivity | discord.Spotify],
    *,
    prefer: str | None = None,
    ignored: frozenset[str] = frozenset(),
) -> str | None:
    """Retorna o nome do jogo atual, considerando só activities do tipo `playing`.

    Spotify (listening), status customizado (custom), streaming etc. são descartados.
    Se houver mais de um jogo listado e um deles for o da sessão atual (`prefer`),
    ele é mantido para não gerar trocas falsas.
    """
    games = [
        activity.name
        for activity in activities
        if getattr(activity, "type", None) is discord.ActivityType.playing
        and activity.name
        and activity.name.casefold() not in ignored
    ]
    if not games:
        return None
    if prefer in games:
        return prefer
    return games[0]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PresenceWatcher(discord.Client):
    """Observa a presence de `user_id` no servidor `guild_id` e emite início/fim de sessão.

    Debounce: uma mudança só é confirmada se o novo estado se mantiver por
    `debounce_seconds`. Se a presence voltar ao estado confirmado antes disso,
    a mudança é descartada. O horário registrado é o do início da mudança, não
    o da confirmação, então o debounce não "come" tempo das sessões.
    """

    def __init__(
        self,
        *,
        guild_id: int,
        user_id: int,
        listener: SessionListener,
        debounce_seconds: float = 15.0,
        ignored_activities: frozenset[str] = frozenset(),
        initial_game: str | None = None,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True  # garante que o membro fique em cache
        intents.presences = True  # privilegiado: precisa estar ligado no Developer Portal
        super().__init__(intents=intents)

        self._guild_id = guild_id
        self._user_id = user_id
        self._listener = listener
        self._debounce = debounce_seconds
        self._ignored = ignored_activities

        self._current_game: str | None = initial_game
        self._pending_game: str | None = None
        self._pending_task: asyncio.Task[None] | None = None

    @property
    def current_game(self) -> str | None:
        return self._current_game

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Conectado ao Discord como %s (id=%s)", self.user, self.user.id)

        guild = self.get_guild(self._guild_id)
        if guild is None:
            log.error(
                "O bot não está no servidor DISCORD_GUILD_ID=%s. Convide o bot para o servidor "
                "(veja o README).",
                self._guild_id,
            )
            return

        member = guild.get_member(self._user_id)
        if member is None:
            log.error(
                "Usuário DISCORD_USER_ID=%s não encontrado no servidor %r. Você precisa estar no "
                "mesmo servidor que o bot e o 'SERVER MEMBERS INTENT' precisa estar ligado.",
                self._user_id,
                guild.name,
            )
            return

        log.info(
            "Monitorando %s no servidor %r (status=%s, sessão em andamento=%r)",
            member,
            guild.name,
            member.status,
            self._current_game,
        )
        # Sincroniza o estado inicial (também roda após reconexões completas).
        self._observe(member.activities, source="ready")

    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.id != self._user_id or after.guild.id != self._guild_id:
            return
        log.debug(
            "presence_update status=%s activities=%s",
            after.status,
            [(a.type.name, a.name) for a in after.activities],
        )
        self._observe(after.activities, source="presence_update")

    async def close(self) -> None:
        self._cancel_pending()
        await super().close()

    def _observe(self, activities: Iterable[discord.BaseActivity | discord.Spotify], *, source: str) -> None:
        game = extract_game(activities, prefer=self._current_game, ignored=self._ignored)

        if game == self._current_game:
            if self._pending_task is not None:
                log.info(
                    "Mudança para %r durou menos de %.0fs; ignorada (source=%s)",
                    self._pending_game,
                    self._debounce,
                    source,
                )
                self._cancel_pending()
            return

        if self._pending_task is not None and game == self._pending_game:
            return  # update duplicado da mesma mudança; o timer já está correndo

        self._cancel_pending()
        since = _utcnow()
        self._pending_game = game
        self._pending_task = asyncio.create_task(
            self._confirm_after_debounce(game, since), name="presence-debounce"
        )
        log.info(
            "Possível mudança %r -> %r; confirmando em %.0fs (source=%s)",
            self._current_game,
            game,
            self._debounce,
            source,
        )

    def _cancel_pending(self) -> None:
        if self._pending_task is not None:
            self._pending_task.cancel()
        self._pending_task = None
        self._pending_game = None

    async def _confirm_after_debounce(self, game: str | None, since: datetime) -> None:
        await asyncio.sleep(self._debounce)

        self._pending_task = None
        self._pending_game = None
        previous = self._current_game
        self._current_game = game
        log.info("Mudança confirmada %r -> %r (desde %s)", previous, game, since.isoformat())

        try:
            if previous is not None:
                await self._listener.on_game_stop(previous, since)
            if game is not None:
                await self._listener.on_game_start(game, since)
        except Exception:
            log.exception("Erro no listener ao processar %r -> %r", previous, game)


async def _run_standalone() -> None:
    from config import load_app_config, load_discord_config

    discord_cfg = load_discord_config()
    app_cfg = load_app_config()
    watcher = PresenceWatcher(
        guild_id=discord_cfg.guild_id,
        user_id=discord_cfg.user_id,
        listener=LoggingListener(),
        debounce_seconds=app_cfg.debounce_seconds,
        ignored_activities=app_cfg.ignored_activities,
    )
    try:
        await watcher.start(discord_cfg.bot_token)
    except discord.PrivilegedIntentsRequired:
        log.critical(INTENTS_HELP)
    except discord.LoginFailure:
        log.critical("DISCORD_BOT_TOKEN inválido.")
    finally:
        await watcher.close()


if __name__ == "__main__":
    from config import load_app_config, setup_logging

    setup_logging(load_app_config().log_level)
    try:
        asyncio.run(_run_standalone())
    except KeyboardInterrupt:
        log.info("Encerrado pelo usuário.")
