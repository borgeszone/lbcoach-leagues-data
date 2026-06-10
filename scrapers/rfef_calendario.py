"""Scraper del calendario RFEF (`resultados.rfef.es`, endpoint `NFG_CmpJornada`).

Complementa a `rfef_clasificacion` (que aporta equipos + escudos). Aquí
extraemos, por división/grupo, el **calendario**: para cada jornada, la lista
de enfrentamientos (local, visitante, fecha+hora). Lo consume la app para
autorrellenar el partido al seleccionar una jornada.

Plataforma: misma PNFG que la clasificación, **mismos códigos** de competición
y grupo que ya están en `rfef.py` (`comp`/`grupo`). Diferencias respecto a la
clasificación:
- Endpoint `NFG_CmpJornada` (no `NFG_VisClasificacion`).
- Parámetros **capitalizados**: `CodCompeticion`, `CodGrupo`, `CodJornada`,
  `CodTemporada` (mapea "YYYY-YYYY" a un código numérico vía el `<select>`).

Estructura HTML observada (validada en vivo, Primera FS Femenina 2025-2026):
- `<select name=jornada>` con opciones `"N - DD-MM-YYYY"` → nº total de jornadas.
- Cada partido es un `<tr>` con exactamente un `div.font_widgetL` (local),
  un `div.font_widgetV` (visitante) y un sello `DD-MM-YYYY HH:MM`.

Tolerante: ante rate-limit / cambio de estructura devuelve `[]`. El calendario
es best-effort: si falla, la división se publica sin él y la app cae al
formulario manual.

`NFG_VisCalendario_Vis` (vista de calendario completo) se descartó: es un
shell que carga los datos por AJAX → no es scrapeable de forma estática.
"""
from __future__ import annotations

import re
import time

import requests
from bs4 import BeautifulSoup

from scrapers.rfef_clasificacion import (
    BASE_URL,
    COD_PRIMARIA,
    make_session,
    _norm,
)

CAL_PATH = "/pnfg/NPcd/NFG_CmpJornada"

# "DD-MM-YYYY HH:MM" dentro de la fila del partido.
_DT_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})\s+(\d{2}):(\d{2})")
# Opción del <select name=jornada>: "1 - 06-09-2025".
_JORNADA_OPT_RE = re.compile(r"^\s*(\d+)\s*-\s*\d{2}-\d{2}-\d{4}")

_BACKOFFS = [15, 30, 60, 120]


def resolve_temporada_code(season: str, *, session: requests.Session | None = None) -> str | None:
    """Mapea una temporada "YYYY-YYYY" a su `CodTemporada` numérico leyendo el
    `<select name=temporada>` de la página de calendario. Devuelve None si no
    se encuentra (el caller omitirá `CodTemporada` y el servidor usará la
    temporada vigente por defecto)."""
    s = session or make_session()
    try:
        r = s.get(BASE_URL + CAL_PATH, params={"cod_primaria": COD_PRIMARIA}, timeout=20)
        if not r.content:
            return None
        soup = BeautifulSoup(r.content.decode("iso-8859-15", errors="replace"), "html.parser")
        sel = soup.find("select", attrs={"name": "temporada"})
        if sel is None:
            return None
        for opt in sel.find_all("option"):
            if opt.get_text(strip=True) == season:
                value = (opt.get("value") or "").strip()
                return value or None
    except requests.RequestException as e:
        print(f"  [rfef-cal] No se pudo resolver CodTemporada para {season}: {e}")
    return None


def fetch_division_calendar(
    cod_competicion: str | int,
    cod_grupo: str | int,
    *,
    temporada_code: str | None = None,
    session: requests.Session | None = None,
    retries: int = 4,
    jornada_delay: float = 4.0,
) -> list[dict]:
    """Devuelve `[{"jornada": N, "matches": [{"home","away","date"}]}]`.

    `date` en ISO-8601 (`YYYY-MM-DDTHH:MM:00`) o None si la fila no trae fecha.
    Estrategia: descarga la jornada 1 para leer el `<select>` de jornadas
    (cuántas hay), parsea sus partidos, y luego itera el resto de jornadas con
    una pausa entre cada una. Ante fallo de una jornada concreta, la salta.
    """
    s = session or make_session()
    label = f"{cod_competicion}/{cod_grupo}"

    first_html = _fetch_jornada_html(s, cod_competicion, cod_grupo, 1, temporada_code, retries)
    if first_html is None:
        print(f"  [rfef-cal] {label}: sin respuesta para J1; calendario vacío")
        return []

    jornada_nums = _parse_jornada_numbers(first_html)
    if not jornada_nums:
        # Página sin <select> de jornadas poblado: intentar parsear J1 sola.
        matches = _parse_matches(first_html)
        return [{"jornada": 1, "matches": matches}] if matches else []

    out: list[dict] = []
    for num in jornada_nums:
        if num == 1:
            html = first_html
        else:
            time.sleep(jornada_delay)
            html = _fetch_jornada_html(s, cod_competicion, cod_grupo, num, temporada_code, retries)
            if html is None:
                print(f"  [rfef-cal] {label}: J{num} sin respuesta, saltando")
                continue
        matches = _parse_matches(html)
        if matches:
            out.append({"jornada": num, "matches": matches})

    print(f"  [rfef-cal] {label}: {len(out)} jornadas con partidos")
    return out


