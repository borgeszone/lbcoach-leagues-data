"""Scrapea el portal `futsal.rfef.es` para mapear cada club al ID con el
que se construye la URL de su escudo oficial.

URL del escudo: `https://futsal.rfef.es/media/lnfs/shields_futsal/png/{ID}.png`

El portal lista clubes en varias páginas (home, clasificaciones por categoría).
Cada `<a href="/equipo/{slug}/{ID}/info">` apunta a la página del club y
contiene un `<img src=".../shields_futsal/png/{ID}.png" alt="{Nombre}">`.

Esta función devuelve un dict `{nombre_normalizado: URL_escudo}` que el
orchestrator (`scrape.py`) inyecta en la caché del logo resolver para que
los clubes RFEF reciban escudos automáticamente.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urljoin

import requests

BASE = "https://futsal.rfef.es"
SHIELD_URL = BASE + "/media/lnfs/shields_futsal/png/{id}.png"

# Páginas que listan clubes (cada una añade IDs nuevos al dict)
PAGES_TO_SCAN = [
    "/",
    "/clasificacion/primera",
    "/clasificacion/segunda",
    "/clasificacion/segundab",
    "/clasificacion/primera-femenina",
    "/clasificacion/segunda-femenina",
]

# Captura el bloque <a href=".../equipo/SLUG/ID/info"> ... <img ... alt="NAME">
_TEAM_RE = re.compile(
    r'href="https?://futsal\.rfef\.es/equipo/[^/]+/(\d+)/info"'
    r'.*?alt="([^"]+)"',
    re.DOTALL,
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def fetch_shield_map() -> dict[str, str]:
    """Devuelve `{nombre_normalizado: url_escudo}` para todos los clubes
    descubiertos en futsal.rfef.es. Errores de red se ignoran (mejor un
    mapa parcial que ninguno)."""
    out: dict[str, str] = {}

    for path in PAGES_TO_SCAN:
        url = urljoin(BASE, path)
        try:
            r = requests.get(url, headers=_HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            html = r.text
        except requests.RequestException:
            continue

        for m in _TEAM_RE.finditer(html):
            shield_id = m.group(1)
            name = m.group(2).strip()
            if not name:
                continue
            key = _norm(name)
            # No sobreescribir si ya existe (la home suele tener nombres
            # canónicos más limpios que las clasificaciones).
            if key not in out:
                out[key] = SHIELD_URL.format(id=shield_id)

    return out
