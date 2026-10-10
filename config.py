"""Carrega configuração a partir de variáveis de ambiente (arquivo .env)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")


class ConfigError(RuntimeError):
    """Variável de ambiente ausente ou inválida."""


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Variável de ambiente obrigatória ausente: {name} (veja .env.example)")
    return value


def _optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} deve ser um número inteiro, recebido: {value!r}") from exc


@dataclass(frozen=True)
class DiscordConfig:
    bot_token: str
    # Opcional, de versões antigas (um servidor só): o /vincular registrado nesse servidor é removido,
    # já que agora o comando é global.
    legacy_guild_id: int | None
    invite_url: str | None  # convite de um servidor "oficial", mostrado no /register


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str


@dataclass(frozen=True)
class IGDBConfig:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    key: str
    table: str
    sync_interval: float


@dataclass(frozen=True)
class AppConfig:
    db_path: Path
    debounce_seconds: float
    ignored_activities: frozenset[str]
    log_level: str


def load_discord_config() -> DiscordConfig:
    return DiscordConfig(
        bot_token=_require("DISCORD_BOT_TOKEN"),
        legacy_guild_id=_optional_int("DISCORD_GUILD_ID"),
        invite_url=os.getenv("DISCORD_INVITE_URL", "").strip() or None,
    )


def load_telegram_config() -> TelegramConfig:
    return TelegramConfig(bot_token=_require("TELEGRAM_BOT_TOKEN"))


def load_igdb_config() -> IGDBConfig | None:
    """IGDB é opcional: retorna None se as credenciais não estiverem definidas."""
    client_id = os.getenv("IGDB_CLIENT_ID", "").strip()
    client_secret = os.getenv("IGDB_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    return IGDBConfig(client_id=client_id, client_secret=client_secret)


def load_supabase_config() -> SupabaseConfig | None:
    """Supabase é opcional: retorna None se URL/chave não estiverem definidas."""
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()
    if not url or not key:
        return None
    raw_interval = os.getenv("SUPABASE_SYNC_INTERVAL", "").strip() or "30"
    try:
        interval = float(raw_interval)
    except ValueError as exc:
        raise ConfigError(f"SUPABASE_SYNC_INTERVAL inválido: {raw_interval!r}") from exc
    return SupabaseConfig(
        url=url,
        key=key,
        table=os.getenv("SUPABASE_TABLE", "").strip() or "game_sessions",
        sync_interval=max(5.0, interval),
    )


def load_app_config() -> AppConfig:
    db_path = Path(os.getenv("DATABASE_PATH", "").strip() or "games.db")
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    raw_debounce = os.getenv("DEBOUNCE_SECONDS", "").strip() or "15"
    try:
        debounce = float(raw_debounce)
    except ValueError as exc:
        raise ConfigError(f"DEBOUNCE_SECONDS inválido: {raw_debounce!r}") from exc

    ignored = frozenset(
        name.strip().casefold()
        for name in os.getenv("IGNORED_ACTIVITIES", "").split(",")
        if name.strip()
    )

    return AppConfig(
        db_path=db_path,
        debounce_seconds=max(0.0, debounce),
        ignored_activities=ignored,
        log_level=(os.getenv("LOG_LEVEL", "").strip() or "INFO").upper(),
    )


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # httpx loga cada request em INFO, incluindo a URL do Telegram que contém o token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