def _fetch_jornada_html(
    session: requests.Session,
    cod_competicion: str | int,
    cod_grupo: str | int,
    jornada: int,
    temporada_code: str | None,
    retries: int,
) -> str | None:
    """GET de una jornada con reintentos ante body vacío / error de red
    (mismo patrón que `rfef_clasificacion.fetch_division_teams`). Renueva la
    sesión entre reintentos para forzar una `JSESSIONID` nueva.

    NOTA: muta `session` no es posible (es local del caller), así que ante
    fallo creamos una sesión nueva local y reintentamos con ella."""
    params = {
        "cod_primaria": COD_PRIMARIA,
        "CodCompeticion": str(cod_competicion),
        "CodGrupo": str(cod_grupo),
        "CodJornada": str(jornada),
    }
    if temporada_code:
        params["CodTemporada"] = temporada_code

    url = BASE_URL + CAL_PATH
    s = session
    for attempt in range(retries + 1):
        try:
            r = s.get(url, params=params, timeout=20)
        except requests.RequestException as e:
            if attempt < retries:
                backoff = _BACKOFFS[min(attempt, len(_BACKOFFS) - 1)]
                time.sleep(backoff)
                s = make_session()
                continue
            print(f"  [rfef-cal] J{jornada}: error de red agotados reintentos ({e})")
            return None

        if r.status_code != 200:
            print(f"  [rfef-cal] J{jornada}: HTTP {r.status_code}")
            return None
        if r.content:
            return r.content.decode("iso-8859-15", errors="replace")
        # 200 con body vacío → rate-limit / sesión perdida.
        if attempt < retries:
            backoff = _BACKOFFS[min(attempt, len(_BACKOFFS) - 1)]
            time.sleep(backoff)
            s = make_session()
    return None


def _parse_jornada_numbers(html: str) -> list[int]:
    """Lee el `<select name=jornada>` y devuelve los números de jornada
    ordenados (las opciones son `"N - DD-MM-YYYY"`)."""
    soup = BeautifulSoup(html, "html.parser")
    sel = soup.find("select", attrs={"name": "jornada"})
    if sel is None:
        return []
    nums: set[int] = set()
    for opt in sel.find_all("option"):
        m = _JORNADA_OPT_RE.match(opt.get_text(strip=True))
        if m:
            nums.add(int(m.group(1)))
    return sorted(nums)


def _parse_matches(html: str) -> list[dict]:
    """Extrae los partidos de una jornada. Cada `<tr>` con exactamente un
    `div.font_widgetL` (local) y un `div.font_widgetV` (visitante) es un
    partido; la fecha+hora se lee del propio `<tr>`.

    Deduplica por (local, visitante) normalizados porque un `<tr>` ancestro
    puede envolver al de cada partido y colar duplicados."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for tr in soup.find_all("tr"):
        locals_ = tr.select("div.font_widgetL")
        visitors = tr.select("div.font_widgetV")
        if len(locals_) != 1 or len(visitors) != 1:
            continue
        home = _clean(locals_[0].get_text(strip=True))
        away = _clean(visitors[0].get_text(strip=True))
        if not home or not away:
            continue
        key = (_norm(home), _norm(away))
        if key in seen:
            continue
        seen.add(key)

        date_iso = None
        m = _DT_RE.search(tr.get_text(" ", strip=True))
        if m:
            dd, mm, yyyy, hh, mn = m.groups()
            date_iso = f"{yyyy}-{mm}-{dd}T{hh}:{mn}:00"
        out.append({"home": home, "away": away, "date": date_iso})
    return out


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()
