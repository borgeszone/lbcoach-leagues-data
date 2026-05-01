"""Resuelve URLs de escudos de equipos via Wikipedia.

Usa el endpoint REST de Wikipedia ES para buscar el artículo del club por
nombre y devolver la imagen principal (`originalimage.source`), que para
clubes deportivos suele ser el escudo en su infobox.

Es best-effort: si no se encuentra una imagen razonable, devuelve None.
La app puede mostrar un icono por defecto y el usuario puede subir el
escudo a mano (flujo existente).
"""
from __future__ import annotations

import re
import time
from urllib.parse import quote

import requests

WIKI_API = "https://es.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKI_SEARCH = "https://es.wikipedia.org/w/api.php"

_HEADERS = {
    "User-Agent": "lbcoach-leagues-data/1.0 (https://github.com/laura/goaldash)",
    "Accept": "application/json",
}

# Caché en proceso para no repetir lookups del mismo nombre
_cache: dict[str, str | None] = {}


def resolve_logo_url(team_name: str) -> str | None:
    """Devuelve URL del escudo del equipo o None.

    Estrategia:
    1. Intentar /page/summary directo con el nombre tal cual.
    2. Si falla, hacer una búsqueda con `action=opensearch` y reintentar
       con el primer resultado.
    """
    if not team_name:
        return None

    norm = _norm(team_name)
    if norm in _cache:
        return _cache[norm]

    candidates = [team_name, _strip_suffixes(team_name)]
    seen: set[str] = set()
    for cand in candidates:
        if cand in seen or not cand:
            continue
        seen.add(cand)
        url = _try_summary(cand)
        if url:
            _cache[norm] = url
            return url

    # Fallback: opensearch
    suggested = _opensearch_first_hit(team_name)
    if suggested and suggested not in seen:
        url = _try_summary(suggested)
        if url:
            _cache[norm] = url
            return url

    _cache[norm] = None
    return None


def _try_summary(title: str) -> str | None:
    try:
        r = requests.get(
            WIKI_API.format(title=quote(title, safe="")),
            headers=_HEADERS,
            timeout=10,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    img = data.get("originalimage") or data.get("thumbnail")
    if not img:
        return None
    src = img.get("source")
    if not src:
        return None
    # Filtrar imágenes que claramente no son escudos (fotos, banderas, mapas)
    if any(token in src.lower() for token in ("flag_of", "map_of", "bandera_")):
        return None
    return src


def _opensearch_first_hit(query: str) -> str | None:
    try:
        r = requests.get(
            WIKI_SEARCH,
            params={
                "action": "opensearch",
                "search": query,
                "limit": 3,
                "namespace": 0,
                "format": "json",
            },
            headers=_HEADERS,
            timeout=10,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    # opensearch devuelve [query, [titles], [descs], [urls]]
    if not isinstance(data, list) or len(data) < 2 or not data[1]:
        return None
    # Pequeña pausa para no abusar de la API
    time.sleep(0.2)
    return data[1][0]


def _strip_suffixes(name: str) -> str:
    # Elimina sufijos típicos para mejorar la coincidencia con Wikipedia.
    # "Barça FS" -> "Barça"
    # "ElPozo Murcia Costa Cálida" -> "ElPozo Murcia"
    out = re.sub(r"\s+(FS|FC|F\.S\.|Costa Cálida|Patrimonio.*)$", "", name).strip()
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())
