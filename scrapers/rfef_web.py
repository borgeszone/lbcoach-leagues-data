"""Equipos desde la web pública de la RFEF (`rfef.es`), no desde la PNFG.

## Por qué hace falta otra fuente

La PNFG solo sabe de equipos cuando hay **clasificación**, y la clasificación no
existe hasta que se juega la J1. O sea: justo en agosto, que es cuando una
entrenadora se crea el equipo y quiere importar sus rivales, la fuente primaria
está vacía. Hasta ahora eso se tapaba con calendarios PDF cuya URL se llevaba
escrita, y una de ellas —la de Segunda Femenina— **no lleva la temporada
dentro**:

    https://rfef.es/sites/default/files/calendario_grupo_2_segunda_femenina_futbol_sala.pdf

Ese fichero sigue sirviendo el de 2025-26 (portada "Temporada 2025-2026",
subido el 25/08/2025). Resultado: en agosto de 2026 se publicaron los 48 equipos
del año pasado con el sello de la temporada nueva. El PDF real de 2026-27 existe,
pero se llama `calendario_2af_g2-1-3.pdf` — un nombre que **no se puede deducir**.

De ahí las dos reglas de este módulo:

1. **Ninguna URL de PDF se escribe a mano.** Se descubren siguiendo los enlaces
   de la página de competición, que es lo que la federación mantiene.
2. **Un PDF solo vale si su portada declara la temporada pedida.** Es la misma
   regla que `_pdf_declares_season` en `rfef.py`, aplicada aquí desde el principio
   y no como parche.

## Qué aporta cada fuente

La página de competición (`/es/competiciones/<slug>`) trae un `ul.lista-escudos`
con **todos** los equipos de la división y su escudo. Es exacta (16 / 48 / 96) y
está viva desde antes del sorteo. Lo que no trae es el reparto por grupos: los 48
de Segunda Femenina van en una sola lista.

El PDF de cada grupo sí lo trae, y además el nombre oficial completo. Así que:

    nombre corto y escudo  ->  página        ("Bisontes Castellón")
    grupo y nombre oficial ->  PDF del grupo ("Feme Castellón C.F.S.")

El corto es el que ve la entrenadora; el oficial viaja como alias para casar con
el calendario y con los partidos que ya tenga guardados. Ver `pair_names`.

## Los escudos de esta página NO se publican

`ul.lista-escudos` sirve las imágenes desde `t.resfu.com`, que es el CDN de
BeSoccer, no de la federación. La allowlist de `logo_resolver` admite solo
`rfef.filesnovanet.es` desde IP-004, y enlazar en caliente el CDN de un tercero
desde la app es exactamente lo que esa auditoría quitó. Se parsea la URL porque
identifica al equipo y ayuda a depurar, pero `rfef.py` no la usa como `logoUrl`.
Los escudos oficiales llegan solos en cuanto arranca la clasificación de la PNFG,
y `badges-cache.json` los conserva a partir de ahí.
"""
from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field

import requests

BASE = "https://rfef.es"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}

_TIMEOUT = 40

# Slug de la página de competición de cada división. Los slugs son estables
# —describen la competición, no su edición— a diferencia de los códigos de la
# PNFG, que cambian cada temporada. Aun así son un valor escrito a mano: si uno
# deja de responder, la división se queda sin esta fuente y `rfef.py` lo avisa,
# no lo rellena con otra cosa.
#
# Ojo con el de Primera Femenina: la página lleva el nombre del patrocinador
# ("iberdrola"), así que es el candidato más probable a cambiar de un año a otro.
COMPETITION_SLUGS: dict[str, str] = {
    "rfef-primera-fs-masc": "primera-division-fs",
    "rfef-segunda-fs-masc": "segunda-division-fs",
    "rfef-segunda-b-fs-masc": "segunda-division-b-fs",
    "rfef-primera-fs-fem": "primera-futbol-sala-iberdrola",
    "rfef-segunda-fs-fem": "segunda-division-fs-femenina",
}


