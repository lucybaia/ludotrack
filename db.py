"""Persistência em SQLite: usuários vinculados e sessões de jogo.

Timestamps são guardados como unix epoch (segundos, UTC) para facilitar somas
e filtros por período. A API pública trabalha com datetimes timezone-aware.

As operações são rápidas (banco local, poucos registros), então são chamadas
diretamente do event loop, sempre pela mesma thread.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id       INTEGER,           -- dono da sessão (usuário do Discord)
    game             TEXT    NOT NULL,
    started_at       INTEGER NOT NULL,  -- epoch UTC
    ended_at         INTEGER,           -- NULL enquanto a sessão está aberta
    duration_seconds INTEGER,           -- preenchido ao encerrar
    last_seen_at     INTEGER NOT NULL,  -- heartbeat, usado para fechar sessões após crash
    -- sincronização com o Supabase:
    uuid             TEXT,              -- id global da sessão (chave primária no Supabase)
    version          INTEGER NOT NULL DEFAULT 1,  -- incrementado a cada alteração local
    synced_version   INTEGER NOT NULL DEFAULT 0   -- pendente se version > synced_version
);

CREATE TABLE IF NOT EXISTS users (
    telegram_id  INTEGER PRIMARY KEY,
    discord_id   INTEGER NOT NULL UNIQUE,
    discord_name TEXT    NOT NULL,
    linked_at    INTEGER NOT NULL
);
"""

# Colunas que não existiam nas primeiras versões; bancos antigos ganham elas em _migrate().
ADDED_COLUMNS = {
    "discord_id": "INTEGER",
    "uuid": "TEXT",
    "version": "INTEGER NOT NULL DEFAULT 1",
    "synced_version": "INTEGER NOT NULL DEFAULT 0",
}

INDEXES = """
DROP INDEX IF EXISTS idx_sessions_single_open;  -- da versão de um usuário só
CREATE INDEX IF NOT EXISTS idx_sessions_user_started ON sessions(discord_id, started_at);
-- No máximo uma sessão aberta por usuário.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_open_per_user
    ON sessions(discord_id) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_uuid ON sessions(uuid);
"""


@dataclass(frozen=True)
class Session:
    id: int
    discord_id: int | None
    game: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None

    def elapsed_seconds(self, now: datetime) -> int:
        if self.duration_seconds is not None:
            return self.duration_seconds
        return max(0, int((now - self.started_at).total_seconds()))


@dataclass(frozen=True)
class User:
    telegram_id: int
    discord_id: int
    discord_name: str
    linked_at: datetime


@dataclass(frozen=True)
class PendingSync:
    """Linha alterada localmente que ainda não foi enviada ao Supabase."""

    local_id: int
    version: int
    uuid: str
    discord_id: int | None
    game: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None
    last_seen_at: datetime


@dataclass(frozen=True)
class GameStats:
    game: str
    total_seconds: int
    sessions: int


def _to_ts(value: datetime) -> int:
    return int(value.timestamp())


def _from_ts(value: int | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value, tz=timezone.utc)


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        discord_id=row["discord_id"],
        game=row["game"],
        started_at=_from_ts(row["started_at"]),  # type: ignore[arg-type]
        ended_at=_from_ts(row["ended_at"]),
        duration_seconds=row["duration_seconds"],
    )


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        telegram_id=row["telegram_id"],
        discord_id=row["discord_id"],
        discord_name=row["discord_name"],
        linked_at=_from_ts(row["linked_at"]),  # type: ignore[arg-type]
    )


