"""Caché persistente de `actaUrl` por partido.

Cada run del scraper guarda los `actaUrl` que consiguió extraer del HTML de
`NFG_CmpJornada` (que es la única fuente que trae `CodActa`). Runs futuros
fusionan: si el scrape fresco no consigue extraer el `actaUrl` de un partido
— porque RFEF rate-limita la API y caímos al fallback de PDF, que solo
trae jornada+fecha+enfrentamientos pero NO `CodActa` — recuperamos el que
guardamos en la caché de un run anterior.

Una vez la federación publica el `CodActa` de un partido, queda **permanente
para el resto de la temporada**: la URL del PDF del acta depende solo de ese
código numérico, que la PNFG no cambia.

Clave del caché: `{comp}|{grupo}|J{jornada}|{home_norm}|{away_norm}`.
Incluimos comp/grupo para que dos divisiones con los mismos equipos (raro
pero posible entre regular y copa) no colapsen.

Se commitea al repo en `data/calendar-cache.json` para que el siguiente run
arranque caliente. El scraper hace lookup → si miss, scrape; → si hit, sirve
de la caché.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
CACHE_PATH = DATA_DIR / "calendar-cache.json"

_cache: dict[str, str] | None = None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _key(comp: str, grupo: str, jornada: int, home: str, away: str) -> str:
    return f"{comp}|{grupo}|J{jornada}|{_norm(home)}|{_norm(away)}"


def _ensure_loaded() -> None:
    global _cache
    if _cache is not None:
        return
    if not CACHE_PATH.exists():
        _cache = {}
        return
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        # Filtrar metadatos (_comment) — quedarse solo con entries reales.
        _cache = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, str)}
    except (json.JSONDecodeError, OSError):
        _cache = {}


def lookup(comp: str | int, grupo: str | int, jornada: int, home: str, away: str) -> str | None:
    """Devuelve el `actaUrl` previamente cacheado para este partido, o None."""
    _ensure_loaded()
    assert _cache is not None
    return _cache.get(_key(str(comp), str(grupo), jornada, home, away))


def store(comp: str | int, grupo: str | int, jornada: int, home: str, away: str, acta_url: str) -> None:
    """Guarda el `actaUrl` recién extraído en la caché en memoria. El flush a
    disco se hace al final del run con `save_cache()`."""
    if not acta_url:
        return
    _ensure_loaded()
    assert _cache is not None
    _cache[_key(str(comp), str(grupo), jornada, home, away)] = acta_url


def save_cache() -> None:
    """Persiste la caché a `data/calendar-cache.json`. Llamado al final del
    run desde `scrape.py`."""
    _ensure_loaded()
    payload = {
        "_comment": (
            "Caché persistente de actaUrls por partido auto-generada por "
            "scrapers.calendar_cache. NO editar a mano. Cada run del scraper "
            "fusiona lo fresco con esta caché: una vez la federación publica "
            "el CodActa de un partido queda permanente para el resto de la "
            "temporada aunque RFEF rate-limite en runs posteriores. Si una URL "
            "deja de funcionar, simplemente borra esa entrada y la próxima "
            "ejecución la reresolverá."
        ),
        **{k: v for k, v in (_cache or {}).items()},
    }
    CACHE_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