@dataclass(frozen=True)
class WebTeam:
    """Un equipo tal y como lo lista la página de competición."""
    name: str
    badge_url: str | None = None


@dataclass(frozen=True)
class PdfRoster:
    """El plantel de un grupo, leído de su PDF de calendario."""
    season: str | None
    competition: str | None
    group_label: str | None
    group_id: str | None
    teams: list[str] = field(default_factory=list)
    url: str | None = None


# ── Red ─────────────────────────────────────────────────────────────────────

def _get(url: str) -> str | None:
    """HTML de una página de rfef.es, o None. Sin reintentos: `rfef.es` es un
    Drupal normal y no rate-limita como la PNFG; si falla, falla."""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as e:
        print(f"  [rfef-web] {url}: {e}")
        return None
    if r.status_code != 200:
        print(f"  [rfef-web] {url}: HTTP {r.status_code}")
        return None
    return r.text


def _get_bytes(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as e:
        print(f"  [rfef-web] {url}: {e}")
        return None
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        return None
    return r.content


def fetch_competition_html(slug: str) -> str | None:
    return _get(f"{BASE}/es/competiciones/{slug}")


# ── Parsers de la página (puros) ────────────────────────────────────────────

def parse_teams(html: str) -> list[WebTeam]:
    """Equipos del bloque `ul.lista-escudos`.

    El nombre se toma del tooltip y no del `alt` de la imagen: coinciden hoy,
    pero el tooltip es el texto que la federación enseña y el `alt` es
    accesibilidad, que es lo primero que se queda sin mantener.

    El orden se conserva porque en las divisiones con grupos la lista viene
    agrupada (16 + 16 + 16). **No se usa para asignar grupos** —para eso están
    los PDF— pero saber que llega ordenada ayuda a leer los avisos.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: list[WebTeam] = []
    seen: set[str] = set()
    for li in soup.select("ul.lista-escudos li.escudo"):
        tip = li.select_one(".escudos-tooltip")
        img = li.find("img")
        name = (tip.get_text(strip=True) if tip else "") or (
            (img.get("alt") or "").strip() if img else "")
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            continue
        key = norm(name)
        if not key or key in seen:
            continue
        seen.add(key)
        src = (img.get("src") or "").strip() if img else ""
        out.append(WebTeam(name=name, badge_url=src or None))
    return out


_PDF_HREF_RE = re.compile(r'href="(/sites/default/files/[^"]+\.pdf)"', re.I)
_NEWS_HREF_RE = re.compile(
    r'href="(?:https?://rfef\.es)?(/es/noticias/[^"#?]+)"', re.I)


def parse_pdf_links(html: str) -> list[str]:
    """URLs absolutas de los PDF servidos por el propio rfef.es."""
    out, seen = [], set()
    for path in _PDF_HREF_RE.findall(html):
        url = BASE + path
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def parse_news_links(html: str, *, must_contain: str = "calendario") -> list[str]:
    """Noticias enlazadas cuyo slug menciona `must_contain`.

    Los PDF de calendario no cuelgan de la página de competición: viven en la
    noticia que los anuncia, y la página los enlaza. Filtrar por slug evita
    descargar las veinte noticias de portada para encontrar tres ficheros.
    """
    out, seen = [], set()
    for path in _NEWS_HREF_RE.findall(html):
        if must_contain and must_contain not in path.lower():
            continue
        url = BASE + path
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


_PNFG_LINK_RE = re.compile(
    r"NFG_CmpJornada\?[^\"'<>\s]*CodCompeticion=(?P<comp>\d+)[^\"'<>\s]*?"
    r"CodGrupo=(?P<grupo>\d+)[^\"'<>\s]*?CodTemporada=(?P<temporada>-?\d+)",
    re.I)


def parse_pnfg_links(html: str) -> list[tuple[str, str, str]]:
    """`[(competicion, grupo, temporada), ...]` de los enlaces a la PNFG.

    La página de competición enlaza su propio calendario en la PNFG —es el botón
    "Actas, clasificación y calendario"— y ese enlace lleva los tres códigos
    dentro. Es la vía para recuperar el código de **grupo** cuando el catálogo
    de la PNFG no contesta, que es su llamada más frágil.

    **No se puede creer sin comprobar la temporada**, y no es teórico: el 29 de
    agosto de 2026 la página de Segunda B enlazaba `CodTemporada=21` y, encima,
    al playoff de ascenso de 2025-26; la de Segunda Femenina también apuntaba a
    la temporada pasada. Las de Segunda y Primera Femenina sí estaban al día.
    Media web actualizada y media no es el estado normal de este sitio, así que
    el caller filtra por `CodTemporada`. Ver `pnfg_group_for`.
    """
    out, seen = [], set()
    for m in _PNFG_LINK_RE.finditer(html.replace("&amp;", "&")):
        key = (m.group("comp"), m.group("grupo"), m.group("temporada"))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def pnfg_group_for(
    html: str, competition_code: str, season_code: str,
) -> str | None:
    """Código de grupo que la página anuncia para **esta** competición y
    temporada, o None.

    Las dos condiciones son el guard: la temporada evita heredar la del año
    pasado, y exigir que la competición sea la que ya se descubrió evita que un
    enlace a un playoff o a otra categoría se cuele como si fuera la liga.
    """
    for comp, grupo, temporada in parse_pnfg_links(html):
        if comp == str(competition_code) and temporada == str(season_code):
            return grupo
    return None


def discover_calendar_pdfs(slug: str, *, max_news: int = 6) -> list[str]:
    """PDF de calendario alcanzables desde la página de una competición.

    Se miran también los PDF enlazados directamente en la página, aunque hoy no
    haya ninguno: si la federación deja de pasar por una noticia, esto sigue
    funcionando sin tocar nada.
    """
    html = fetch_competition_html(slug)
    if html is None:
        return []
    urls = list(parse_pdf_links(html))
    for news in parse_news_links(html)[:max_news]:
        news_html = _get(news)
        if news_html is None:
            continue
        for url in parse_pdf_links(news_html):
            if url not in urls:
                urls.append(url)
    return urls


# ── Parsers del PDF ─────────────────────────────────────────────────────────

# La cabecera de los dos generadores que usa la RFEF comparte forma:
#
#   Segunda División Fútbol Sala Femenino, Grupo 2   Temporada 2026-2027
#   Segunda División B Fútbol Sala, Grupo 4          Temporada 2026-2027
#   Segunda División Fútbol Sala Masculino (OPCIÓN 1), único  Temporada 2026-2027
#
# El de Liga Prime es el raro y no dice "Temporada":
#
#   Calendario Liga Prime Futsal 2026/2027
_SEASON_RE = re.compile(r"Temporada\s+(\d{4})\s*[-/]\s*(\d{4})", re.I)
_SEASON_LOOSE_RE = re.compile(r"\b(\d{4})\s*[-/]\s*(\d{4})\b")
_HEADER_RE = re.compile(
    r"^(?P<comp>.+?),\s*(?P<group>Grupo\s*\d+|[Úú]nico)\s+Temporada", re.I | re.M)
_GRUPO_N_RE = re.compile(r"grupo\s*(\d+)", re.I)

# Ruido de la primera línea de los dos generadores, antes del nombre real de la
# competición.
_HEADER_NOISE_RE = re.compile(
    r"^\s*(creaci[oó]n de calendario|gesti[oó]n de competiciones|"
    r"calendario de competiciones|calendario)\s*", re.I)


def parse_pdf_header(text: str) -> tuple[str | None, str | None, str | None]:
    """`(competicion, grupo, temporada)` de la portada de un calendario.

    `temporada` se devuelve normalizada a `YYYY-YYYY` para poder compararla con
    la que pide `scrape.py` sin pelearse con la barra o el guion.

    Hay **dos formatos** y el segundo no es una rareza que se pueda ignorar: es
    justo el de la máxima categoría masculina.

        Segunda División Fútbol Sala Femenino, Grupo 2  Temporada 2026-2027
        Calendario Liga Prime Futsal 2026/2027

    El segundo no dice "Temporada" ni lleva grupo, así que se lee quitando el
    ruido de cabecera y la temporada del final. Sacar el nombre importa aunque
    no haya grupo: es lo que permite comprobar **de qué competición es** un PDF
    en vez de fiarse de qué página lo enlazaba. Ver `roster_division`.
    """
    season = None
    m = _SEASON_RE.search(text) or _SEASON_LOOSE_RE.search(text)
    if m:
        season = f"{m.group(1)}-{m.group(2)}"

    comp = group = None
    h = _HEADER_RE.search(text)
    if h:
        comp = re.sub(r"\s+", " ", h.group("comp")).strip()
        group = re.sub(r"\s+", " ", h.group("group")).strip()
    else:
        for line in text.splitlines()[:4]:
            line = re.sub(r"\s+", " ", line).strip()
            if not line:
                continue
            cand = _HEADER_NOISE_RE.sub("", line).strip()
            cand = (_SEASON_RE.sub("", cand)
                    if _SEASON_RE.search(cand) else _SEASON_LOOSE_RE.sub("", cand))
            cand = cand.strip(" ,-")
            if len(cand) >= 6:
                comp = cand
                break
    return comp, group, season


def group_id_from_label(label: str | None) -> str | None:
    """`"Grupo 4"` → `"g4"`. `"único"` → None (división plana).

    El id sale del **número impreso**, no de la posición del PDF en la lista.
    La app persiste `Team.groupId`, así que un id posicional movería a cada
    equipo al grupo del vecino el día que cambie el orden de los enlaces, y no
    fallaría nada visiblemente. Misma regla que `rfef_discovery.parse_groups`.
    """
    if not label:
        return None
    m = _GRUPO_N_RE.search(label)
    return f"g{m.group(1)}" if m else None


# Formato viejo ("Calendario de Competiciones"): lista numerada con el código
# de club entre paréntesis.
_NUMBERED_RE = re.compile(r"^\s*\d+\.-\s*(?P<name>.+?)\s*\((?P<code>\d+)\)\s*$")


def parse_roster_numbered(text: str) -> list[str]:
    """Plantel de la sección "Equipos Participantes" numerada."""
    out = []
    for line in text.splitlines():
        m = _NUMBERED_RE.match(line)
        if m:
            name = re.sub(r"\s+", " ", m.group("name")).strip()
            if name:
                out.append(name)
    return out


def parse_roster_two_columns(page, *, gap_threshold: float = 12.0) -> list[str]:
    """Plantel del generador nuevo ("Creación de Calendario").

    Ahí el plantel va **a dos columnas y sin numerar**, entre la cabecera y la
    primera "Jornada":

        CD Albacete FS                Club Unión Deportiva Loeches
        Inter JP Financial F.S.       Naturpellet San Cristóbal

    En el texto plano las dos columnas quedan pegadas en una sola línea, que es
    justo cómo el parser viejo acababa inventándose equipos como
    "CD Albacete FS Club Unión Deportiva Loeches". Se separan por la posición
    horizontal de las palabras, no por el texto.
    """
    from collections import defaultdict

    lines: dict[int, list[dict]] = defaultdict(list)
    for w in page.extract_words():
        lines[round(w["top"])].append(w)

    out: list[str] = []
    started = False
    for top in sorted(lines):
        words = sorted(lines[top], key=lambda w: w["x0"])
        text = " ".join(w["text"] for w in words)
        low = text.lower()
        if "jornada" in low:
            break
        if not started:
            # El plantel empieza después de la línea de cabecera.
            if "temporada" in low:
                started = True
            continue
        if "algoritmo" in low or not text.strip():
            continue
        for part in _split_columns(words, gap_threshold):
            name = re.sub(r"\s+", " ", part).strip()
            if _plausible_team(name):
                out.append(name)
    return out


def _split_columns(words: list[dict], gap_threshold: float) -> list[str]:
    """Parte una línea por su hueco horizontal mayor, si es lo bastante ancho."""
    if len(words) < 2:
        return [" ".join(w["text"] for w in words)]
    best_gap, at = 0.0, -1
    for i in range(len(words) - 1):
        gap = words[i + 1]["x0"] - (words[i]["x0"] + words[i]["width"])
        if gap > best_gap:
            best_gap, at = gap, i
    if best_gap < gap_threshold or at < 0:
        return [" ".join(w["text"] for w in words)]
    return [
        " ".join(w["text"] for w in words[: at + 1]),
        " ".join(w["text"] for w in words[at + 1:]),
    ]


_NOT_A_TEAM = {
    "real federacion espanola de futbol", "calendario", "temporada",
    "creacion de calendario", "gestion de competiciones", "pagina",
    "primera vuelta", "segunda vuelta", "equipos participantes",
}


def _plausible_team(s: str) -> bool:
    if len(s) < 3 or len(s) > 80:
        return False
    if not re.search(r"[A-Za-zÀ-ÿ]", s):
        return False
    k = norm(s)
    if not k or k in {norm(p) for p in _NOT_A_TEAM}:
        return False
    if sum(c.isdigit() for c in s) > len(s) * 0.4:
        return False
    return True


# ── Calendario a varias columnas ────────────────────────────────────────────
#
# Los calendarios de la RFEF se maquetan a **dos columnas de jornadas**: la ida
# a la izquierda y la vuelta a la derecha, con la J1 y la J16 empezando en la
# misma línea. `extract_text()` las devuelve pegadas, así que leído como texto
# plano un grupo de 16 equipos daba 240 nombres distintos y 15 jornadas en vez
# de 30 — cada línea era un partido inventado entre el visitante de la ida y el
# local de la vuelta.
#
# Lo que sí es estable es que **el guion separador está a una x fija por
# columna** (159 y 439 en los PDF de 2026-27, 155 y 428 en los del generador
# clásico). Un guion dentro de un nombre —"Entreparéntesis - Enertel FS
# Talavera", "SolarMon-Les Glories"— cae en otra x y no se confunde.
#
# Lo que **no** sirve, y se probó: buscar el canal vertical entre columnas. En
# los PDF de Segunda Femenina no hay ninguno, porque algún nombre largo lo cruza.

_DASHES = frozenset({"-", "–", "—"})

_JORNADA_RE = re.compile(
    r"jornada\s*(?P<n>\d+)\s*-?\s*\(\s*(?P<d>\d{1,2})[-/](?P<m>\d{1,2})[-/](?P<y>\d{2,4})\s*\)",
    re.I)


def _lines_of(page) -> list[list[dict]]:
    from collections import defaultdict
    lines: dict[int, list[dict]] = defaultdict(list)
    for w in page.extract_words():
        lines[round(w["top"])].append(w)
    return [sorted(lines[t], key=lambda w: w["x0"]) for t in sorted(lines)]


def separator_columns(
    pages_lines: list[list[list[dict]]], *, tol: float = 6.0,
    share: float = 0.4, min_lines: int = 5,
) -> list[float]:
    """Las x en las que se repite el guion separador: una por columna.

    **No basta con "se repite mucho"**, y este es el detalle que cuesta una
    tarde: un club con guion en el nombre —"Entreparéntesis - Enertel FS
    Talavera"— lo lleva siempre a la misma x, porque su línea empieza siempre
    donde empieza la columna. Con un umbral absoluto, ese guion se colaba como
    una tercera columna y descuadraba el reparto entero: la mitad de las
    jornadas se quedaban sin un solo partido.

    Lo que distingue a un separador de verdad es que aparece en **casi todas**
    las líneas de partido, no en las 30 de un equipo. De ahí el umbral
    relativo al mayor: un club juega 30 de las ~240 líneas de su grupo, así que
    se queda muy por debajo.

    Devuelve `[]` cuando el PDF no usa guion —Liga Prime separa local y
    visitante solo por el hueco— y entonces el caller se queda con el parser
    de siempre.
    """
    xs: list[float] = []
    for lines in pages_lines:
        for words in lines:
            for w in words:
                if w["text"] in _DASHES:
                    xs.append(w["x0"])
    xs.sort()

    clusters: list[list[float]] = []
    for x in xs:
        if clusters and x - clusters[-1][-1] <= tol:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    if not clusters:
        return []

    biggest = max(len(c) for c in clusters)
    keep = [c for c in clusters
            if len(c) >= min_lines and len(c) >= biggest * share]
    return sorted(sum(c) / len(c) for c in keep)


def _split_largest_gap(words: list[dict]) -> tuple[list[dict], list[dict]]:
    """Parte por el salto horizontal mayor. Es lo que separa al visitante de una
    columna del local de la siguiente, que van pegados en la misma línea."""
    if len(words) < 2:
        return words, []
    best, at = -1.0, 0
    for i in range(len(words) - 1):
        gap = words[i + 1]["x0"] - (words[i]["x0"] + words[i]["width"])
        if gap > best:
            best, at = gap, i
    return words[: at + 1], words[at + 1:]


def _text(words: list[dict]) -> str:
    return re.sub(r"\s+", " ", " ".join(w["text"] for w in words)).strip()


def split_match_line(
    words: list[dict], separators: list[float], *, tol: float = 6.0,
) -> list[tuple[int, str, str]]:
    """`[(columna, local, visitante), ...]` de una línea de partidos."""
    seps = []
    for i, w in enumerate(words):
        if w["text"] not in _DASHES:
            continue
        for col, sx in enumerate(separators):
            if abs(w["x0"] - sx) <= tol:
                seps.append((i, col))
                break
    if not seps:
        return []

    segments: list[list[dict]] = []
    prev = 0
    for i, _col in seps:
        segments.append(words[prev:i])
        prev = i + 1
    segments.append(words[prev:])

    out: list[tuple[int, str, str]] = []
    home = segments[0]
    for k, (_i, col) in enumerate(seps):
        nxt = segments[k + 1]
        if k + 1 < len(seps):
            away, home_next = _split_largest_gap(nxt)
        else:
            away, home_next = nxt, []
        h, a = _text(home), _text(away)
        if _plausible_team(h) and _plausible_team(a):
            out.append((col, h, a))
        home = home_next
    return out


def _nearest_column(x: float, separators: list[float]) -> int:
    return min(range(len(separators)), key=lambda i: abs(separators[i] - x))


def headers_in_line(
    words: list[dict], separators: list[float],
) -> list[tuple[int, int, str]]:
    """`[(columna, jornada, fecha_iso), ...]` de una línea de cabeceras.

    La columna sale de **la x de la palabra "Jornada"**, no del orden en que
    aparece en la línea. Parece lo mismo y no lo es: en el calendario de
    Segunda las dos cabeceras no están alineadas verticalmente, así que
    "Jornada 16" cae en su propia línea — y por orden se la asignaba a la
    columna izquierda, dejando quince jornadas sin un solo partido.
    """
    out: list[tuple[int, int, str]] = []
    for i, w in enumerate(words):
        if not w["text"].lower().startswith("jornada"):
            continue
        m = _JORNADA_RE.match(_text(words[i:i + 6]))
        if m:
            out.append((_nearest_column(w["x0"], separators),
                        int(m.group("n")), _iso_date(m)))
    return out


def _iso_date(m: re.Match) -> str:
    y = m.group("y")
    y = f"20{y}" if len(y) == 2 else y
    return f"{int(y):04d}-{int(m.group('m')):02d}-{int(m.group('d')):02d}"


def parse_calendar_multicolumn(pdf_bytes: bytes) -> list[dict]:
    """Calendario de un PDF maquetado a dos columnas de jornadas.

    Devuelve `[{"jornada": n, "date": iso, "matches": [{home, away, date}]}]`,
    ordenado por número de jornada. `[]` si el PDF no es de este tipo, para que
    el caller caiga al parser clásico.

    La fecha se repite **en cada partido** además de en la jornada: el cliente
    (`CalendarMatch.fromJson`) la lee de ahí, así que un calendario que solo la
    llevara arriba llegaba a la app sin fecha ninguna.
    """
    try:
        import pdfplumber
    except ImportError:
        return []

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages_lines = [_lines_of(p) for p in pdf.pages]
    except Exception as e:  # noqa: BLE001
        print(f"  [rfef-web] no se pudo leer el calendario: {e}")
        return []

    separators = separator_columns(pages_lines)
    if len(separators) < 2:
        return []

    jornadas: dict[int, dict] = {}
    order: list[int] = []
    current: list[int | None] = [None] * len(separators)

    for lines in pages_lines:
        for words in lines:
            line = _text(words)  # noqa: F841 (se conserva por claridad)

            heads = headers_in_line(words, separators)
            if heads:
                for col, n, date in heads:
                    if n not in jornadas:
                        jornadas[n] = {"jornada": n, "date": date, "matches": []}
                        order.append(n)
                    current[col] = n
                continue

            for col, home, away in split_match_line(words, separators):
                n = current[col] if col < len(current) else None
                if n is None:
                    continue
                jornadas[n]["matches"].append(
                    {"home": home, "away": away, "date": jornadas[n]["date"]})

    return [jornadas[n] for n in sorted(order)]


def read_pdf_roster(pdf_bytes: bytes, *, url: str | None = None) -> PdfRoster | None:
    """Cabecera + plantel de un PDF de calendario, con los dos generadores.

    Devuelve None si no se puede abrir. Un `PdfRoster` con `season=None` es un
    PDF que no dice de qué temporada es: el caller **tiene que descartarlo**, no
    darle el beneficio de la duda. Ver la cabecera del módulo.
    """
    try:
        import pdfplumber
    except ImportError:
        return None
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if not pdf.pages:
                return None
            first = pdf.pages[0]
            text = first.extract_text() or ""
            comp, group, season = parse_pdf_header(text)
            teams = parse_roster_numbered(text)
            if not teams:
                teams = parse_roster_two_columns(first)
    except Exception as e:  # noqa: BLE001 - un PDF roto no puede tumbar el run
        print(f"  [rfef-web] no se pudo leer el PDF: {e}")
        return None

    return PdfRoster(
        season=season, competition=comp, group_label=group,
        group_id=group_id_from_label(group), teams=teams, url=url,
    )


def roster_division(roster: PdfRoster) -> str | None:
    """A qué división nuestra pertenece un PDF, según **su propia portada**.

    Hace falta porque una página de competición enlaza las noticias de
    calendario de media federación: desde `segunda-division-b-fs` se llega a los
    ocho PDF de División de Honor Juvenil y al de Segunda. Filtrar por quién
    enlazó el fichero sería fiarse otra vez de la ruta; se filtra por lo que el
    documento declara ser.

    Reutiliza `classify_competition`, que es quien ya sabe casar el nombre de
    una competición con un id estable y descartar juveniles y copas.
    """
    from scrapers.rfef_discovery import COMP_LEAGUE, classify_competition

    if not roster.competition:
        return None
    clase, div_id, _gender = classify_competition(roster.competition)
    return div_id if clase == COMP_LEAGUE else None


def fetch_rosters(slug: str, season: str, division_id: str) -> list[PdfRoster]:
    """Planteles por grupo de una división, **solo** de la temporada pedida.

    Los PDF de otra temporada se descartan con un aviso por pantalla: son el
    fallo que este módulo existe para no repetir, así que tienen que dejar
    rastro aunque el run acabe bien. Los de otra competición se descartan en
    silencio, que es lo normal.
    """
    out: list[PdfRoster] = []
    seen_groups: set[str | None] = set()
    for url in discover_calendar_pdfs(slug):
        pdf = _get_bytes(url)
        if not pdf:
            continue
        roster = read_pdf_roster(pdf, url=url)
        if roster is None or not roster.teams:
            continue
        if roster_division(roster) != division_id:
            continue
        if roster.season != season:
            print(f"  [rfef-web] descartado {url.rsplit('/', 1)[-1]}: declara "
                  f"temporada {roster.season!r}, se pidió {season!r}")
            continue
        if roster.group_id in seen_groups:
            # Dos ficheros para el mismo grupo (la federación resubió uno y
            # Drupal le puso `_0`). Gana el primero; los enlaces van de más
            # nuevo a más viejo en la noticia.
            continue
        seen_groups.add(roster.group_id)
        out.append(roster)
    return out


# ── Emparejado nombre corto ↔ nombre oficial ────────────────────────────────

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


# Palabras que casi todos los clubes comparten y que por tanto no distinguen a
# ninguno. Sin quitarlas, "CD Leganés FS" casa igual de bien con "C.D. Melistar"
# que con "C.D. Leganés F.S.", porque comparten "cd" y "fs".
_STOP = frozenset({
    "cd", "cf", "cfs", "fs", "fsf", "ad", "ae", "sd", "ud", "udc", "cdb", "cde",
    "club", "deportivo", "deportiva", "futbol", "sala", "futsal", "fc", "ca",
    "de", "del", "la", "el", "los", "las", "y", "a", "b", "c", "d", "e",
    "sociedad", "agrupacion", "asociacion", "cfsf", "efs", "cdu", "at",
    "atletico", "atletic", "union",
})


def _tokens(name: str) -> set[str]:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    words = [w for w in re.split(r"[^a-z0-9]+", s.lower()) if w]
    keep = {w for w in words if w not in _STOP and len(w) > 1}
    # Si el nombre es solo palabras genéricas ("AE Les Corts B"), mejor
    # quedarse con ellas que con un conjunto vacío que no casa con nada.
    return keep or set(words)


def _score(short: str, official: str) -> tuple[float, float]:
    """`(cobertura, ajuste)`. Dos números, y los dos hacen falta.

    **Cobertura** = palabras compartidas sobre el nombre más corto de los dos.
    Es la que decide si son el mismo club: "Nunsys El Pilar" y "Colegio El Pilar
    Valencia" comparten su única palabra distintiva.

    **Ajuste** = las mismas compartidas sobre el más largo. Solo desempata, y
    hace falta porque la cobertura satura en 1,0: "Granada FS" cubre por igual
    a "Granada FS Femenino" y a "Fundación UAPO Granada FS Femenino". Sin el
    ajuste, el desempate sería alfabético y le cambiaría el nombre a un club.
    """
    a, b = _tokens(short), _tokens(official)
    if not a or not b:
        return (0.0, 0.0)
    inter = a & b
    if not inter:
        # Última oportunidad: el corto entero contenido en el oficial
        # ("Burela FS" dentro de "REYCO Burela FS") con el ruido ya fuera.
        ka, kb = norm(short), norm(official)
        if len(ka) >= 5 and ka in kb:
            return (0.55, len(ka) / max(len(kb), 1))
        return (0.0, 0.0)
    return (len(inter) / min(len(a), len(b)), len(inter) / max(len(a), len(b)))


def pair_names(
    short_names: list[str], official_names: list[str], *, threshold: float = 0.5
) -> dict[str, str]:
    """`{nombre_oficial: nombre_corto}` para los que casan con confianza.

    Asignación **1:1 y voraz por puntuación**: el par más claro se decide
    primero y retira a los dos de la baraja. Sin eso, "Granada FS" se lleva
    tanto a "Granada FS Femenino" como a "Fundación UAPO Granada FS", y el
    segundo se queda sin corto o —peor— con el del primero.

    Lo que no llega al umbral se queda fuera a propósito: el caller publica
    entonces el nombre oficial, que es feo pero cierto. Un emparejado inventado
    le cambia el nombre al rival en el histórico de la entrenadora.
    """
    pairs = []
    for o in official_names:
        for s in short_names:
            cover, fit = _score(s, o)
            if cover >= threshold:
                pairs.append((cover, fit, o, s))
    pairs.sort(key=lambda p: (-p[0], -p[1], p[2], p[3]))

    out: dict[str, str] = {}
    used_short: set[str] = set()
    for _cover, _fit, o, s in pairs:
        if o in out or s in used_short:
            continue
        out[o] = s
        used_short.add(s)
    return out
