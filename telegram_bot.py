"""Bot do Telegram: comandos (/nowplaying, /stats) e envio das notificações de sessão."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape

from telegram import BotCommand, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from db import Database, GameStats, Session
from igdb_client import GameInfo

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024  # limite do Telegram para legendas de foto
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

COMMANDS = [
    BotCommand("nowplaying", "O que estou jogando agora"),
    BotCommand("stats", "Resumo de jogos da semana/mês"),
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TelegramBot:
    def __init__(self, token: str, chat_id: int | str, db: Database) -> None:
        self._chat_id = chat_id
        self._db = db
        self.app = Application.builder().token(token).build()

        # Comandos só respondem no chat configurado: as stats são pessoais.
        if isinstance(chat_id, int):
            only_owner = filters.Chat(chat_id=chat_id)
        else:
            only_owner = filters.Chat(username=chat_id.lstrip("@"))

        self.app.add_handler(CommandHandler(["start", "help"], self._cmd_help, filters=only_owner))
        self.app.add_handler(CommandHandler("nowplaying", self._cmd_nowplaying, filters=only_owner))
        self.app.add_handler(CommandHandler("stats", self._cmd_stats, filters=only_owner))
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

    # --- notificações ---------------------------------------------------------

    async def send_game_started(self, game: str, started_at: datetime, info: GameInfo | None) -> None:
        header = f"🎮 <b>Comecei a jogar:</b> {escape(game)}"
        details: list[str] = []
        if info:
            meta = []
            if info.release_year:
                meta.append(f"📅 {info.release_year}")
            if info.genres:
                meta.append(f"🏷 {escape(', '.join(info.genres[:3]))}")
            if info.rating:
                meta.append(f"⭐ {info.rating:.0f}/100")
            if meta:
                details.append(" · ".join(meta))
            if info.url:
                details.append(f'<a href="{escape(info.url, quote=True)}">Ver no IGDB</a>')
        details.append(f"🕒 {_fmt_time(started_at)}")

        summary = info.summary if info else None
        text = self._compose(header, details, summary, limit=CAPTION_LIMIT if info and info.cover_url else 4096)

        if info and info.cover_url:
            try:
                await self.app.bot.send_photo(
                    self._chat_id, photo=info.cover_url, caption=text, parse_mode=ParseMode.HTML
                )
                return
            except TelegramError as exc:
                log.warning("Falha ao enviar capa (%s); enviando só texto", exc)
        await self._send_text(text)

    async def send_game_stopped(self, session: Session) -> None:
        assert session.ended_at is not None and session.duration_seconds is not None
        week = self._db.totals(_now(), since=_now() - timedelta(days=7), game=session.game)
        lines = [
            f"⏹ <b>Parei de jogar:</b> {escape(session.game)}",
            f"⏱ Sessão: <b>{format_duration(session.duration_seconds)}</b> "
            f"({_fmt_time(session.started_at)} → {_fmt_time(session.ended_at)})",
            f"📈 Últimos 7 dias nesse jogo: {format_duration(week.total_seconds)} "
            f"em {week.sessions} sessão(ões)",
        ]
        await self._send_text("\n".join(lines))

    @staticmethod
    def _compose(header: str, details: list[str], summary: str | None, *, limit: int) -> str:
        base = "\n".join([header, *details])
        if not summary:
            return base
        room = limit - len(base) - len("\n\n<i></i>") - 1
        if room < 40:
            return base
        snippet = summary if len(summary) <= room else summary[: room - 1].rstrip() + "…"
        # escape() pode aumentar o tamanho; corta de novo se necessário.
        escaped = escape(snippet)
        while len(escaped) > room and snippet:
            snippet = snippet[:-20].rstrip() + "…"
            escaped = escape(snippet)
        return f"{base}\n\n<i>{escaped}</i>"

    async def _send_text(self, text: str) -> None:
        try:
            await self.app.bot.send_message(
                self._chat_id, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW
            )
        except TelegramError as exc:
            log.error("Falha ao enviar mensagem para o Telegram: %s", exc)

    # --- comandos ---------------------------------------------------------------

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            "🎮 <b>Last.fm de jogos</b>\n"
            "Eu acompanho o que você joga pela presence do Discord.\n\n"
            "/nowplaying — o que está rodando agora\n"
            "/stats — jogos mais jogados na semana e no mês",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_nowplaying(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        session = self._db.get_open_session()
        if session is None:
            text = "😴 Offline / sem jogo no momento."
            last = self._db.get_last_finished_session()
            if last and last.ended_at:
                text += f"\nÚltimo: <b>{escape(last.game)}</b> em {_fmt_datetime(last.ended_at)}"
        else:
            text = (
                f"🎮 Jogando agora: <b>{escape(session.game)}</b>\n"
                f"⏱ Há {format_duration(session.elapsed_seconds(_now()))} "
                f"(desde {_fmt_time(session.started_at)})"
            )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = _now()
        parts = ["📊 <b>Scrobbles de jogos</b>"]
        for label, days in (("Últimos 7 dias", 7), ("Últimos 30 dias", 30)):
            since = now - timedelta(days=days)
            parts.append(self._period_block(label, self._db.totals(now, since), self._db.top_games(now, since)))

        all_time = self._db.totals(now)
        parts.append(
            f"<b>Total geral:</b> {format_duration(all_time.total_seconds)} "
            f"em {all_time.sessions} sessão(ões)"
        )

        footer = []
        if last := self._db.get_last_finished_session():
            assert last.ended_at is not None
            footer.append(
                f"<b>Última sessão:</b> {escape(last.game)} — "
                f"{format_duration(last.elapsed_seconds(now))} ({_fmt_datetime(last.started_at)})"
            )
        if current := self._db.get_open_session():
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
