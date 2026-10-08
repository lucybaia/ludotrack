"""Persistência em SQLite das sessões de jogo.

Timestamps são guardados como unix epoch (segundos, UTC) para facilitar somas
e filtros por período. A API pública trabalha com datetimes timezone-aware.

As operações são rápidas (banco local, poucos registros), então são chamadas
diretamente do event loop, sempre pela mesma thread.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    game             TEXT    NOT NULL,
    started_at       INTEGER NOT NULL,  -- epoch UTC
    ended_at         INTEGER,           -- NULL enquanto a sessão está aberta
    duration_seconds INTEGER,           -- preenchido ao encerrar
    last_seen_at     INTEGER NOT NULL   -- heartbeat, usado para fechar sessões após crash
);
CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at);
-- No máximo uma sessão aberta por vez.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_single_open
    ON sessions((ended_at IS NULL)) WHERE ended_at IS NULL;
"""

# Colunas de sincronização com o Supabase, adicionadas também em bancos já existentes.
#   uuid:           id global da sessão (chave primária no Supabase)
#   version:        incrementado a cada alteração local da linha
#   synced_version: última versão confirmada pelo Supabase (pendente se version > synced_version)
SYNC_COLUMNS = {
    "uuid": "TEXT",
    "version": "INTEGER NOT NULL DEFAULT 1",
    "synced_version": "INTEGER NOT NULL DEFAULT 0",
}


@dataclass(frozen=True)
class Session:
    id: int
    game: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None

    def elapsed_seconds(self, now: datetime) -> int:
        if self.duration_seconds is not None:
            return self.duration_seconds
        return max(0, int((now - self.started_at).total_seconds()))


@dataclass(frozen=True)
class PendingSync:
    """Linha alterada localmente que ainda não foi enviada ao Supabase."""

    local_id: int
    version: int
    uuid: str
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
        game=row["game"],
        started_at=_from_ts(row["started_at"]),  # type: ignore[arg-type]
        ended_at=_from_ts(row["ended_at"]),
        duration_seconds=row["duration_seconds"],
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
            for column, definition in SYNC_COLUMNS.items():
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} {definition}")
            missing = self._conn.execute("SELECT id FROM sessions WHERE uuid IS NULL").fetchall()
            self._conn.executemany(
                "UPDATE sessions SET uuid = ? WHERE id = ?",
                [(str(uuid.uuid4()), row["id"]) for row in missing],
            )
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_uuid ON sessions(uuid)")

    def close(self) -> None:
        self._conn.close()

    # --- escrita -----------------------------------------------------------

    def start_session(self, game: str, started_at: datetime) -> Session:
        """Abre uma sessão nova; se houver outra aberta, ela é fechada antes."""
        with self._conn:
            self._close_open(started_at)
            ts = _to_ts(started_at)
            cursor = self._conn.execute(
                "INSERT INTO sessions (uuid, game, started_at, last_seen_at) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), game, ts, ts),
            )
        log.info("Sessão #%s aberta: %r", cursor.lastrowid, game)
        return Session(cursor.lastrowid, game, started_at, None, None)  # type: ignore[arg-type]

    def end_open_session(self, ended_at: datetime) -> Session | None:
        """Fecha a sessão aberta (se houver) e a retorna já com duração."""
        with self._conn:
            return self._close_open(ended_at)

    def touch_open_session(self, now: datetime) -> None:
        """Heartbeat: marca que a sessão aberta ainda estava ativa em `now`."""
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET last_seen_at = ?, version = version + 1 WHERE ended_at IS NULL",
                (_to_ts(now),),
            )

    def close_stale_session(self, stale_before: datetime) -> Session | None:
        """Fecha a sessão aberta cujo último heartbeat é anterior a `stale_before`.

        Usado na inicialização: se o processo caiu no meio de uma sessão, ela é
        encerrada no último momento em que sabíamos que você estava jogando.
        """
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE ended_at IS NULL AND last_seen_at < ?",
            (_to_ts(stale_before),),
        ).fetchone()
        if row is None:
            return None
        with self._conn:
            return self._close_open(_from_ts(row["last_seen_at"]))  # type: ignore[arg-type]

    def _close_open(self, ended_at: datetime) -> Session | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE ended_at IS NULL").fetchone()
        if row is None:
            return None
        end_ts = max(_to_ts(ended_at), row["started_at"])
        duration = end_ts - row["started_at"]
        self._conn.execute(
            "UPDATE sessions SET ended_at = ?, duration_seconds = ?, last_seen_at = ?, "
            "version = version + 1 WHERE id = ?",
            (end_ts, duration, end_ts, row["id"]),
        )
        log.info("Sessão #%s fechada: %r (%ss)", row["id"], row["game"], duration)
        return Session(
            id=row["id"],
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

    # --- leitura -----------------------------------------------------------

    def get_open_session(self) -> Session | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE ended_at IS NULL").fetchone()
        return _row_to_session(row) if row else None

    def get_last_finished_session(self) -> Session | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1"
        ).fetchone()
        return _row_to_session(row) if row else None

    def top_games(self, now: datetime, since: datetime | None = None, limit: int = 5) -> list[GameStats]:
        """Jogos com mais tempo jogado desde `since` (sessão aberta conta até `now`)."""
        rows = self._conn.execute(
            """
            SELECT game,
                   SUM(COALESCE(duration_seconds, MAX(0, :now - started_at))) AS total,
                   COUNT(*) AS sessions
              FROM sessions
             WHERE started_at >= :since
             GROUP BY game
             ORDER BY total DESC
             LIMIT :limit
            """,
            {"now": _to_ts(now), "since": _to_ts(since) if since else 0, "limit": limit},
        ).fetchall()
        return [GameStats(r["game"], r["total"], r["sessions"]) for r in rows]

    def totals(self, now: datetime, since: datetime | None = None, game: str | None = None) -> GameStats:
        """Tempo total e nº de sessões desde `since`, opcionalmente filtrando por jogo."""
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE(duration_seconds, MAX(0, :now - started_at))), 0) AS total,
                   COUNT(*) AS sessions
              FROM sessions
             WHERE started_at >= :since AND (:game IS NULL OR game = :game)
            """,
            {"now": _to_ts(now), "since": _to_ts(since) if since else 0, "game": game},
        ).fetchone()
        return GameStats(game or "*", row["total"], row["sessions"])
