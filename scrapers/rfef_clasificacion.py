"""Scraper de la clasificación pública de RFEF (`resultados.rfef.es`).

Esta es la fuente *primaria* de equipos + escudos para divisiones RFEF de
fútbol sala. Cada división expone una página de clasificación con la tabla
de equipos del grupo, donde cada fila contiene:

    <td><img src="https://rfef.filesnovanet.es/..." class=escudo_widget></td>
    <td><a> Nombre del Equipo </a></td>

Esto reemplaza la cascada anterior (PDF para nombres + Wikipedia/DDG para
escudos) que sufría de cobertura ~7% para escudos y nombres con artefactos
del parseo PDF. Aquí ambos vienen de la misma fila → 100% coverage y nombres
oficiales canónicos.

Notas operativas:
- El servidor exige una `JSESSIONID` por sesión. La primera petición a
  `resultados.rfef.es/` la siembra (vía 302 a marcadores.rfef.es).
- Subjetivo a rate limiting si se golpea muchas veces seguidas. El scraper
  usa pausas de 1.5s entre divisiones y reintenta una vez tras 5s ante
  respuestas vacías (200 con Content-Length: 0).
- El parser es tolerante: si la sesión expira/cambia o la estructura cambia,
  devuelve lista vacía y deja que `rfef.py` caiga al fallback.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://resultados.rfef.es"
CLAS_PATH = "/pnfg/NPcd/NFG_VisClasificacion"
COD_PRIMARIA = "1000120"  # Estable para todas las competiciones FS

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": f"{BASE_URL}/",
}


@dataclass(frozen=True)
class ScrapedTeam:
    name: str
    logo_url: str | None


def make_session() -> requests.Session:
    """Crea una `Session` con cookies + headers preparados. Hace un GET inicial
    a la home para sembrar la `JSESSIONID` que el endpoint de clasificación
    requiere para devolver contenido."""
    s = requests.Session()
    s.headers.update(_HEADERS)
    try:
        s.get(BASE_URL + "/", timeout=15, allow_redirects=True)
    except requests.RequestException as e:
        print(f"  [rfef-clas] No se pudo iniciar sesión: {e}")
    return s


def fetch_division_teams(
    cod_competicion: str | int,
    cod_grupo: str | int,
    *,
    session: requests.Session | None = None,
    retries: int = 1,
) -> list[ScrapedTeam]:
    """Descarga la clasificación y devuelve los equipos de la división/grupo.

    Devuelve `[]` ante:
    - rate-limit (200 con body vacío y sin reintento exitoso),
    - errores de red,
    - HTML sin la tabla esperada (estructura cambió → fallback).
    """
    s = session or make_session()
    params = {
        "cod_primaria": COD_PRIMARIA,
        "codcompeticion": str(cod_competicion),
        "codgrupo": str(cod_grupo),
    }
    url = BASE_URL + CLAS_PATH

    for attempt in range(retries + 1):
        try:
            r = s.get(url, params=params, timeout=20)
        except requests.RequestException as e:
            print(f"  [rfef-clas] Error de red en {cod_competicion}/{cod_grupo}: {e}")
            return []
        if r.status_code != 200:
            print(f"  [rfef-clas] HTTP {r.status_code} en {cod_competicion}/{cod_grupo}")
            return []
        if r.content:
            teams = _parse(r.content)
            if teams:
                return teams
            # 200 con cuerpo pero sin tabla: estructura cambió o página de error
            print(
                f"  [rfef-clas] {cod_competicion}/{cod_grupo}: respuesta sin "
                f"tabla de clasificación ({len(r.content)} bytes)"
            )
            return []
        # 200 con body vacío → típico de rate-limit / sesión perdida.
        if attempt < retries:
            print(
                f"  [rfef-clas] {cod_competicion}/{cod_grupo}: respuesta vacía, "
                f"reintentando en 5s ({attempt + 1}/{retries})"
            )
            time.sleep(5)

    print(
        f"  [rfef-clas] {cod_competicion}/{cod_grupo}: respuesta vacía tras "
        f"{retries + 1} intentos"
    )
    return []


def _parse(html_bytes: bytes) -> list[ScrapedTeam]:
    """Extrae `(nombre, logo_url)` de cada fila de la tabla de clasificación.

    Estrategia: buscar todas las `<img class=escudo_widget>`, subir al `<tr>`
    y leer el `<a>` de la fila como nombre del equipo. Deduplica por nombre
    normalizado por si la página repite la imagen en tooltips/footer.
    """
    soup = BeautifulSoup(html_bytes, "lxml", from_encoding="iso-8859-15")
    seen_keys: set[str] = set()
    teams: list[ScrapedTeam] = []
    for img in soup.select("img.escudo_widget"):
        tr = img.find_parent("tr")
        if tr is None:
            continue
        a = tr.find("a")
        if a is None:
            continue
        name = a.get_text(strip=True)
        if not name:
            continue
        key = _norm(name)
        if not key or key in seen_keys:
            continue
        src = (img.get("src") or "").strip()
        logo_url: str | None = src if src.startswith(("http://", "https://")) else None
        seen_keys.add(key)
        teams.append(ScrapedTeam(name=name, logo_url=logo_url))
    return teams


def _norm(s: str) -> str:
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def fetch_all(
    items: Iterable[tuple[str | int, str | int]],
    *,
    delay_seconds: float = 1.5,
) -> dict[tuple[str, str], list[ScrapedTeam]]:
    """Helper para scrapear varias `(cod_competicion, cod_grupo)` con la misma
    sesión y un sleep entre llamadas. Devuelve dict por par de IDs."""
    s = make_session()
    out: dict[tuple[str, str], list[ScrapedTeam]] = {}
    for i, (comp, grp) in enumerate(items):
        if i > 0:
            time.sleep(delay_seconds)
        teams = fetch_division_teams(comp, grp, session=s)
        out[(str(comp), str(grp))] = teams
        print(f"  [rfef-clas] {comp}/{grp}: {len(teams)} equipos")
    return out
