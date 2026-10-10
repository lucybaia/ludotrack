"""Cadastro de usuários: vincula uma conta do Telegram a uma do Discord.

Fluxo: /register no Telegram gera um código temporário; a pessoa usa
/vincular <código> no servidor do Discord. Como só o dono da conta do Discord
consegue rodar o comando por ela, isso prova a posse sem pedir senha ou token.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import TYPE_CHECKING

import discord

from db import Database

if TYPE_CHECKING:
    from discord_presence import PresenceWatcher

log = logging.getLogger(__name__)

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sem 0/O, 1/I
CODE_LENGTH = 6
CODE_TTL_SECONDS = 10 * 60


@dataclass(frozen=True)
class _PendingLink:
    telegram_id: int
    expires_at: float  # time.monotonic()


class AccountService:
    def __init__(self, db: Database, *, on_change: Callable[[], None] | None = None) -> None:
        self._db = db
        self._on_change = on_change
        self._codes: dict[str, _PendingLink] = {}  # em memória: códigos duram só 10 min
        # Ligados depois da construção (dependência circular com o watcher/Telegram).
        self.watcher: PresenceWatcher | None = None
        self.notify_user: Callable[[int, str], Awaitable[None]] | None = None

    def create_link_code(self, telegram_id: int) -> str:
        """Gera um código novo para o usuário, invalidando códigos anteriores dele."""
        now = time.monotonic()
        self._codes = {
            code: pending
            for code, pending in self._codes.items()
            if pending.expires_at > now and pending.telegram_id != telegram_id
        }
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in self._codes:
                break
        self._codes[code] = _PendingLink(telegram_id, now + CODE_TTL_SECONDS)
        log.info("Código de vinculação gerado para telegram=%s", telegram_id)
        return code

    async def complete_link(self, code: str, member: discord.Member) -> str:
        """Chamado pelo /vincular do Discord. Retorna a resposta (efêmera) para o usuário."""
        pending = self._codes.pop(code, None)
        if pending is None or pending.expires_at < time.monotonic():
            return "❌ Código inválido ou expirado. Gere outro com /register no privado do bot do Telegram."

        # Se esse Telegram estava ligado a outra conta do Discord, para de acompanhar a antiga.
        previous = self._db.get_user_by_telegram(pending.telegram_id)
        if previous and previous.discord_id != member.id:
            self._stop_tracking(previous.discord_id)

        # A conta do Discord é a identidade: quem prova a posse dela fica com o vínculo,
        # substituindo um Telegram vinculado antes a ela.
        self._db.link_user(pending.telegram_id, member.id, str(member), datetime.now(timezone.utc))
        if self.watcher is not None:
            open_session = self._db.get_open_session(member.id)
            self.watcher.track(member.id, current_game=open_session.game if open_session else None)

        if self.notify_user is not None:
            await self.notify_user(
                pending.telegram_id,
                f"✅ Discord vinculado: <b>{escape(str(member))}</b>\n"
                "Agora use /nowplaying ou /stats aqui ou em qualquer grupo em que eu estiver.",
            )
        return "✅ Conta vinculada! Volte ao Telegram e use /nowplaying."

    def unregister(self, telegram_id: int) -> bool:
        user = self._db.unlink_user(telegram_id)
        if user is None:
            return False
        self._stop_tracking(user.discord_id)
        return True

    def _stop_tracking(self, discord_id: int) -> None:
        if self.watcher is not None:
            self.watcher.untrack(discord_id)
        if self._db.end_open_session(discord_id, datetime.now(timezone.utc)) and self._on_change:
            self._on_change()
