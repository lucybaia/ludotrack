"""Wrapper mínimo da IGDB API (autenticação via Twitch client_credentials).

Nenhum método público lança exceção por falha de rede/API: erros são logados e
o resultado vem como None, para que o bot siga funcionando sem o enriquecimento.

Teste rápido:

    python igdb_client.py "Hollow Knight"
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_URL = "https://api.igdb.com/v4"
COVER_URL = "https://images.igdb.com/igdb/image/upload/t_cover_big/{image_id}.jpg"

GAME_FIELDS = "name,summary,first_release_date,genres.name,cover.image_id,url,total_rating"


@dataclass(frozen=True)
class GameInfo:
    name: str
    summary: str | None
    genres: tuple[str, ...]
    release_year: int | None
    cover_url: str | None
    url: str | None
    rating: float | None  # 0-100


def _clean_name(name: str) -> str:
    """Remove símbolos de marca e espaços extras que o Discord às vezes inclui."""
    return re.sub(r"\s+", " ", re.sub(r"[™®©]", "", name)).strip()


def _match_key(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "", name.casefold())


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _parse_game(raw: dict[str, Any]) -> GameInfo:
    release_year = None
    if ts := raw.get("first_release_date"):
        release_year = datetime.fromtimestamp(ts, tz=timezone.utc).year

    cover_url = None
    if (cover := raw.get("cover")) and cover.get("image_id"):
        cover_url = COVER_URL.format(image_id=cover["image_id"])

    return GameInfo(
        name=raw.get("name", ""),
        summary=raw.get("summary"),
        genres=tuple(g["name"] for g in raw.get("genres", []) if g.get("name")),
        release_year=release_year,
        cover_url=cover_url,
        url=raw.get("url"),
        rating=raw.get("total_rating"),
    )


class IGDBClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = 10.0,
        cache_ttl: float = 24 * 3600,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.AsyncClient(timeout=timeout)
        self._token: str | None = None
        self._token_expires_at = 0.0  # time.monotonic()
        self._token_lock = asyncio.Lock()
        self._cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, GameInfo | None]] = {}

    async def __aenter__(self) -> IGDBClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get_token(self, *, force_refresh: bool = False) -> str:
        async with self._token_lock:
            # Renova com 60s de folga antes de expirar.
            if not force_refresh and self._token and time.monotonic() < self._token_expires_at - 60:
                return self._token

            log.info("Obtendo token de acesso da Twitch para a IGDB")
            # Credenciais no corpo (não na query string) para não aparecerem em logs de erro.
            response = await self._http.post(
                TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                },
            )
            response.raise_for_status()
            data = response.json()
            self._token = data["access_token"]
            self._token_expires_at = time.monotonic() + float(data.get("expires_in", 3600))
            return self._token

    async def _query(self, endpoint: str, body: str) -> list[dict[str, Any]]:
        for attempt in range(2):
            token = await self._get_token(force_refresh=attempt > 0)
            response = await self._http.post(
                f"{API_URL}/{endpoint}",
                content=body,
                headers={
                    "Client-ID": self._client_id,
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
            if response.status_code == 401 and attempt == 0:
                log.warning("Token da IGDB rejeitado (401); renovando e tentando de novo")
                continue
            response.raise_for_status()
            return response.json()
        return []  # inalcançável: a 2ª tentativa retorna ou lança

    async def search_game(self, name: str) -> GameInfo | None:
        """Busca um jogo pelo nome. Retorna None se não achar ou se a API falhar."""
        cleaned = _clean_name(name)
        if not cleaned:
            return None

        key = _match_key(cleaned)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self._cache_ttl:
            return cached[1]

        body = f'search "{_escape(cleaned)}"; fields {GAME_FIELDS}; limit 10;'
        try:
            results = await self._query("games", body)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.error("Falha ao consultar IGDB para %r: %s: %s", name, type(exc).__name__, exc)
            return None  # não cacheia falhas: tenta de novo na próxima sessão

        if not results:
            log.info("IGDB não encontrou resultados para %r", cleaned)
            info = None
        else:
            # Prefere match exato do nome (ignorando caixa/pontuação); senão, o mais relevante.
            best = next((r for r in results if _match_key(r.get("name", "")) == key), results[0])
            info = _parse_game(best)
            log.info("IGDB: %r -> %r (%s)", cleaned, info.name, info.release_year)

        self._cache[key] = (time.monotonic(), info)
        return info


async def _main(game_name: str) -> None:
    from config import load_igdb_config

    cfg = load_igdb_config()
    if cfg is None:
        log.error("Defina IGDB_CLIENT_ID e IGDB_CLIENT_SECRET no .env")
        return
    async with IGDBClient(cfg.client_id, cfg.client_secret) as client:
        info = await client.search_game(game_name)
        log.info("Resultado: %s", info)


if __name__ == "__main__":
    import sys

    from config import setup_logging

    setup_logging("INFO")
    if len(sys.argv) < 2:
        print('Uso: python igdb_client.py "Nome do Jogo"')
        raise SystemExit(2)
    asyncio.run(_main(" ".join(sys.argv[1:])))
