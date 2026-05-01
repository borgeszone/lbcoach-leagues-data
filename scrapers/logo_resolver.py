"""Resolver multi-fuente de URLs de escudos de equipos.

Cascada de resolución (la primera que tenga éxito gana):

1. **Override curado** (`data/badges-overrides.json`): el maintainer añade
   manualmente entradas cuando una resolución automática es errónea o un club
   importante no aparece en ningún lado. Es la fuente autoritativa.

2. **Caché persistente** (`data/badges-cache.json`): resoluciones exitosas
   de runs anteriores. Se commitea al repo en `main` para que la siguiente
   ejecución arranque caliente. Si una URL cacheada deja de funcionar, se
   purga y se reresuelve.

3. **Wikipedia article images** (`prop=images`): descarga la lista completa
   de imágenes del artículo y filtra por nombre (preferir `logo`, `escut`,
   `escudo`, `crest`, `shield`; descartar `flag_of`, `bandera`, `map_of`,
   fotos JPG/JPEG).

4. **DuckDuckGo image search** (HTML scrape): última red. Sin API key.
   Heurística: preferir dominios `upload.wikimedia.org` o el dominio del
   propio club, preferir PNG/SVG sobre JPG.

5. `None` si nada funciona → la app muestra placeholder genérico.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, quote_plus

import requests

DATA_DIR = Path(__file__).parent.parent / "data"
OVERRIDES_PATH = DATA_DIR / "badges-overrides.json"
CACHE_PATH = DATA_DIR / "badges-cache.json"

_HEADERS = {
    "User-Agent": (
        "lbcoach-leagues-data/1.0 "
        "(https://github.com/borgeszone/lbcoach-leagues-data; "
        "ai-advdev@aggity.com)"
    ),
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
}

# Patrones para puntuar imágenes de Wikipedia
_LOGO_HINTS = ("logo", "escut", "escudo", "crest", "shield", "_fc_", "_cf_", "_fs_")
_LOGO_PENALTIES = (
    "flag_of", "bandera_", "map_of", "mapa_", "stadium_", "estadio_",
    "photo_", "foto_", "_jpg.", "_jpeg.",
)
# Disqualifiers absolutos: si la URL contiene cualquiera de estos, no es escudo.
# Wikipedia mete "Wikidata-logo.svg" en footers de muchos artículos.
# "Fantasy_..." son diseños no-oficiales subidos por usuarios.
# Compañías padre (Telefónica → Movistar Inter) NO sirven como escudo del club.
_LOGO_DISQUALIFIERS = (
    "wikidata", "fantasy", "concept_", "telef%c3%b3nica", "telefonica",
    "global_solutions", "globalsolutions", "wikipedia", "commons-logo",
    "openstreetmap", "wikimedia",
)
_PREFERRED_EXT = (".png", ".svg", ".webp")
# Palabras genéricas a ignorar al hacer matching team-name vs filename
_TEAM_STOPWORDS = {
    "club", "deportivo", "deportiva", "futbol", "fs", "fc", "cf", "ad", "cd",
    "ce", "uds", "ud", "atletico", "atlético", "fútbol", "sala", "equipo",
    "team", "real", "the", "el", "la", "los", "las", "de", "del", "feminino",
    "femenina", "femeni", "masculino", "primera", "segunda", "tercera",
    "agrupacion", "agrupación", "esportiva", "esportiu", "social",
}


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


# ── Persistencia: overrides + caché ─────────────────────────────────────────

_overrides: dict[str, str] | None = None
_cache: dict[str, str | None] | None = None


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _ensure_loaded() -> None:
    global _overrides, _cache
    if _overrides is None:
        _overrides = {k: v for k, v in _load_json(OVERRIDES_PATH).items()
                      if not k.startswith("_")}
    if _cache is None:
        _cache = {k: v for k, v in _load_json(CACHE_PATH).items()
                  if not k.startswith("_")}


def _save_cache() -> None:
    """Persiste la caché. Se llama al terminar el run desde scrape.py."""
    _ensure_loaded()
    payload = {
        "_comment": (
            "Caché auto-generada por scrapers.logo_resolver. NO editar a mano "
            "(usa data/badges-overrides.json para entradas curadas). "
            "Si una URL deja de funcionar, simplemente borra esa entrada y "
            "la próxima ejecución la reresolverá."
        ),
        **{k: v for k, v in (_cache or {}).items()},
    }
    CACHE_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_cache() -> None:
    """Punto público para flush manual desde scrape.py al final del run."""
    _save_cache()


# ── Resolver principal ──────────────────────────────────────────────────────

def resolve_logo_url(team_name: str) -> str | None:
    """Devuelve la URL del escudo del equipo o None si nada funciona."""
    if not team_name or not team_name.strip():
        return None
    _ensure_loaded()
    key = _norm(team_name)

    # 1. Override
    if key in _overrides:
        return _overrides[key] or None

    # 2. Caché
    if key in _cache:
        cached = _cache[key]
        # Permitir que valores explícitos a None se respeten para no spammear
        # APIs en cada run con equipos imposibles. Se purgan re-borrando el
        # cache.json o quitando esa entrada.
        return cached

    # 3. Wikipedia article images
    url = _wikipedia_logo(team_name)
    if url:
        _cache[key] = url
        return url

    # 4. DuckDuckGo image search
    url = _ddg_image_search(team_name)
    if url:
        _cache[key] = url
        return url

    # Marcar como "intentado, sin resultado" para no reintentar cada run
    _cache[key] = None
    return None


# ── 3) Wikipedia images list ────────────────────────────────────────────────

def _wikipedia_logo(team_name: str) -> str | None:
    """Estrategia: opensearch para encontrar el artículo, luego descargar la
    lista de imágenes y elegir la mejor candidata."""
    title = _wiki_search_title(team_name)
    if not title:
        return None

    try:
        r = requests.get(
            "https://es.wikipedia.org/w/api.php",
            params={
                "action": "parse",
                "page": title,
                "prop": "images",
                "format": "json",
                "redirects": 1,
            },
            headers=_HEADERS,
            timeout=15,
        )
        if r.status_code != 200:
            return None
        images = r.json().get("parse", {}).get("images", [])
    except (requests.RequestException, ValueError):
        return None

    if not images:
        return None

    scored = sorted(
        ((_score_wiki_image(img, team_name), img) for img in images),
        key=lambda t: t[0],
        reverse=True,
    )
    best_score, best_image = scored[0]
    # Umbral: requiere al menos un match con el nombre del equipo + algún hint
    # de logo. Sin esto, "Wikidata-logo.svg" o imágenes genéricas se cuelan.
    if best_score < 8:
        return None

    return (
        "https://commons.wikimedia.org/wiki/Special:FilePath/"
        + quote(best_image.replace(" ", "_"), safe="")
    )


def _wiki_search_title(team_name: str) -> str | None:
    try:
        r = requests.get(
            "https://es.wikipedia.org/w/api.php",
            params={
                "action": "opensearch",
                "search": team_name,
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
    if not isinstance(data, list) or len(data) < 2 or not data[1]:
        return None
    time.sleep(0.15)  # rate-limit-friendly
    return data[1][0]


def _score_wiki_image(filename: str, team_name: str) -> int:
    """Score considerando coincidencia con el nombre del equipo.

    Estrategia: el archivo SOLO puntúa positivo si contiene al menos una
    palabra significativa del nombre del equipo (filtrando stopwords).
    De otro modo, ítems genéricos como "Wikidata-logo.svg" o el logo de la
    empresa patrocinadora suben artificialmente."""
    low = filename.lower()

    # Disqualifiers absolutos
    for bad in _LOGO_DISQUALIFIERS:
        if bad in low:
            return -100

    score = 0

    # Match con palabras del nombre del equipo (señal fuerte)
    norm_filename = _norm(filename)
    team_words = [
        w for w in re.findall(r"[a-z]{3,}", _strip_accents(team_name).lower())
        if w not in _TEAM_STOPWORDS
    ]
    matched = sum(1 for w in team_words if w in norm_filename)
    score += matched * 6

    # Hints de logo
    for hint in _LOGO_HINTS:
        if hint in low:
            score += 2
            break

    # Penalties
    for penalty in _LOGO_PENALTIES:
        if penalty in low:
            score -= 10
            break

    # Formato preferido
    for ext in _PREFERRED_EXT:
        if low.endswith(ext):
            score += 2
            break

    return score


def _strip_accents(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


# ── 4) DuckDuckGo image search ──────────────────────────────────────────────

def _ddg_image_search(team_name: str) -> str | None:
    """DDG image search via su endpoint i.js (no oficial pero estable)."""
    try:
        # Primer paso: obtener el token vqd que requiere DDG para llamadas i.js
        q = f"{team_name} escudo logo"
        r = requests.get(
            "https://duckduckgo.com/",
            params={"q": q, "iax": "images", "ia": "images"},
            headers=_HEADERS,
            timeout=10,
        )
        if r.status_code != 200:
            return None
        m = re.search(r"vqd=([\d-]+)", r.text)
        if not m:
            return None
        vqd = m.group(1)

        time.sleep(0.2)
        r2 = requests.get(
            "https://duckduckgo.com/i.js",
            params={
                "q": q,
                "vqd": vqd,
                "o": "json",
                "p": "1",
                "f": ",,,type:photo",
            },
            headers={**_HEADERS, "Referer": "https://duckduckgo.com/"},
            timeout=10,
        )
        if r2.status_code != 200:
            return None
        results = r2.json().get("results", [])
    except (requests.RequestException, ValueError):
        return None

    # Heurística: priorizar wikimedia, dominio del club, PNG/SVG sobre JPG
    best = None
    best_score = -999
    for item in results[:15]:
        url = item.get("image") or item.get("thumbnail")
        if not url:
            continue
        score = _score_ddg_url(url, item.get("source", ""))
        if score > best_score:
            best_score = score
            best = url
    return best if best_score > 0 else None


def _score_ddg_url(url: str, source: str) -> int:
    score = 0
    low = url.lower()
    if "upload.wikimedia.org" in low:
        score += 10
    if any(low.endswith(ext) for ext in _PREFERRED_EXT):
        score += 5
    if any(p in low for p in _LOGO_PENALTIES):
        score -= 8
    if any(h in low for h in _LOGO_HINTS):
        score += 3
    # JPG penaliza levemente (suele ser foto, no logo)
    if low.endswith(".jpg") or low.endswith(".jpeg"):
        score -= 2
    return score