class Database:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        log.info("Banco SQLite aberto em %s", path)

    def _migrate(self) -> None:
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(sessions)")}
        with self._conn:
            for column, definition in ADDED_COLUMNS.items():
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} {definition}")
            missing = self._conn.execute("SELECT id FROM sessions WHERE uuid IS NULL").fetchall()
            self._conn.executemany(
                "UPDATE sessions SET uuid = ? WHERE id = ?",
                [(str(uuid.uuid4()), row["id"]) for row in missing],
            )
        self._conn.executescript(INDEXES)

    def close(self) -> None:
        self._conn.close()

    # --- usuários ------------------------------------------------------------

    def link_user(self, telegram_id: int, discord_id: int, discord_name: str, now: datetime) -> None:
        """Vincula Telegram ↔ Discord. Substitui vínculos anteriores de qualquer um dos dois lados."""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO users (telegram_id, discord_id, discord_name, linked_at) "
                "VALUES (?, ?, ?, ?)",
                (telegram_id, discord_id, discord_name, _to_ts(now)),
            )
        log.info("Usuário vinculado: telegram=%s discord=%s (%s)", telegram_id, discord_id, discord_name)

    def unlink_user(self, telegram_id: int) -> User | None:
        user = self.get_user_by_telegram(telegram_id)
        if user is not None:
            with self._conn:
                self._conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
            log.info("Usuário desvinculado: telegram=%s discord=%s", telegram_id, user.discord_id)
        return user

    def get_user_by_telegram(self, telegram_id: int) -> User | None:
        row = self._conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return _row_to_user(row) if row else None

    def get_user_by_discord(self, discord_id: int) -> User | None:
        row = self._conn.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,)).fetchone()
        return _row_to_user(row) if row else None

    def linked_discord_ids(self) -> set[int]:
        return {row["discord_id"] for row in self._conn.execute("SELECT discord_id FROM users")}

    # --- sessões: escrita ----------------------------------------------------

    def start_session(self, discord_id: int, game: str, started_at: datetime) -> Session:
        """Abre uma sessão nova; se o usuário tiver outra aberta, ela é fechada antes."""
        with self._conn:
            self._close_open(discord_id, started_at)
            ts = _to_ts(started_at)
            cursor = self._conn.execute(
                "INSERT INTO sessions (uuid, discord_id, game, started_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), discord_id, game, ts, ts),
            )
        log.info("Sessão #%s aberta: discord=%s game=%r", cursor.lastrowid, discord_id, game)
        return Session(cursor.lastrowid, discord_id, game, started_at, None, None)  # type: ignore[arg-type]

    def end_open_session(self, discord_id: int, ended_at: datetime) -> Session | None:
        """Fecha a sessão aberta do usuário (se houver) e a retorna já com duração."""
        with self._conn:
            return self._close_open(discord_id, ended_at)

    def touch_open_sessions(self, now: datetime) -> None:
        """Heartbeat: marca que as sessões abertas ainda estavam ativas em `now`."""
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET last_seen_at = ?, version = version + 1 WHERE ended_at IS NULL",
                (_to_ts(now),),
            )

    def close_stale_sessions(self, stale_before: datetime) -> list[Session]:
        """Fecha sessões abertas cujo último heartbeat é anterior a `stale_before`.

        Usado na inicialização: se o processo caiu no meio de uma sessão, ela é
        encerrada no último momento em que sabíamos que a pessoa estava jogando.
        """
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE ended_at IS NULL AND last_seen_at < ?",
            (_to_ts(stale_before),),
        ).fetchall()
        with self._conn:
            return [self._close_row(row, _from_ts(row["last_seen_at"])) for row in rows]  # type: ignore[arg-type]

    def close_sessions_except(self, discord_ids: Collection[int]) -> list[Session]:
        """Fecha (no último heartbeat) sessões abertas de quem não está em `discord_ids`."""
        rows = [
            row
            for row in self._conn.execute("SELECT * FROM sessions WHERE ended_at IS NULL")
            if row["discord_id"] not in discord_ids
        ]
        with self._conn:
            return [self._close_row(row, _from_ts(row["last_seen_at"])) for row in rows]  # type: ignore[arg-type]

    def _close_open(self, discord_id: int, ended_at: datetime) -> Session | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE discord_id = ? AND ended_at IS NULL", (discord_id,)
        ).fetchone()
        return self._close_row(row, ended_at) if row else None

    def _close_row(self, row: sqlite3.Row, ended_at: datetime) -> Session:
        end_ts = max(_to_ts(ended_at), row["started_at"])
        duration = end_ts - row["started_at"]
        self._conn.execute(
            "UPDATE sessions SET ended_at = ?, duration_seconds = ?, last_seen_at = ?, "
            "version = version + 1 WHERE id = ?",
            (end_ts, duration, end_ts, row["id"]),
        )
        log.info(
            "Sessão #%s fechada: discord=%s game=%r (%ss)", row["id"], row["discord_id"], row["game"], duration
        )
        return Session(
            id=row["id"],
            discord_id=row["discord_id"],
            game=row["game"],
            started_at=_from_ts(row["started_at"]),  # type: ignore[arg-type]
            ended_at=_from_ts(end_ts),
            duration_seconds=duration,
        )

    # --- sincronização ------------------------------------------------------

    def pending_sync(self, limit: int = 200) -> list[PendingSync]:
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE version > synced_version ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [
            PendingSync(
                local_id=r["id"],
                version=r["version"],
                uuid=r["uuid"],
                discord_id=r["discord_id"],
                game=r["game"],
                started_at=_from_ts(r["started_at"]),  # type: ignore[arg-type]
                ended_at=_from_ts(r["ended_at"]),
                duration_seconds=r["duration_seconds"],
                last_seen_at=_from_ts(r["last_seen_at"]),  # type: ignore[arg-type]
            )
            for r in rows
        ]

    def mark_synced(self, rows: list[PendingSync]) -> None:
        """Marca como sincronizada a versão enviada. Se a linha mudou durante o envio,
        `version` já é maior e ela continua pendente para o próximo ciclo."""
        with self._conn:
            self._conn.executemany(
                "UPDATE sessions SET synced_version = MAX(synced_version, ?) WHERE id = ?",
                [(r.version, r.local_id) for r in rows],
            )

    # --- sessões: leitura ----------------------------------------------------

    def get_open_session(self, discord_id: int) -> Session | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE discord_id = ? AND ended_at IS NULL", (discord_id,)
        ).fetchone()
        return _row_to_session(row) if row else None

    def get_open_sessions(self) -> list[Session]:
        rows = self._conn.execute("SELECT * FROM sessions WHERE ended_at IS NULL").fetchall()
        return [_row_to_session(r) for r in rows]

    def get_last_finished_session(self, discord_id: int) -> Session | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE discord_id = ? AND ended_at IS NOT NULL "
            "ORDER BY ended_at DESC LIMIT 1",
            (discord_id,),
        ).fetchone()
        return _row_to_session(row) if row else None

    def top_games(
        self, discord_id: int, now: datetime, since: datetime | None = None, limit: int = 5
    ) -> list[GameStats]:
        """Jogos com mais tempo jogado desde `since` (sessão aberta conta até `now`)."""
        rows = self._conn.execute(
            """
            SELECT game,
                   SUM(COALESCE(duration_seconds, MAX(0, :now - started_at))) AS total,
                   COUNT(*) AS sessions
              FROM sessions
             WHERE discord_id = :user AND started_at >= :since
             GROUP BY game
             ORDER BY total DESC
             LIMIT :limit
            """,
            {"user": discord_id, "now": _to_ts(now), "since": _to_ts(since) if since else 0, "limit": limit},
        ).fetchall()
        return [GameStats(r["game"], r["total"], r["sessions"]) for r in rows]

    def totals(
        self, discord_id: int, now: datetime, since: datetime | None = None, game: str | None = None
    ) -> GameStats:
        """Tempo total e nº de sessões desde `since`, opcionalmente filtrando por jogo."""
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE(duration_seconds, MAX(0, :now - started_at))), 0) AS total,
                   COUNT(*) AS sessions
              FROM sessions
             WHERE discord_id = :user AND started_at >= :since AND (:game IS NULL OR game = :game)
            """,
            {"user": discord_id, "now": _to_ts(now), "since": _to_ts(since) if since else 0, "game": game},
        ).fetchone()
        return GameStats(game or "*", row["total"], row["sessions"])
