"""Bot do Telegram: cadastro (/register) e consultas (/nowplaying, /stats).

Funciona no privado e em qualquer grupo: cada pessoa consulta os próprios jogos,
e a resposta fica visível para quem estiver no chat.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from accounts import CODE_TTL_SECONDS, AccountService
from db import Database, GameStats, User
from discord_presence import LiveGame, PresenceWatcher
from igdb_client import IGDBClient

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

COMMANDS = [
    BotCommand("nowplaying", "Mostra o que você está jogando agora"),
    BotCommand("stats", "Seus jogos mais jogados na semana/mês"),
    BotCommand("register", "Vincular sua conta do Discord"),
    BotCommand("unregister", "Desvincular sua conta"),
    BotCommand("help", "Ajuda"),
]


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}h {minutes:02d}min"
    if minutes:
        return f"{minutes}min"
    return f"{seconds}s"


def _local(dt: datetime) -> datetime:
    return dt.astimezone()  # fuso horário da máquina


def _fmt_time(dt: datetime) -> str:
    return _local(dt).strftime("%H:%M")


def _fmt_datetime(dt: datetime) -> str:
    return _local(dt).strftime("%d/%m %H:%M")


def _ago(dt: datetime, now: datetime) -> str:
    delta = now - dt
    if delta < timedelta(days=2):
        return f"há {format_duration(int(delta.total_seconds()))}"
    return f"em {_fmt_datetime(dt)}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TelegramBot:
    def __init__(
        self,
        token: str,
        db: Database,
        accounts: AccountService,
        igdb: IGDBClient | None,
        watcher: PresenceWatcher,
        *,
        discord_invite_url: str | None = None,
    ) -> None:
        self._db = db
        self._accounts = accounts
        self._igdb = igdb
        self._watcher = watcher
        self._invite_url = discord_invite_url
        self.app = Application.builder().token(token).build()

        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CommandHandler("register", self._cmd_register))
        self.app.add_handler(CommandHandler("unregister", self._cmd_unregister))
        self.app.add_handler(CommandHandler(["nowplaying", "np"], self._cmd_nowplaying))
        self.app.add_handler(CommandHandler("stats", self._cmd_stats))
        self.app.add_error_handler(self._on_error)

    # --- ciclo de vida (sem run_polling: compartilha o event loop com o Discord) ---

    async def start(self) -> None:
        await self.app.initialize()
        try:
            await self.app.bot.set_my_commands(COMMANDS)
        except TelegramError as exc:
            log.warning("Não foi possível registrar o menu de comandos: %s", exc)
        await self.app.start()
        assert self.app.updater is not None
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Bot do Telegram iniciado como @%s", self.app.bot.username)

    async def stop(self) -> None:
        if self.app.updater is not None and self.app.updater.running:
            await self.app.updater.stop()
        if self.app.running:
            await self.app.stop()
        await self.app.shutdown()

    async def send_private(self, telegram_id: int, text: str) -> None:
        try:
            await self.app.bot.send_message(
                telegram_id, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW
            )
        except TelegramError as exc:
            log.error("Falha ao enviar mensagem para telegram=%s: %s", telegram_id, exc)

    # --- cadastro ---------------------------------------------------------------

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Link t.me/<bot>?start=register (enviado quando alguém tenta /register num grupo).
        if context.args and context.args[0] == "register":
            await self._cmd_register(update, context)
        else:
            await self._cmd_help(update, context)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            "🎮 <b>LudoTrack — um Last.fm de jogos</b>\n"
            "Eu acompanho o que você joga pela sua atividade no Discord.\n\n"
            "/register — vincular sua conta do Discord (no privado)\n"
            "/nowplaying ou /np — mostra o que você está jogando\n"
            "/stats — seus jogos mais jogados na semana e no mês\n"
            "/unregister — desvincular sua conta\n\n"
            "Os comandos funcionam aqui e em grupos: me adicione a um grupo para mostrar seus jogos lá.",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_register(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        if user is None:
            return

        if update.effective_chat.type != ChatType.PRIVATE:
            url = f"https://t.me/{context.bot.username}?start=register"
            await message.reply_text(
                "Para se cadastrar, fale comigo no privado 👇",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cadastrar", url=url)]]),
            )
            return

        lines = []
        if existing := self._db.get_user_by_telegram(user.id):
            lines.append(
                f"Você já está vinculado a <b>{escape(existing.discord_name)}</b>. "
                "Usar um novo código troca a conta vinculada.\n"
            )

        code = self._accounts.create_link_code(user.id)
        bot_invite = self._watcher.invite_url
        lines.append(f"🔗 Seu código: <code>{code}</code> (vale {CODE_TTL_SECONDS // 60} min)\n")
        if bot_invite:
            step1 = "Adicione o bot do Discord a um servidor seu (botão <b>Adicionar ao Discord</b> abaixo)"
            if self._invite_url:
                step1 += " ou entre no servidor oficial (botão <b>Entrar no servidor</b>)"
        elif self._invite_url:
            step1 = "Entre no servidor do Discord (botão <b>Entrar no servidor</b> abaixo)"
        else:
            step1 = "Esteja em um servidor do Discord em que o bot esteja"
        steps = [step1, f"Nesse servidor, digite <code>/vincular {code}</code>"]
        lines += [f"{i}. {step}" for i, step in enumerate(steps, start=1)]
        lines.append(
            "\nℹ️ Eu só enxergo seus jogos enquanto você estiver num servidor com o bot e com "
            "<i>Configurações > Privacidade de atividade > Compartilhar atividade</i> ligado."
        )

        buttons = []
        if bot_invite:
            buttons.append([InlineKeyboardButton("➕ Adicionar ao Discord", url=bot_invite)])
        if self._invite_url:
            buttons.append([InlineKeyboardButton("🎮 Entrar no servidor", url=self._invite_url)])
        await message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            link_preview_options=NO_PREVIEW,
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        )

    async def _cmd_unregister(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None:
            return
        if self._accounts.unregister(update.effective_user.id):
            text = "👋 Conta desvinculada. Não acompanho mais seus jogos."
        else:
            text = "Você não tem conta vinculada."
        await update.effective_message.reply_text(text)

    async def _require_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> User | None:
        if update.effective_user is None:
            return None
        user = self._db.get_user_by_telegram(update.effective_user.id)
        if user is None:
            url = f"https://t.me/{context.bot.username}?start=register"
            await update.effective_message.reply_text(
                "Você ainda não vinculou seu Discord.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cadastrar", url=url)]]),
            )
        return user

    # --- consultas --------------------------------------------------------------

    async def _cmd_nowplaying(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._require_user(update, context)
        if user is None:
            return
        message = update.effective_message
        name = escape(update.effective_user.first_name)
        now = _now()

        # A presence ao vivo responde na hora e traz os detalhes do Rich Presence.
        live = self._watcher.live_game(user.discord_id)
        session = self._db.get_open_session(user.discord_id)
        if live is None and session is None:
            text = f"😴 <b>{name}</b> não está jogando nada agora."
            if last := self._db.get_last_finished_session(user.discord_id):
                assert last.ended_at is not None
                text += f"\nÚltimo jogo: <b>{escape(last.game)}</b> ({_ago(last.ended_at, now)})"
            await message.reply_text(text, parse_mode=ParseMode.HTML)
            return

        game = live.name if live else session.game  # type: ignore[union-attr]
        started_at = session.started_at if session and session.game == game else None
        if started_at is None and live:
            started_at = live.started_at

        info = await self._igdb.search_game(game) if self._igdb else None
        lines = [f"🎮 <b>{name}</b> está jogando <b>{escape(game)}</b>"]
        if live:
            lines += self._live_lines(live)
        if started_at:
            elapsed = int((now - started_at).total_seconds())
            lines.append(f"⏱ há {format_duration(elapsed)} (desde {_fmt_time(started_at)})")
        if info:
            meta = []
            if info.release_year:
                meta.append(f"📅 {info.release_year}")
            if info.genres:
                meta.append(f"🏷 {escape(', '.join(info.genres[:3]))}")
            if info.rating:
                meta.append(f"⭐ {info.rating:.0f}/100")
            if meta:
                lines.append(" · ".join(meta))
        week = self._db.totals(user.discord_id, now, since=now - timedelta(days=7), game=game)
        if week.total_seconds:
            lines.append(f"📈 {format_duration(week.total_seconds)} nesse jogo nos últimos 7 dias")
        if info and info.url:
            lines.append(f'<a href="{escape(info.url, quote=True)}">Ver no IGDB</a>')
        text = "\n".join(lines)

        # Capa: arte da IGDB; sem ela, a imagem que o próprio jogo publica no Rich Presence.
        covers = [url for url in (info.cover_url if info else None, live.image_url if live else None) if url]
        for cover in dict.fromkeys(covers):
            try:
                await message.reply_photo(cover, caption=text, parse_mode=ParseMode.HTML)
                return
            except TelegramError as exc:
                log.warning("Falha ao enviar imagem %s (%s)", cover, exc)
        await message.reply_text(text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)

    @staticmethod
    def _live_lines(live: LiveGame) -> list[str]:
        """Detalhes do Rich Presence: modo, mapa, placar/KDA, personagem, elo, grupo...

        Cada jogo decide o que publica; mostramos o que vier, sem repetir textos.
        """
        lines = []
        seen = {live.name.casefold()}
        for icon, value in (
            ("🕹", live.details),
            ("📍", live.state),
            ("🧙", live.large_text),
            ("🏅", live.small_text),
        ):
            if value and value.casefold() not in seen:
                seen.add(value.casefold())
                lines.append(f"{icon} {escape(value)}")
        if live.party and live.party[1]:
            lines.append(f"👥 Grupo {live.party[0]}/{live.party[1]}")
        return lines

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._require_user(update, context)
        if user is None:
            return
        uid = user.discord_id
        now = _now()
        parts = [f"📊 <b>Scrobbles de {escape(update.effective_user.first_name)}</b>"]
        for label, days in (("Últimos 7 dias", 7), ("Últimos 30 dias", 30)):
            since = now - timedelta(days=days)
            parts.append(
                self._period_block(label, self._db.totals(uid, now, since), self._db.top_games(uid, now, since))
            )

        all_time = self._db.totals(uid, now)
        parts.append(
            f"<b>Total geral:</b> {format_duration(all_time.total_seconds)} "
            f"em {all_time.sessions} sessão(ões)"
        )

        footer = []
        if last := self._db.get_last_finished_session(uid):
            footer.append(
                f"<b>Última sessão:</b> {escape(last.game)} — "
                f"{format_duration(last.elapsed_seconds(now))} ({_fmt_datetime(last.started_at)})"
            )
        if current := self._db.get_open_session(uid):
            footer.append(
                f"🎮 <b>Agora:</b> {escape(current.game)} "
                f"(há {format_duration(current.elapsed_seconds(now))})"
            )
        if footer:
            parts.append("\n".join(footer))

        await update.effective_message.reply_text("\n\n".join(parts), parse_mode=ParseMode.HTML)

    @staticmethod
    def _period_block(label: str, total: GameStats, top: list[GameStats]) -> str:
        if not top:
            return f"<b>{label}</b>\nNenhuma sessão registrada."
        lines = [f"<b>{label}</b> — {format_duration(total.total_seconds)} em {total.sessions} sessão(ões)"]
        for i, stats in enumerate(top, start=1):
            lines.append(
                f"{i}. {escape(stats.game)} — {format_duration(stats.total_seconds)} ({stats.sessions}x)"
            )
        return "\n".join(lines)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Erro ao processar update do Telegram", exc_info=context.error)
