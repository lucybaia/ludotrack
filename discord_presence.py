"""Client do Discord que observa a presence dos usuários vinculados e detecta sessões de jogo.

O bot pode estar em vários servidores: qualquer pessoa adiciona o bot ao próprio
servidor (link no /register do Telegram) e passa a ser acompanhada por ele.

Também registra o slash command /vincular, usado para ligar a conta do Discord
ao Telegram com o código gerado pelo /register.

Pode rodar sozinho para testar a detecção (só loga, sem Telegram/IGDB/banco):

    python discord_presence.py <discord_user_id> [<outro_id> ...]
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import discord
from discord import app_commands

log = logging.getLogger(__name__)

INTENTS_HELP = (
    "O Discord recusou os intents privilegiados. Abra https://discord.com/developers/applications, "
    "selecione a aplicação > Bot > Privileged Gateway Intents e habilite "
    "'PRESENCE INTENT' e 'SERVER MEMBERS INTENT'."
)

Activities = Iterable[discord.BaseActivity | discord.Spotify]
# Recebe (código, membro do Discord) e devolve o texto de resposta ao usuário.
LinkHandler = Callable[[str, discord.Member], Awaitable[str]]


class SessionListener(Protocol):
    """Recebe as transições de sessão já confirmadas."""

    async def on_game_start(self, discord_id: int, game: str, started_at: datetime) -> None: ...

    async def on_game_stop(self, discord_id: int, game: str, ended_at: datetime) -> None: ...


class LoggingListener:
    """Listener usado no modo standalone: apenas loga as transições."""

    async def on_game_start(self, discord_id: int, game: str, started_at: datetime) -> None:
        log.info("SESSÃO INICIADA user=%s game=%r started_at=%s", discord_id, game, started_at.isoformat())

    async def on_game_stop(self, discord_id: int, game: str, ended_at: datetime) -> None:
        log.info("SESSÃO ENCERRADA user=%s game=%r ended_at=%s", discord_id, game, ended_at.isoformat())


@dataclass(frozen=True)
class LiveGame:
    """O que o Discord mostra agora sobre o jogo (Rich Presence, quando o jogo publica)."""

    name: str
    details: str | None  # ex.: "Summoner's Rift (Ranqueada)", "Lvl 12 Invoker"
    state: str | None  # ex.: "Em partida", "3/1/5"
    large_text: str | None  # texto da imagem grande (personagem, mapa...)
    small_text: str | None  # texto da imagem pequena (elo, nível...)
    party: tuple[int, int] | None  # (atual, máximo)
    image_url: str | None
    started_at: datetime | None  # início informado pelo próprio jogo


def _playing(activities: Activities, ignored: frozenset[str]) -> list[discord.BaseActivity]:
    return [
        activity
        for activity in activities
        if getattr(activity, "type", None) is discord.ActivityType.playing
        and activity.name
        and activity.name.casefold() not in ignored
    ]


def extract_game(
    activities: Activities,
    *,
    prefer: str | None = None,
    ignored: frozenset[str] = frozenset(),
) -> str | None:
    """Retorna o nome do jogo atual, considerando só activities do tipo `playing`.

    Spotify (listening), status customizado (custom), streaming etc. são descartados.
    Se houver mais de um jogo listado e um deles for o da sessão atual (`prefer`),
    ele é mantido para não gerar trocas falsas.
    """
    games = [activity.name for activity in _playing(activities, ignored)]
    if not games:
        return None
    if prefer in games:
        return prefer
    return games[0]


def _clean(value: object) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def to_live_game(activity: discord.BaseActivity) -> LiveGame:
    party = None
    size = (getattr(activity, "party", None) or {}).get("size")
    if isinstance(size, list) and len(size) == 2 and all(isinstance(n, int) for n in size):
        party = (size[0], size[1])
    image_url = None
    try:
        image_url = getattr(activity, "large_image_url", None)
    except Exception:  # assets malformados não devem derrubar o /nowplaying
        log.debug("large_image_url indisponível para %r", activity.name, exc_info=True)
    return LiveGame(
        name=activity.name or "",
        details=_clean(getattr(activity, "details", None)),
        state=_clean(getattr(activity, "state", None)),
        large_text=_clean(getattr(activity, "large_image_text", None)),
        small_text=_clean(getattr(activity, "small_image_text", None)),
        party=party,
        image_url=image_url,
        started_at=getattr(activity, "start", None),
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _UserState:
    current_game: str | None = None
    # Jogo sumiu da presence: a sessão só fecha se ele não voltar dentro do período de tolerância.
    stop_since: datetime | None = None
    stop_task: asyncio.Task[None] | None = None

    def cancel_stop(self) -> None:
        if self.stop_task is not None:
            self.stop_task.cancel()
        self.stop_task = None
        self.stop_since = None


class PresenceWatcher(discord.Client):
    """Observa a presence dos usuários rastreados em todos os servidores do bot.

    - Abrir ou trocar de jogo vale **na hora**.
    - Fechar o jogo tem tolerância de `stop_grace_seconds`: se ele voltar antes disso
      (oscilação da presence, reconexão), a sessão continua. O horário de fim
      registrado é o de quando o jogo sumiu, não o do fim da espera.
    """

    def __init__(
        self,
        *,
        listener: SessionListener,
        link_handler: LinkHandler | None = None,
        stop_grace_seconds: float = 15.0,
        ignored_activities: frozenset[str] = frozenset(),
        legacy_guild_id: int | None = None,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True  # mantém os membros em cache (privilegiado)
        intents.presences = True  # privilegiado: precisa estar ligado no Developer Portal
        super().__init__(intents=intents)

        self._listener = listener
        self._link_handler = link_handler
        self._grace = stop_grace_seconds
        self._ignored = ignored_activities
        self._legacy_guild_id = legacy_guild_id
        self._users: dict[int, _UserState] = {}
        # Garante que as transições cheguem ao listener na ordem em que aconteceram.
        # (discord_id, jogo anterior, fim do anterior, jogo novo, início do novo)
        self._transitions: asyncio.Queue[tuple[int, str | None, datetime, str | None, datetime]] = (
            asyncio.Queue()
        )
        self._transition_task: asyncio.Task[None] | None = None

        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    @property
    def invite_url(self) -> str | None:
        """Link para adicionar o bot a um servidor (None até o bot conectar)."""
        if self.application_id is None:
            return None
        return discord.utils.oauth_url(
            self.application_id,
            permissions=discord.Permissions.none(),
            scopes=("bot", "applications.commands"),
        )

    # --- usuários rastreados ---------------------------------------------------

    def track(self, discord_id: int, *, current_game: str | None = None) -> None:
        """Começa a acompanhar um usuário. `current_game` = sessão já aberta no banco."""
        if discord_id in self._users:
            return
        self._users[discord_id] = _UserState(current_game=current_game)
        log.info("Acompanhando user=%s (sessão em andamento=%r)", discord_id, current_game)
        if self.is_ready():
            self._sync_member(discord_id, source="track")

    def untrack(self, discord_id: int) -> None:
        state = self._users.pop(discord_id, None)
        if state is not None:
            state.cancel_stop()
            log.info("Deixou de acompanhar user=%s", discord_id)

    def current_game(self, discord_id: int) -> str | None:
        state = self._users.get(discord_id)
        return state.current_game if state else None

    def find_member(self, discord_id: int) -> discord.Member | None:
        """O usuário em qualquer servidor que ele compartilhe com o bot."""
        for guild in self.guilds:
            if (member := guild.get_member(discord_id)) is not None:
                return member
        return None

    def live_game(self, discord_id: int) -> LiveGame | None:
        """Jogo que o Discord mostra agora para o usuário, com os detalhes do Rich Presence."""
        if not self.is_ready() or (member := self.find_member(discord_id)) is None:
            return None
        playing = _playing(member.activities, self._ignored)
        if not playing:
            return None
        current = self.current_game(discord_id)
        activity = next((a for a in playing if a.name == current), playing[0])
        return to_live_game(activity)

    # --- slash command /vincular -------------------------------------------------

    def _register_commands(self) -> None:
        @self.tree.command(name="vincular", description="Vincula sua conta do Discord ao bot do Telegram")
        @app_commands.describe(codigo="Código recebido com /register no Telegram")
        async def vincular(interaction: discord.Interaction, codigo: str) -> None:
            await self._handle_link(interaction, codigo)

    async def setup_hook(self) -> None:
        # Comando global: aparece em todo servidor que adicionar o bot.
        try:
            synced = await self.tree.sync()
            log.info("Slash commands globais registrados: %s", ", ".join(f"/{c.name}" for c in synced))
        except discord.HTTPException as exc:
            log.error("Não foi possível registrar o /vincular (%s)", exc)

        # Versões antigas registravam o /vincular só no servidor de DISCORD_GUILD_ID;
        # remove essa cópia para ele não aparecer duplicado lá.
        if self._legacy_guild_id is not None:
            try:
                await self.tree.sync(guild=discord.Object(id=self._legacy_guild_id))
            except discord.HTTPException as exc:
                log.debug("Limpeza dos comandos antigos do servidor %s falhou: %s", self._legacy_guild_id, exc)

        self._transition_task = asyncio.create_task(self._process_transitions(), name="presence-transitions")

    async def _handle_link(self, interaction: discord.Interaction, code: str) -> None:
        if self._link_handler is None:
            await interaction.response.send_message("Vinculação indisponível no modo de teste.", ephemeral=True)
            return

        member = self.find_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message(
                "Não te encontrei em nenhum servidor em que eu estou. Adicione o bot a um "
                "servidor do qual você participa (link no /register do Telegram) e tente de novo.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            reply = await self._link_handler(code.strip().upper(), member)
        except Exception:
            log.exception("Erro ao vincular user=%s", member.id)
            reply = "❌ Erro interno ao vincular. Tente de novo em instantes."
        await interaction.followup.send(reply, ephemeral=True)

    # --- eventos do gateway ----------------------------------------------------

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Conectado ao Discord como %s (id=%s)", self.user, self.user.id)
        log.info("Link para adicionar o bot a um servidor: %s", self.invite_url)
        if not self.guilds:
            log.warning("O bot ainda não está em nenhum servidor; use o link acima para adicioná-lo")
        log.info("Em %s servidor(es), acompanhando %s usuário(s)", len(self.guilds), len(self._users))
        # Sincroniza o estado de todos (também roda após reconexões completas).
        for discord_id in list(self._users):
            self._sync_member(discord_id, source="ready")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Bot adicionado ao servidor %r (id=%s)", guild.name, guild.id)
        for discord_id in list(self._users):
            if guild.get_member(discord_id) is not None:
                self._sync_member(discord_id, source="guild_join")

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("Bot removido do servidor %r (id=%s)", guild.name, guild.id)
        for discord_id in list(self._users):
            self._sync_member(discord_id, source="guild_remove")

    async def on_member_join(self, member: discord.Member) -> None:
        if member.id in self._users:
            self._observe(member.id, member.activities, source="member_join")

    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.id not in self._users:
            return
        log.debug(
            "presence_update user=%s guild=%s status=%s activities=%s",
            after.id,
            after.guild.id,
            after.status,
            [(a.type.name, a.name) for a in after.activities],
        )
        # Com vários servidores em comum o mesmo update chega repetido; _observe ignora repetições.
        self._observe(after.id, after.activities, source="presence_update")

    async def on_member_remove(self, member: discord.Member) -> None:
        if member.id in self._users:
            log.info("user=%s saiu do servidor %s", member.id, member.guild.id)
            self._sync_member(member.id, source="member_remove")

    async def close(self) -> None:
        for state in self._users.values():
            state.cancel_stop()
        if self._transition_task is not None:
            self._transition_task.cancel()
        await super().close()

    # --- detecção --------------------------------------------------------------

    def _sync_member(self, discord_id: int, *, source: str) -> None:
        member = self.find_member(discord_id)
        if member is None:
            log.warning(
                "user=%s não está em nenhum servidor do bot; não consigo ver os jogos dele", discord_id
            )
            self._observe(discord_id, [], source=source)
            return
        self._observe(discord_id, member.activities, source=source)

    def _observe(self, discord_id: int, activities: Activities, *, source: str) -> None:
        state = self._users.get(discord_id)
        if state is None:
            return
        game = extract_game(activities, prefer=state.current_game, ignored=self._ignored)

        if game == state.current_game:
            if state.stop_task is not None:
                log.info("user=%s: %r voltou antes de %.0fs; sessão mantida", discord_id, game, self._grace)
                state.cancel_stop()
            return

        now = _utcnow()
        if game is None:
            if state.stop_task is None:
                state.stop_since = now
                state.stop_task = asyncio.create_task(
                    self._stop_after_grace(discord_id, state),
                    name=f"presence-stop-{discord_id}",
                )
                log.info(
                    "user=%s: %r sumiu da presence; encerrando em %.0fs se não voltar (source=%s)",
                    discord_id,
                    state.current_game,
                    self._grace,
                    source,
                )
            return

        # Jogo novo: vale na hora. Se o anterior já tinha sumido, ele termina quando sumiu.
        ended_at = state.stop_since or now
        state.cancel_stop()
        previous = state.current_game
        state.current_game = game
        log.info("user=%s: %r -> %r (source=%s)", discord_id, previous, game, source)
        self._transitions.put_nowait((discord_id, previous, ended_at, game, now))

    async def _stop_after_grace(self, discord_id: int, state: _UserState) -> None:
        await asyncio.sleep(self._grace)
        if self._users.get(discord_id) is not state or state.current_game is None:
            return  # usuário desvinculado durante a espera
        assert state.stop_since is not None
        previous, since = state.current_game, state.stop_since
        state.stop_task = None
        state.stop_since = None
        state.current_game = None
        log.info("user=%s: %r encerrado (sumiu em %s)", discord_id, previous, since.isoformat())
        self._transitions.put_nowait((discord_id, previous, since, None, since))

    async def _process_transitions(self) -> None:
        while True:
            discord_id, previous, ended_at, game, started_at = await self._transitions.get()
            try:
                if previous is not None:
                    await self._listener.on_game_stop(discord_id, previous, ended_at)
                if game is not None:
                    await self._listener.on_game_start(discord_id, game, started_at)
            except Exception:
                log.exception("Erro no listener ao processar user=%s %r -> %r", discord_id, previous, game)


async def _run_standalone(user_ids: list[int]) -> None:
    from config import load_app_config, load_discord_config

    discord_cfg = load_discord_config()
    app_cfg = load_app_config()
    watcher = PresenceWatcher(
        listener=LoggingListener(),
        stop_grace_seconds=app_cfg.debounce_seconds,
        ignored_activities=app_cfg.ignored_activities,
    )
    for user_id in user_ids:
        watcher.track(user_id)
    try:
        await watcher.start(discord_cfg.bot_token)
    except discord.PrivilegedIntentsRequired:
        log.critical(INTENTS_HELP)
    except discord.LoginFailure:
        log.critical("DISCORD_BOT_TOKEN inválido.")
    finally:
        await watcher.close()


if __name__ == "__main__":
    import sys

    from config import load_app_config, setup_logging

    if len(sys.argv) < 2 or not all(arg.isdigit() for arg in sys.argv[1:]):
        print("Uso: python discord_presence.py <discord_user_id> [<outro_id> ...]")
        raise SystemExit(2)

    setup_logging(load_app_config().log_level)
    try:
        asyncio.run(_run_standalone([int(arg) for arg in sys.argv[1:]]))
    except KeyboardInterrupt:
        log.info("Encerrado pelo usuário.")
