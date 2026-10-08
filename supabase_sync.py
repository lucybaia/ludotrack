"""Sincroniza as sessões do SQLite local com uma tabela no Supabase (REST/PostgREST).

O SQLite é a fonte da verdade: o bot sempre grava localmente, e este worker envia
em background as linhas alteradas (upsert por uuid). Se o Supabase ou a internet
cair, as linhas ficam pendentes no SQLite e são enviadas quando a conexão voltar.
Nenhuma falha aqui derruba o processo.

A tabela precisa existir antes: rode supabase/schema.sql no SQL Editor do Supabase.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from db import Database, PendingSync

log = logging.getLogger(__name__)

MAX_BACKOFF = 300.0  # segundos


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _payload(row: PendingSync, synced_at: str) -> dict[str, Any]:
    return {
        "id": row.uuid,
        "game": row.game,
        "started_at": _iso(row.started_at),
        "ended_at": _iso(row.ended_at),
        "duration_seconds": row.duration_seconds,
        "last_seen_at": _iso(row.last_seen_at),
        "synced_at": synced_at,
    }


class SupabaseSync:
    def __init__(
        self,
        db: Database,
        url: str,
        key: str,
        *,
        table: str = "game_sessions",
        interval: float = 30.0,
        batch_size: int = 200,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._db = db
        self._endpoint = f"{url.rstrip('/')}/rest/v1/{table}"
        self._interval = interval
        self._batch_size = batch_size
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=15.0)
        self._headers = {
            "apikey": key,
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }
        # Chaves legadas (anon/service_role) são JWTs e também vão no Authorization;
        # as chaves novas (sb_secret_...) vão só no header apikey.
        if key.startswith("eyJ"):
            self._headers["Authorization"] = f"Bearer {key}"
        self._wake = asyncio.Event()

    def notify(self) -> None:
        """Pede uma sincronização imediata (ex.: logo após início/fim de sessão)."""
        self._wake.set()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def run(self) -> None:
        failures = 0
        while True:
            if await self.sync_once():
                failures = 0
                delay = self._interval
            else:
                failures += 1
                delay = min(self._interval * 2**failures, MAX_BACKOFF)
                log.info("Nova tentativa de sincronizar com o Supabase em %.0fs", delay)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass
            self._wake.clear()

    async def sync_once(self) -> bool:
        """Envia todas as linhas pendentes. Retorna False se alguma requisição falhou."""
        sent = 0
        while rows := self._db.pending_sync(self._batch_size):
            synced_at = datetime.now(timezone.utc).isoformat()
            try:
                response = await self._http.post(
                    self._endpoint,
                    params={"on_conflict": "id"},
                    json=[_payload(r, synced_at) for r in rows],
                    headers=self._headers,
                )
            except httpx.HTTPError as exc:
                log.warning("Falha de rede ao sincronizar com o Supabase: %s: %s", type(exc).__name__, exc)
                return False
            if response.is_error:
                log.error(
                    "Supabase recusou a sincronização (HTTP %s): %s",
                    response.status_code,
                    response.text[:500],
                )
                return False
            self._db.mark_synced(rows)
            sent += len(rows)
            if len(rows) < self._batch_size:
                break
        if sent:
            log.info("%s sessão(ões) sincronizada(s) com o Supabase", sent)
        return True
