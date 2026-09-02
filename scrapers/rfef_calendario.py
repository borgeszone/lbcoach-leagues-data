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
from scrapers.rfef_discovery import resolve_season_code

CAL_PATH = "/pnfg/NPcd/NFG_CmpJornada"
# Endpoint del acta oficial (PDF) que la PNFG genera por partido. Cada partido
# de `NFG_CmpJornada` viene con un `CodActa` numérico único; con él se construye
# esta URL y la federación devuelve el acta firmada por el árbitro como PDF.
ACTA_URL_TMPL = (
    "https://resultados.rfef.es/pnfg/NPcd/NFG_CMP_Alineacion"
    "?cod_primaria=1000121&codacta={cod_acta}&NPcd_Pdf=1"
)

# "DD-MM-YYYY HH:MM" dentro de la fila del partido.
_DT_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})\s+(\d{2}):(\d{2})")
# Opción del <select name=jornada>: "1 - 06-09-2025".
_JORNADA_OPT_RE = re.compile(r"^\s*(\d+)\s*-\s*\d{2}-\d{2}-\d{4}")
# `<a href="...NFG_CmpPartido?...CodActa=NNN...">` o `cod_acta=NNN`.
_COD_ACTA_RE = re.compile(r"[Cc]od[_]?[Aa]cta=(\d+)")

_BACKOFFS = [15, 30, 60, 120]


class RateLimitBreaker:
    """Cortacircuitos para el rate-limit de la PNFG.

    **El problema que resuelve.** Un run completo pide ~360 páginas (12 grupos
    x ~30 jornadas). Cuando la PNFG bloquea —200 con el cuerpo vacío—, cada
    jornada agota sus cuatro backoffs: 15+30+60+120 = 225 s **por jornada**, y
    después el bucle sigue con la siguiente como si nada. Un run bloqueado de
    verdad se pasa horas reintentando, alimentando el bloqueo que causó el
    reintento, y muere en el corte de 6 h de Actions **sin llegar a publicar**.

    Así que aquí el fallo no se reintenta indefinidamente: se cuenta.

    - `per_group` jornadas seguidas sin respuesta → se abandona ese grupo. A
      225 s cada una, tres ya son once minutos de evidencia; la cuarta no
      informa de nada nuevo.
    - `run_budget` fallos en total → no se pide **ni una página más** en todo el
      run. El presupuesto es del run entero y no por grupo a propósito: doce
      grupos con dos fallos cada uno describen el mismo servidor enfadado que
      un grupo con veinticuatro, y el objetivo es dejar de pegarle.

    **Abandonar no es perder.** Lo que este cortacircuitos deja a medias lo
    rellena `calendar_cache` con lo que trajeron los runs anteriores, y por
    encima `inherit_calendars` conserva el calendario publicado si es más
    completo. Sin esas dos piezas, cortar antes sería sencillamente publicar
    menos: no lo actives sin ellas.

    **El contador de reintentos se hunde tras el primer fallo, y se recupera
    solo.** Ese primer fallo ya demostró que el servidor está negando; volver a
    pagar 225 s en la jornada siguiente es lo que convierte un run de 26 min en
    uno de seis horas. Una sola respuesta buena lo restaura, así que un corte
    transitorio no degrada el resto del run.
    """

    def __init__(self, *, per_group: int = 3, run_budget: int = 12) -> None:
        self.per_group = per_group
        self.run_budget = run_budget
        self.failures = 0
        self.groups_abandoned = 0
        self._consecutive = 0
        self._label = ""

    @property
    def blocked(self) -> bool:
        """El run agotó su presupuesto: no se pide nada más a la PNFG."""
        return self.failures >= self.run_budget

    @property
    def group_tripped(self) -> bool:
        """Demasiadas jornadas seguidas sin respuesta en el grupo en curso."""
        return self._consecutive >= self.per_group

    def start_group(self, label: str) -> None:
        self._label = label
        self._consecutive = 0

    def note_success(self) -> None:
        self._consecutive = 0

    def note_failure(self) -> None:
        self.failures += 1
        self._consecutive += 1

    def note_group_abandoned(self) -> None:
        self.groups_abandoned += 1

    def retries_for(self, retries: int) -> int:
        """Reintentos que tocan ahora. Ver la nota de la clase: tras un fallo sin
        éxito de por medio, uno solo."""
        return 1 if self._consecutive else retries

    def summary(self) -> str | None:
        """Una línea para `warnings` del JSON publicado, o None si no hubo nada
        que contar.

        Va al JSON y no solo al log porque un run recortado se parece
        demasiado a un run normal: sin esto, la única diferencia visible entre
        "la federación no lo ha publicado" y "nos bloquearon" es un recuento de
        jornadas que nadie mira.
        """
        if not self.failures:
            return None
        parte = (f"la PNFG rate-limitó {self.failures} jornada/s"
                 f"{f' y se abandonaron {self.groups_abandoned} grupo/s' if self.groups_abandoned else ''}")
        if self.blocked:
            return (f"{parte}; agotado el presupuesto de {self.run_budget} fallos, "
                    f"el resto del calendario no se pidió. Lo publicado sale de la "
                    f"caché y del JSON anterior")
        return f"{parte}; esas jornadas salen de la caché o del JSON anterior"


def resolve_temporada_code(season: str, *, session: requests.Session | None = None) -> str | None:
    """Alias histórico de `rfef_discovery.resolve_season_code`.

    La implementación se mudó a `rfef_discovery` porque allí el `<select
    name=temporada>` no es un detalle del calendario sino el primer paso del
    descubrimiento de competiciones. Se mantiene el nombre para no tocar los
    callers.
    """
    return resolve_season_code(season, session=session)


def teams_from_calendar(calendar: list[dict]) -> list[str]:
    """Nombres de equipo deducidos de un calendario (unión de local y visitante).

    Es la **fuente primaria de equipos antes de que arranque la liga**, y ese
    "antes" es justo cuando el scraper hace falta. La tabla de clasificación no
    existe hasta que se juega la J1: en agosto está siempre vacía, el scraper se
    quedaba sin equipos y caía al fallback curado de la temporada anterior. El
    calendario, en cambio, existe en cuanto se sortea.

    Se recorren **todas** las jornadas y no solo la primera: con un número impar
    de equipos hay uno que descansa cada jornada, y mirando solo la J1 ese se
    perdería.

    Devuelve los nombres ordenados alfabéticamente. El orden importa poco para
    la app —el selector los reordena— pero uno estable evita que el JSON
    publicado cambie de un run a otro sin que haya cambiado nada.
    """
    seen: dict[str, str] = {}
    for jornada in calendar:
        for match in jornada.get("matches", []):
            for name in (match.get("home"), match.get("away")):
                name = (name or "").strip()
                if not name:
                    continue
                key = _norm(name)
                # Se conserva la primera grafía vista: las siguientes solo
                # varían en espacios o comillas.
                if key and key not in seen:
                    seen[key] = name
    return [seen[k] for k in sorted(seen)]


def fetch_division_calendar(
    cod_competicion: str | int,
    cod_grupo: str | int,
    *,
    temporada_code: str | None = None,
    session: requests.Session | None = None,
    retries: int = 4,
    jornada_delay: float = 4.0,
    max_jornadas: int | None = None,
    breaker: "RateLimitBreaker | None" = None,
) -> list[dict]:
    """Devuelve `[{"jornada": N, "matches": [{"home","away","date"}]}]`.

    `max_jornadas` corta tras las N primeras. Existe para el modo "solo
    equipos" (`scrape.py --teams-only`): la plantilla de un grupo se deduce de
    dos jornadas, y bajarse las ~30 cuesta hora y media contra el rate-limit de
    la PNFG. Dos, y no una, porque con un número impar de equipos hay uno que
    descansa cada jornada y con la J1 sola se perdería.

    `breaker` corta cuando la PNFG está bloqueando (ver `RateLimitBreaker`). Se
    comparte entre grupos para que el presupuesto sea del run entero; sin él se
    usa uno propio, que da el corte por grupo pero no el global.

    `date` en ISO-8601 (`YYYY-MM-DDTHH:MM:00`) o None si la fila no trae fecha.
    Estrategia: descarga la jornada 1 para leer el `<select>` de jornadas
    (cuántas hay), parsea sus partidos, y luego itera el resto de jornadas con
    una pausa entre cada una. Ante fallo de una jornada concreta, la salta.
    """
    s = session or make_session()
    label = f"{cod_competicion}/{cod_grupo}"
    br = breaker or RateLimitBreaker()
    br.start_group(label)

    if br.blocked:
        print(f"  [rfef-cal] {label}: omitido, el run agotó su presupuesto de fallos")
        return []

    first_html = _fetch_jornada_html(
        s, cod_competicion, cod_grupo, 1, temporada_code, br.retries_for(retries))
    if first_html is None:
        br.note_failure()
        br.note_group_abandoned()
        print(f"  [rfef-cal] {label}: sin respuesta para J1; calendario vacío")
        return []
    br.note_success()

    jornada_nums = _parse_jornada_numbers(first_html)
    if not jornada_nums:
        # Página sin <select> de jornadas poblado: intentar parsear J1 sola.
        matches = _parse_matches(first_html)
        return [{"jornada": 1, "matches": matches}] if matches else []

    if max_jornadas is not None:
        jornada_nums = jornada_nums[:max_jornadas]

    out: list[dict] = []
    for num in jornada_nums:
        if num == 1:
            html = first_html
        else:
            if br.blocked:
                print(f"  [rfef-cal] {label}: cortado en J{num}, el run agotó su "
                      f"presupuesto de fallos")
                br.note_group_abandoned()
                break
            time.sleep(jornada_delay)
            html = _fetch_jornada_html(
                s, cod_competicion, cod_grupo, num, temporada_code,
                br.retries_for(retries))
            if html is None:
                br.note_failure()
                if br.group_tripped:
                    print(f"  [rfef-cal] {label}: {br.per_group} jornadas seguidas "
                          f"sin respuesta; se abandona el grupo en J{num}")
                    br.note_group_abandoned()
                    break
                print(f"  [rfef-cal] {label}: J{num} sin respuesta, saltando")
                continue
            br.note_success()
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
        tr_text = tr.get_text(" ", strip=True)
        m = _DT_RE.search(tr_text)
        if m:
            dd, mm, yyyy, hh, mn = m.groups()
            date_iso = f"{yyyy}-{mm}-{dd}T{hh}:{mn}:00"

        # CodActa: aparece como `CodActa=NNN` o `codacta=NNN` en los <a href>
        # que apuntan al detalle del partido o al PDF de la alineación. Se
        # construye la URL del acta solo si encontramos el código.
        acta_url = None
        href_blob = " ".join(a.get("href", "") for a in tr.find_all("a", href=True))
        ma = _COD_ACTA_RE.search(href_blob)
        if ma:
            acta_url = ACTA_URL_TMPL.format(cod_acta=ma.group(1))

        out.append({
            "home": home,
            "away": away,
            "date": date_iso,
            "actaUrl": acta_url,
        })
    return out


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()
