"""Scraper de RFEF (Real Federación Española de Fútbol).

Qué divisiones existen y con qué códigos se pregunta a la PNFG en cada run
(`scrapers.rfef_discovery`). Aquí ya no hay ningún código de competición
escrito a mano: son por temporada, y tenerlos fijos fue el bug de agosto de
2026 — el scraper republicó la liga de 2025-26 con el sello de 2026-27, sin un
solo error por el camino.

Estrategia de equipos, en cascada por división/grupo:

1. **Clasificación pública** (`scrapers.rfef_clasificacion`): cada `<tr>` trae
   `<img class=escudo_widget>` + `<a>NOMBRE</a>`, así que da nombre canónico y
   escudo oficial en la misma fila. Es la mejor fuente **pero no existe hasta
   que se juega la J1**.
2. **Calendario** (`rfef_calendario.teams_from_calendar`): los equipos salen de
   los enfrentamientos. Existe desde que se sortea, así que es la fuente que
   funciona en pretemporada, que es justo cuando se prepara la temporada nueva.
3. **PDF oficial** de `rfef.es` con `pdfplumber` (legacy). Solo nombres.
4. **Fallback curado** (`data/rfef-fallback.json`), **y solo si su campo
   `season` coincide con la temporada pedida**. Sin esa condición, el fallback
   es una forma silenciosa de publicar la liga del año pasado.

El PDF sigue en la cascada porque su URL sí es estable entre temporadas:

    https://rfef.es/sites/default/files/{YEAR}-07/Calendario_{COMP}_{SEASON}.pdf
    YEAR   = primer año de la temporada (2025-2026 → 2025)
    COMP   = identificador de la competición ("1Div_Sala", "2Div_Sala", …)
    SEASON = "2025-2026"

## Fallar cerrado

Si no se puede verificar contra qué temporada se está scrapeando, `scrape()`
devuelve la categoría vacía con `seasonVerified: False` y `scrape.py` aborta sin
escribir el fichero. Un run que falla deja publicado el JSON anterior, que es
viejo pero coherente; un run que "sale bien" con datos de otra temporada deja
publicada una mentira con fecha de hoy. La segunda es peor y es la que ocurrió.
"""
from __future__ import annotations

import io
import json
import re
import time
import unicodedata
from pathlib import Path

import requests

from scrapers import calendar_cache, rfef_web
from scrapers.logo_resolver import lookup_override, resolve_logo_url
from scrapers.rfef_clasificacion import ScrapedTeam, fetch_division_teams
from scrapers.rfef_calendario import fetch_division_calendar, teams_from_calendar
from scrapers.rfef_discovery import (
    COMP_LEAGUE,
    COMP_UNKNOWN,
    DIVISION_GENDER,
    DIVISION_NAMES,
    SEASON_NOT_PUBLISHED,
    Fetcher,
    classify_competition,
    is_phase,
    list_competitions,
    list_groups,
    resolve_season,
)

DATA_DIR = Path(__file__).parent.parent / "data"

# Config **legacy** de PDFs, indexada por id estable de división.
#
# Los códigos `comp`/`grupo` ya no viven aquí: son por temporada y tenerlos
# escritos a mano fue exactamente el bug de agosto de 2026 (ver la cabecera de
# `rfef_discovery`). Ahora se descubren en cada run.
#
# Lo que sí queda es el PDF oficial de `rfef.es`, que sigue siendo una red de
# seguridad razonable porque su URL **sí** es estable entre temporadas: lleva la
# temporada dentro en vez de un identificador opaco.
#
#   - `pdf_id` → PDF unificado, para divisiones planas.
#   - `groups_url_pattern` → un PDF por grupo (`calendario_grupo_N_*.pdf`).
#
# Segunda B no aparece: nunca ha tenido PDF oficial publicado.
LEGACY_PDF: dict[str, dict] = {
    "rfef-primera-fs-masc": {
        "pdf_id": "1Div_Sala",
        # PDFs sueltos, **indexados por temporada a propósito**.
        #
        # En 2026-27 la RFEF publicó el calendario de Liga Prime Futsal con un
        # nombre que rompe el patrón de siempre: ni prefijo `Calendario_`, ni
        # temporada en el fichero, y la carpeta es el mes de publicación. O sea,
        # una URL que no se puede deducir y que caduca sin avisar — exactamente
        # la clase de valor fijo que causó el bug de agosto.
        #
        # Por eso va bajo su temporada: no se puede usar para otra ni por
        # accidente. Y aun así se comprueba la temporada **impresa dentro del
        # PDF** antes de aceptarlo (`_pdf_declares_season`), porque confiar en
        # la ruta es justo lo que salió mal la otra vez.
        "pdf_urls": {
            "2026-2027":
                "https://rfef.es/sites/default/files/2026-06/Liga_Prime_Futsal.pdf",
        },
    },
    "rfef-segunda-fs-masc": {"pdf_id": "2Div_Sala"},
    "rfef-primera-fs-fem": {"pdf_id": "1DivFem_Sala"},
    "rfef-segunda-fs-fem": {
        "groups_url_pattern":
            "https://rfef.es/sites/default/files/"
            "calendario_grupo_{n}_segunda_femenina_futbol_sala.pdf",
        "max_groups": 10,
    },
}

# Orden de publicación de las divisiones en el JSON. Se fija aquí y no se toma
# del orden en que la PNFG las devuelva: así el fichero publicado no cambia de
# orden entre runs sin que haya cambiado nada, y el desplegable de la app no
# baila.
DIVISION_ORDER = (
    "rfef-primera-fs-masc",
    "rfef-segunda-fs-masc",
    "rfef-segunda-b-fs-masc",
    "rfef-primera-fs-fem",
    "rfef-segunda-fs-fem",
)


# ── De dónde salieron los equipos, y si eso permite fechar la temporada ──────
#
# El scraper ya se equivocó una vez publicando la liga de 2025-26 con el sello de
# 2026-27, y dos veces por caminos distintos: los códigos de competición escritos
# a mano (§6.32) y un PDF cuya URL no lleva la temporada dentro (§6.49). Las dos
# veces el run terminó bien y nadie se enteró.
#
# Así que la procedencia se anota por división y `scrape()` **se niega a publicar
# equipos que no vengan de una fuente fechable**. La lista de abajo es la
# afirmación completa que el scraper puede hacer sobre la temporada; añadir una
# fuente sin poder fecharla es reabrir el mismo agujero.
SOURCE_CLASIFICACION = "clasificacion"  # comp/grupo descubiertos para esta temporada
SOURCE_PDF_RFEF = "pdf-rfef"            # portada del PDF: "Temporada 2026-2027"
SOURCE_CALENDARIO = "calendario"        # NFG_CmpJornada con CodTemporada explícito
SOURCE_PDF_LEGACY = "pdf-legacy"        # idem, por el patrón de URL clásico
SOURCE_FALLBACK = "fallback"            # data/rfef-fallback.json, con su `season`
SOURCE_PAGINA = "pagina"                # rfef.es: NO dice de qué temporada es
SOURCE_NINGUNA = "-"

# Las que permiten afirmar el año. `pagina` queda fuera a propósito.
VERIFIED_SOURCES = frozenset({
    SOURCE_CLASIFICACION, SOURCE_PDF_RFEF, SOURCE_CALENDARIO,
    SOURCE_PDF_LEGACY, SOURCE_FALLBACK,
})


def _pdf_url(pdf_id: str, season: str) -> str:
    year = season.split("-")[0]
    return (
        f"https://rfef.es/sites/default/files/{year}-07/"
        f"Calendario_{pdf_id}_{season}.pdf"
    )


def _download_pdf(url: str, timeout: int = 30) -> bytes | None:
    try:
        r = requests.get(url, timeout=timeout, headers={
            # rfef.es bloquea user-agents por defecto; emulamos un navegador.
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
        })
        if r.status_code != 200:
            print(f"  [rfef] HTTP {r.status_code} en {url}")
            return None
        if not r.content.startswith(b"%PDF"):
            print(f"  [rfef] Respuesta no es un PDF en {url}")
            return None
        return r.content
    except requests.RequestException as e:
        print(f"  [rfef] Error descargando {url}: {e}")
        return None


def _calendar_is_sane(
    calendar: list[dict], roster: list[str] | None, *, label: str = "",
) -> bool:
    """¿El calendario parseado solo nombra a equipos que están en el plantel?

    La asimetría es a propósito, y viene de cómo fallan de verdad estos PDF:

    - **Un nombre de más se rechaza.** Significa que el parser se inventó un
      equipo, casi siempre porque una fila se partió en varias líneas físicas y
      quedó un trozo suelto ("MRB FS", "C.E."). Publicarlo llena el desplegable
      de jornadas de rivales que no existen.
    - **Un nombre de menos se acepta y se avisa.** Es el mismo corte visto por
      el otro lado: en el Grupo 3 de Segunda B, "Col. Santo Ángel/Ccr-Baixsud de
      Castelldefels A" ocupa tres líneas y sus 30 partidos se pierden. Tirar el
      calendario entero por eso deja sin autorrelleno a los otros quince
      equipos para proteger a uno, que es peor negocio.

    Sin plantel con el que comparar no hay nada que afirmar, y se acepta.
    """
    if not roster:
        return True
    known = {rfef_web.norm(n) for n in roster}
    seen = {rfef_web.norm(m[k])
            for j in calendar for m in j.get("matches", []) for k in ("home", "away")}
    invented = seen - known
    if invented:
        print(f"  [rfef-cal] {label}: calendario descartado, nombra a "
              f"{len(invented)} equipo/s que no están en el plantel "
              f"(el PDF parte filas largas en varias líneas)")
        return False
    missing = known - seen
    if missing:
        print(f"  [rfef-cal] {label}: {len(missing)} equipo/s del plantel no "
              f"aparecen en el calendario; se publica igual para el resto")
    return True


def _extract_calendar_from_pdf(pdf_bytes: bytes) -> list[dict]:
    """Extrae el calendario por jornadas del PDF.

    Devuelve `[{jornada: int, date: str?, matches: [{home, away}]}, ...]`
    en orden de jornada. Reutiliza el algoritmo de gap-detection que ya
    funciona para identificar las dos columnas (local | visitante) de cada
    línea de partido, pero además rastrea las cabeceras de jornada que
    aparecen intercaladas en el texto:

        Jornada 1 (06/09/2025)
        TeamA   TeamB
        TeamC   TeamD
        ...
        Jornada 2 (13/09/2025)
        ...

    Si el PDF tiene una sección "Equipos Participantes" al inicio (formato
    de grupos territoriales), la salta — la cabecera de jornada solo
    aparece en las páginas del calendario propiamente dicho.

    NOTA: el calendario es el oficial INICIAL. Aplazamientos y
    recolocaciones mantienen su jornada original por convención de la liga
    (la J10 aplazada al final de temporada sigue siendo J10).
    """
    try:
        import pdfplumber
    except ImportError:
        return []

    COLUMN_GAP_THRESHOLD = 30
    # Dos formatos de cabecera conviven:
    #
    #   "Jornada 1 (06/09/2025)"            PDFs clásicos
    #   "Torneo Apertura J 1 (13/09/2026)"  Liga Prime Futsal desde 2026-27
    #
    # El segundo trae **fase**, y eso no es decorativo: Apertura y Clausura
    # tienen cada uno su J1..J15, así que el número de jornada por sí solo deja
    # de identificar un partido.
    JORNADA_HEADER_RE = re.compile(
        r"^(?:Torneo\s+(\w+)\s+)?J(?:ornada)?\s*(\d+)\s*"
        r"(?:\((\d{1,2}/\d{1,2}/\d{2,4})\))?",
        re.IGNORECASE,
    )

    # Primero, el layout a **dos columnas de jornadas** (ida a la izquierda,
    # vuelta a la derecha, la J1 y la J16 en la misma línea). Ahí este parser
    # no falla a medias: pega el visitante de la ida con el local de la vuelta
    # y devuelve un partido inventado por línea — 240 nombres distintos para un
    # grupo de 16. `parse_calendar_multicolumn` devuelve `[]` si el PDF no es de
    # ese tipo, así que Liga Prime y los calendarios de una sola columna siguen
    # por el camino de siempre.
    multi = rfef_web.parse_calendar_multicolumn(pdf_bytes)
    if multi:
        return multi

    # Clave (fase, jornada) y no solo jornada: con una sola, la J1 de Clausura
    # sobrescribiría la de Apertura y se perderían quince jornadas sin ruido.
    jornadas: dict[tuple[str, int], dict] = {}
    order: list[tuple[str, int]] = []
    current: tuple[str, int] | None = None

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                for line_words in _group_words_by_line(page):
                    line_text = " ".join(w["text"] for w in line_words).strip()

                    # 1. ¿Cabecera de jornada?
                    mm = JORNADA_HEADER_RE.match(line_text)
                    if mm:
                        phase = (mm.group(1) or "").strip().title() or None
                        num = int(mm.group(2))
                        date = _normalize_date(mm.group(3)) if mm.group(3) else None
                        current = (phase or "", num)
                        if current not in jornadas:
                            entry = {"jornada": num, "date": date, "matches": []}
                            if phase:
                                entry["phase"] = phase
                            jornadas[current] = entry
                            # El orden de lectura del PDF es el de la
                            # competición; ordenar por número mezclaría las dos
                            # fases (A1, C1, A2, C2…).
                            order.append(current)
                        elif date and not jornadas[current].get("date"):
                            jornadas[current]["date"] = date
                        continue

                    # 2. ¿Línea de partido?
                    if current is None:
                        continue
                    pair = _split_match_line(line_words, COLUMN_GAP_THRESHOLD)
                    if pair is None:
                        continue
                    home, away = pair
                    if not (_looks_like_team_name(home) and _looks_like_team_name(away)):
                        continue
                    jornadas[current]["matches"].append({
                        "home": home,
                        "away": away,
                    })
    except Exception as e:  # noqa: BLE001
        print(f"  [rfef] Error parseando calendario: {e}")
        return []

    return [jornadas[k] for k in order]


def _group_words_by_line(page) -> list[list[dict]]:
    """Agrupa las palabras de una página por línea (coordenada `top`),
    devolviendo la lista en orden de lectura (top→bottom)."""
    from collections import defaultdict
    lines: dict[int, list[dict]] = defaultdict(list)
    for w in page.extract_words():
        lines[round(w["top"])].append(w)
    out = []
    for top in sorted(lines.keys()):
        words = sorted(lines[top], key=lambda w: w["x0"])
        out.append(words)
    return out


def _split_match_line(line_words: list[dict], gap_threshold: float) -> tuple[str, str] | None:
    """Devuelve `(home, away)` si la línea tiene un gap horizontal claro,
    o None si parece una línea normal (cabecera, sección)."""
    if len(line_words) < 2:
        return None
    max_gap = 0.0
    split_idx = -1
    for i in range(len(line_words) - 1):
        cur_end = line_words[i]["x0"] + line_words[i]["width"]
        gap = line_words[i + 1]["x0"] - cur_end
        if gap > max_gap:
            max_gap = gap
            split_idx = i
    if max_gap < gap_threshold or split_idx < 0:
        return None
    home = _clean_team_name(" ".join(w["text"] for w in line_words[: split_idx + 1]))
    away = _clean_team_name(" ".join(w["text"] for w in line_words[split_idx + 1:]))
    return home, away


def _normalize_date(raw: str) -> str:
    """Normaliza 'DD/MM/YYYY' o 'DD/MM/YY' a ISO 'YYYY-MM-DD'."""
    parts = raw.split("/")
    if len(parts) != 3:
        return raw
    d, m, y = parts
    y = "20" + y if len(y) == 2 else y
    try:
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except ValueError:
        return raw


def _extract_teams_from_pdf(pdf_bytes: bytes) -> list[str]:
    """Extrae nombres únicos de equipos del calendario PDF de RFEF.

    Dos estrategias en cascada:
    1. Sección "Equipos Participantes" con líneas numeradas (formato usado por
       calendarios de divisiones por grupo territorial — más limpio).
    2. Si no encuentra esa sección: agrupa palabras por línea usando
       bounding boxes y separa columnas por el gap horizontal más grande.
    """
    try:
        import pdfplumber
    except ImportError:
        print("  [rfef] pdfplumber no instalado; saltando extracción")
        return []

    teams: set[str] = set()
    COLUMN_GAP_THRESHOLD = 30  # puntos PDF; gaps reales son > 90

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

            # Estrategia 1: sección "Equipos Participantes"
            from_section = _extract_from_participantes_section(full_text)
            if from_section:
                teams.update(from_section)
            else:
                # Estrategia 2: gaps por columnas
                for page in pdf.pages:
                    teams.update(
                        _extract_teams_from_page(page, COLUMN_GAP_THRESHOLD)
                    )
    except Exception as e:  # noqa: BLE001
        print(f"  [rfef] Error parseando PDF: {e}")
        return []

    return sorted(t for t in teams if _looks_like_team_name(t))


def _extract_from_participantes_section(text: str) -> set[str]:
    """Estrategia 1: encuentra la sección 'Equipos Participantes' y extrae las
    líneas numeradas tipo `1.- Nombre del equipo (12345)`."""
    m = re.search(
        r"Equipos\s+Participantes\s*\n(.*?)(?:\nP[áa]gina|\nJornada|\Z)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return set()
    block = m.group(1)
    teams: set[str] = set()
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        mm = re.match(r"^\d+\s*[\.\)\-]+\s*(.+?)(?:\s*\(\d+\))?\s*$", line)
        if mm:
            teams.add(_clean_team_name(mm.group(1)))
    return teams


def _extract_teams_from_page(page, gap_threshold: float) -> set[str]:
    """Extrae los nombres de equipos de una página agrupando palabras por
    línea y partiendo cada línea por el gap más grande."""
    from collections import defaultdict

    lines: dict[int, list[dict]] = defaultdict(list)
    for word in page.extract_words():
        # Redondear `top` a entero para tolerar variaciones sub-pixel
        lines[round(word["top"])].append(word)

    teams: set[str] = set()
    for line_words in lines.values():
        line_words.sort(key=lambda w: w["x0"])
        if len(line_words) < 2:
            continue

        # Encontrar el gap horizontal más grande
        max_gap = 0.0
        split_idx = -1
        for i in range(len(line_words) - 1):
            cur_end = line_words[i]["x0"] + line_words[i]["width"]
            next_start = line_words[i + 1]["x0"]
            gap = next_start - cur_end
            if gap > max_gap:
                max_gap = gap
                split_idx = i

        if max_gap < gap_threshold:
            continue

        left = " ".join(w["text"] for w in line_words[: split_idx + 1])
        right = " ".join(w["text"] for w in line_words[split_idx + 1:])
        teams.add(_clean_team_name(left))
        teams.add(_clean_team_name(right))

    return teams


def _clean_team_name(raw: str) -> str:
    t = raw.strip()
    # Eliminar números de jornada o referencias al final (ej. "BARÇA  J1")
    t = re.sub(r"\s+J\d+$", "", t)
    # Normalizar "F.S." -> "FS" (deduplica variantes)
    t = re.sub(r"\bF\.S\.?\b", "FS", t)
    # Colapsar espacios
    t = re.sub(r"\s+", " ", t)
    return t


# Líneas que aparecen en cabeceras o pies de calendario y NO son equipos.
_BLACKLIST_PHRASES = {
    "real federacion espanola de futbol",
    "calendario",
    "temporada",
    "real federación española de fútbol",
}


def _looks_like_team_name(s: str) -> bool:
    if len(s) < 3 or len(s) > 80:
        return False
    if not re.search(r"[A-Za-zÀ-ÿ]", s):
        return False
    digits = sum(c.isdigit() for c in s)
    if digits > len(s) * 0.4:
        return False
    if _norm(s) in {_norm(p) for p in _BLACKLIST_PHRASES}:
        return False
    if s.upper() in {"JORNADA", "FECHA", "PARTIDO", "EQUIPO", "RFEF"}:
        return False
    return True


def _load_fallback() -> dict:
    """Lee data/rfef-fallback.json con listas hardcodeadas de equipos.

    Estructura esperada:
        {
          "divisions": {
            "rfef-primera-fs-masc": {
              "teams": [{"name": "...", "logoUrl": "..."}, ...]
            },
            ...
          }
        }
    """
    path = DATA_DIR / "rfef-fallback.json"
    if not path.exists():
        return {"divisions": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _group_from_web(div_id: str, comp_code: str, season_code: str):
    """Un grupo único, leído del enlace a la PNFG de la página de competición.

    Devuelve una lista de un `Group` o `[]`. Solo sirve para divisiones de un
    grupo —la página enlaza un calendario, no seis— y por eso el id es `g1`: es
    el que `discover_divisions` daría a un grupo único, y con él la división se
    publica plana, sin desplegable de grupo.

    Vale para lo que vale: recupera Primera Femenina y Segunda cuando el
    catálogo de la PNFG no contesta. En Segunda B y Segunda Femenina el enlace
    de la página apunta a la temporada pasada, así que el guard de
    `pnfg_group_for` lo descarta — que es justo lo que tiene que hacer.
    """
    from scrapers.rfef_discovery import Group

    slug = rfef_web.COMPETITION_SLUGS.get(div_id)
    if not slug:
        return []
    html = rfef_web.fetch_competition_html(slug)
    if not html:
        return []
    grupo = rfef_web.pnfg_group_for(html, comp_code, season_code)
    if not grupo:
        return []
    print(f"  [rfef-disc] {div_id}: grupo {grupo} recuperado del enlace a la "
          f"PNFG en rfef.es (el catálogo no contestó)")
    return [Group(id="g1", code=grupo, name=DIVISION_NAMES[div_id])]


def discover_divisions(season_code: str, *, session=None,
                       unknown: list[str] | None = None) -> list[dict] | None:
    """Pregunta a la PNFG qué divisiones tiene esta temporada y con qué códigos.

    Devuelve una cfg por división, con la misma forma que consumía la constante
    `DIVISIONS` de antes, más el grupo descubierto. Las competiciones que no
    casan con ninguna regla (juveniles, copas, selecciones) se descartan en
    silencio: eso es lo normal, no un fallo.

    Devuelve **None** si no se pudo ni consultar, que es distinto de una lista
    vacía ("la federación aún no ha publicado nada"). Ver `resolve_season`.
    """
    comps = list_competitions(season_code, session=session)
    if comps is None:
        return None
    print(f"[rfef-disc] {len(comps)} competiciones de fútbol sala en la temporada")

    by_id: dict[str, dict] = {}
    for comp in comps:
        clase, div_id, gender = classify_competition(comp.name)
        if clase == COMP_UNKNOWN:
            # Parece una liga de sala y no la reconocemos. Casi siempre
            # significa que la han rebautizado — pasó en 2026-27 con "Liga
            # Prime Futsal". Se denuncia en vez de tirarla en silencio.
            msg = (f"competición de sala sin reconocer: {comp.name!r} "
                   f"(comp {comp.code}). Si es una de nuestras divisiones, "
                   f"añade su nombre a DIVISION_RULES")
            print(f"  [rfef-disc] AVISO: {msg}")
            if unknown is not None:
                unknown.append(msg)
            continue
        if clase != COMP_LEAGUE:
            continue
        if div_id in by_id:
            # Dos competiciones casando con la misma división: quedarse con la
            # primera y avisar. Pasaría si RFEF crease "Segunda División FS
            # Masculino" y "Segunda División FS Masculina" a la vez.
            print(f"  [rfef-disc] AVISO: {comp.name!r} también casa con "
                  f"{div_id}, ya asignado a {by_id[div_id]['competition']['name']!r}")
            continue
        groups = list_groups(comp.code, session=session)
        if not groups:
            # Segundo intento por la web de la federación: su página de
            # competición enlaza el calendario en la PNFG con los tres códigos
            # dentro. Rescata a las divisiones de un grupo cuando el catálogo
            # —la llamada más frágil de la PNFG— se come el rate-limit.
            groups = _group_from_web(div_id, comp.code, season_code)
        if not groups:
            print(f"  [rfef-disc] {div_id}: sin grupos publicados todavía")
            continue
        # Fases (Apertura/Clausura) no son grupos: mismos equipos, y elegir no
        # significa nada. Se publica plana. Ver `rfef_discovery.is_phase`.
        #
        # LÍMITE CONOCIDO: el calendario se toma entonces de la primera fase, o
        # sea el Apertura. A partir de enero habrá que empalmar las dos para que
        # la J16 en adelante aparezca en el autorrelleno.
        phases = len(groups) > 1 and all(is_phase(g.name) for g in groups)
        if phases:
            print(f"  [rfef-disc] {div_id}: "
                  f"{[g.name for g in groups]} son fases, no grupos; se publica plana")
        cfg = {
            "id": div_id,
            "name": DIVISION_NAMES[div_id],
            "gender": gender,
            "competition": {"code": comp.code, "name": comp.name},
            # Una división con un solo grupo se publica plana: es cómo la ve la
            # entrenador (no hay nada que elegir) y cómo la esperan los equipos
            # que ya tienen `groupId` nulo.
            "flat": len(groups) == 1 or phases,
            "groups": groups,
            **LEGACY_PDF.get(div_id, {}),
        }
        by_id[div_id] = cfg
        print(f"  [rfef-disc] {div_id} <- {comp.name!r} "
              f"(comp {comp.code}, {len(groups)} grupo/s)")
        time.sleep(2)

    return [by_id[d] for d in DIVISION_ORDER if d in by_id]


def scrape(season: str, resolve_badges: bool = True,
           teams_only: bool = False) -> dict:
    """Devuelve la categoría RFEF lista para incluir en leagues.json.

    ## El orden importa y no es el de antes

    Antes se capturaban los equipos primero (de la clasificación) y el
    calendario después. Eso funcionaba en mitad de temporada y fallaba justo
    cuando hace falta: **la clasificación no existe hasta que se juega la J1**,
    así que en pretemporada las divisiones salían vacías y se rellenaban con el
    fallback del año pasado.

    Ahora el calendario va primero y es la fuente base de equipos. La
    clasificación sigue siendo preferente cuando responde, porque trae los
    escudos oficiales en la misma fila.

    Cascada por división/grupo:

    1. **Clasificación** — nombres canónicos + escudos. Solo desde la J1.
    2. **Calendario** — nombres desde los enfrentamientos. Desde el sorteo.
    3. **PDF oficial** de `rfef.es` (legacy).
    4. **Fallback curado** (`data/rfef-fallback.json`) — **solo si su temporada
       coincide con la pedida**. Ver más abajo.

    ## El guard de temporada

    El fallback y los códigos de competición son datos *de una temporada
    concreta*. Usarlos para otra es lo que produjo un JSON de 2026-27 lleno de
    equipos de 2025-26 sin una sola señal de error. Aquí:

    - Si no se puede resolver el `CodTemporada` de la temporada pedida, **no se
      publica nada**: se devuelve la categoría vacía con `seasonVerified: False`
      y `scrape.py` aborta el run sin escribir el fichero. Un run fallido deja
      el JSON anterior en su sitio; uno "exitoso" con datos viejos, no.
    - Si `data/rfef-fallback.json` declara otra temporada, se ignora entero.
    """
    warnings: list[str] = []

    fallback = _load_fallback()
    fb_season = fallback.get("season")
    if fb_season == season:
        fb_divisions = fallback.get("divisions", {})
    else:
        fb_divisions = {}
        msg = (f"data/rfef-fallback.json es de {fb_season!r} y se pidió "
               f"{season!r}: se ignora (no se rellenan divisiones con equipos "
               f"de otra temporada)")
        print(f"[rfef] AVISO: {msg}")
        warnings.append(msg)

    # Un `Fetcher` compartido para todo el descubrimiento: cuando una llamada se
    # come el rate-limit y consigue una JSESSIONID nueva, las siguientes
    # arrancan ya con la buena. El scraping pesado (`fetch_division_teams`,
    # `fetch_division_calendar`) sigue creando la suya, porque RFEF cierra la
    # conexión bajo carga y arrastrar una sesión rota propaga el fallo.
    session = Fetcher()

    def _nothing(msg: str, *, pending: bool) -> dict:
        """Categoría vacía. `pending` = la federación aún no lo ha publicado
        (esperable cada julio), frente a una avería de verdad."""
        print(f"[rfef] {'PENDIENTE' if pending else 'ERROR'}: {msg}")
        return {
            "id": "rfef", "name": "Liga Española", "source": "rfef.es",
            "divisions": [], "season": season, "seasonVerified": False,
            "seasonPending": pending, "warnings": warnings + [msg],
        }

    season_code, status = resolve_season(season, session=session)
    if status == SEASON_NOT_PUBLISHED:
        return _nothing(
            f"la PNFG todavía no ha creado la temporada {season}; no hay nada "
            f"que scrapear (normal hasta que la federación abre la temporada)",
            pending=True)
    if season_code is None:
        return _nothing(
            f"no se pudo consultar el CodTemporada de {season} en la PNFG "
            f"(rate-limit, red o cambio de HTML); no se publica nada de RFEF",
            pending=False)
    print(f"[rfef] CodTemporada de {season}: {season_code}")

    divisions_cfg = discover_divisions(season_code, session=session,
                                       unknown=warnings)
    if divisions_cfg is None:
        return _nothing(
            f"no se pudo consultar el catálogo de competiciones de {season}",
            pending=False)
    if not divisions_cfg:
        return _nothing(
            f"la PNFG no publica todavía ninguna división de fútbol sala "
            f"reconocible para {season}",
            pending=True)

    faltan = [d for d in DIVISION_ORDER if d not in {c["id"] for c in divisions_cfg}]
    if faltan:
        # No es un fallo: las masculinas de LNFS se publican semanas más tarde
        # que las femeninas. Pero tiene que constar, porque una división que
        # falta se ve igual que una división que se perdió.
        msg = f"sin publicar en la PNFG para {season}: {', '.join(faltan)}"
        print(f"[rfef] AVISO: {msg}")
        warnings.append(msg)

    # 1. Esqueleto: ids, nombres y grupos. Los equipos se rellenan al final,
    #    cuando ya se sabe qué dio el calendario.
    out_divisions: list[dict] = []
    for cfg in divisions_cfg:
        div: dict = {"id": cfg["id"], "name": cfg["name"],
                     "gender": cfg["gender"], "teams": []}
        if not cfg["flat"]:
            div["groups"] = [{"id": g.id, "name": g.name, "teams": []}
                             for g in cfg["groups"]]
        out_divisions.append(div)

    # 2. Calendarios (y con ellos, la lista de equipos de pretemporada).
    #
    # En modo `teams_only` se bajan solo las dos primeras jornadas: bastan para
    # deducir la plantilla y cuestan 2 peticiones por grupo en vez de ~30. Ese
    # calendario parcial NO se publica (se retira en el paso 4) — lo empalma
    # `scrape.py` con el que ya está publicado, que está completo.
    # La web pública va **antes** que el calendario y que los equipos: es la
    # única fuente que existe en pretemporada (la PNFG no tiene equipos hasta la
    # J1) y además descubre de qué PDF sale el calendario de cada grupo.
    web = fetch_web_sources(divisions_cfg, season, warnings)

    _attach_calendars(out_divisions, season, divisions_cfg, season_code,
                      max_jornadas=2 if teams_only else None, web=web)

    # 3. Equipos.
    _fill_teams(out_divisions, divisions_cfg, fb_divisions, season,
                resolve_badges, web=web, warnings=warnings)


    # 4. Fuera el calendario parcial. Publicarlo sería peor que no tenerlo: la
    #    app ofrecería un desplegable con dos jornadas y el entrenador no
    #    encontraría la suya.
    if teams_only:
        for div in out_divisions:
            div.pop("calendar", None)
            for g in div.get("groups", []):
                g.pop("calendar", None)

    # Grupos que se quedaron sin equipos y sin calendario: se caen del JSON.
    # La app ya los filtraría (`Division.nonEmptyGroups`), pero publicarlos
    # obliga a cada cliente a razonar sobre un grupo que no existe.
    for div in out_divisions:
        if "groups" in div:
            div["groups"] = [g for g in div["groups"]
                             if g.get("teams") or g.get("calendar")]

    if teams_only:
        print("[rfef] modo solo-equipos: el calendario se hereda del publicado")

    out_divisions = _with_placeholders(out_divisions)

    # Las que la PNFG no publica: última oportunidad por el PDF oficial.
    _fill_missing_from_pdf(out_divisions, season, resolve_badges, warnings,
                           teams_only)

    # El último paso antes de devolver nada: fuera todo lo que no se pueda
    # fechar. Va aquí y no junto a `_fill_teams` para que cubra también lo que
    # rellene cualquier vía que se añada después — que es justo como se coló el
    # PDF caducado de §6.49.
    _drop_unverified_teams(out_divisions, season, warnings)

    return {
        "id": "rfef",
        "name": "Liga Española",
        "source": "rfef.es",
        "divisions": out_divisions,
        "season": season,
        "seasonVerified": True,
        "seasonPending": False,
        "teamsOnly": teams_only,
        "warnings": warnings,
    }


def _pdf_declares_season(pdf_bytes: bytes, season: str) -> bool:
    """¿El PDF dice por dentro que es de esta temporada?

    Los calendarios de la RFEF imprimen la temporada en la portada
    ("Calendario / Liga Prime Futsal / 2026/2027"). Comprobarlo es más fuerte
    que fiarse del nombre del fichero: la ruta la elige quien sube el PDF y ya
    ha cambiado de formato una vez, mientras que la portada describe el
    contenido. Es la misma regla que aplica `rfef-fallback.json` con su campo
    `season`, y por el mismo motivo.

    Ante la duda **dice que no**: preferimos una división vacía y avisada a una
    llena de la temporada equivocada.
    """
    try:
        import pdfplumber
    except ImportError:
        return False
    a, b = season.split("-")
    wanted = {f"{a}/{b}", f"{a}-{b}", f"{a}/{b[-2:]}"}
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            head = " ".join((p.extract_text() or "") for p in pdf.pages[:2])
    except Exception:  # noqa: BLE001
        return False
    return any(w in head for w in wanted)


def _pdf_candidates(cfg: dict, season: str) -> list[str]:
    """URLs de PDF a probar, de más fiable a menos.

    El patrón clásico va primero porque lleva la temporada dentro de la URL: si
    existe, no hay ambigüedad posible.
    """
    urls = []
    if cfg.get("pdf_id"):
        urls.append(_pdf_url(cfg["pdf_id"], season))
    extra = (cfg.get("pdf_urls") or {}).get(season)
    if extra:
        urls.append(extra)
    return urls


def _fill_missing_from_pdf(
    out_divisions: list[dict],
    season: str,
    resolve_badges: bool,
    warnings: list[str],
    teams_only: bool,
) -> None:
    """Último recurso para las divisiones que la PNFG no publica todavía.

    Sin esto, el PDF oficial no se llega a intentar nunca para una división que
    no aparece en la PNFG: la cascada de `_teams_for` cuelga de una división
    *descubierta*. Y da la casualidad de que ese es justo el caso donde el PDF
    es la única fuente que hay — en 2026-27, la máxima categoría masculina.
    """
    for div in out_divisions:
        if div.get("teams") or div.get("groups"):
            continue
        cfg = LEGACY_PDF.get(div["id"])
        if not cfg:
            continue
        for url in _pdf_candidates(cfg, season):
            pdf = _download_pdf(url)
            if not pdf:
                continue
            if not _pdf_declares_season(pdf, season):
                msg = (f"{div['id']}: el PDF {url.rsplit('/', 1)[-1]} no declara "
                       f"la temporada {season} en su portada; se descarta")
                print(f"  [rfef] AVISO: {msg}")
                warnings.append(msg)
                continue
            names = _extract_teams_from_pdf(pdf)
            if not names:
                continue
            div["teams"] = _teams_from_names(names, resolve_badges=resolve_badges)
            # El PDF pasó por `_pdf_declares_season` unas líneas más arriba.
            div["teamsSource"] = SOURCE_PDF_LEGACY
            con_escudo = sum(1 for t in div["teams"] if t.get("logoUrl"))
            if not teams_only:
                calendar = _extract_calendar_from_pdf(pdf)
                if calendar:
                    div["calendar"] = calendar
            msg = (f"{div['id']}: sin datos en la PNFG; equipos tomados del PDF "
                   f"oficial ({len(names)} equipos, {con_escudo} con escudo). "
                   f"Los nombres pueden ir por detrás de los patrocinios hasta "
                   f"que arranque la clasificación")
            print(f"  [rfef] {msg}")
            warnings.append(msg)
            break


def _with_placeholders(out_divisions: list[dict]) -> list[dict]:
    """Completa la lista con las divisiones que la federación aún no ha abierto,
    vacías pero presentes, y en el orden canónico.

    **Por qué no basta con omitirlas.** Quitarlas del JSON parece lo coherente
    —no hay datos, no se publica nada— pero rompe algo que no se ve: el
    entrenador no puede *seleccionar* su división, así que su equipo se queda
    con `divisionId` nulo. Y el día que la federación publique, nada se engancha
    solo: tendría que acordarse de volver a editar el equipo. Con la división
    presente, elige ahora y el calendario, los rivales y las novedades aparecen
    solos en cuanto el scraper los encuentre.

    Publicarla vacía **no** reintroduce el bug de agosto de 2026: lo que aquel
    hacía era rellenarla con los equipos del año pasado. Aquí no hay ni un
    equipo, que es exactamente la verdad — y es además cómo se comportaba la app
    antes con las divisiones sin datos: desplegable visible, rivales a mano.
    """
    by_id = {d["id"]: d for d in out_divisions}
    out = []
    for div_id in DIVISION_ORDER:
        if div_id in by_id:
            out.append(by_id[div_id])
        else:
            out.append({
                "id": div_id,
                "name": DIVISION_NAMES[div_id],
                "gender": DIVISION_GENDER[div_id],
                "teams": [],
            })
    # Cualquier división descubierta que no esté en el orden canónico (no
    # deberia pasar: el casado se hace contra esa misma lista) va al final en
    # lugar de perderse.
    for d in out_divisions:
        if d["id"] not in DIVISION_ORDER:
            out.append(d)
    return out


def fetch_web_sources(
    divisions_cfg: list[dict], season: str, warnings: list[str],
) -> dict[str, dict]:
    """Lo que aporta la web pública de la RFEF, por división.

        {div_id: {"shorts": [nombre corto, ...],
                  "groups": {"g2": [nombre oficial, ...], ...}}}

    Es la fuente que **funciona en pretemporada**, que es cuando la app se usa
    para preparar la temporada y cuando la PNFG no tiene nada: su clasificación
    no existe hasta la J1. Ver la cabecera de `rfef_web`.

    Best-effort de principio a fin: si `rfef.es` no contesta, esto devuelve
    `{}` y la cascada de siempre sigue funcionando. Lo que **no** hace es
    rellenar con lo que sea — un PDF que no declare esta temporada se descarta.
    """
    out: dict[str, dict] = {}
    for cfg in divisions_cfg:
        div_id = cfg["id"]
        slug = rfef_web.COMPETITION_SLUGS.get(div_id)
        if not slug:
            continue
        html = rfef_web.fetch_competition_html(slug)
        shorts = [t.name for t in rfef_web.parse_teams(html)] if html else []
        rosters = rfef_web.fetch_rosters(slug, season, div_id) if html else []
        groups = {r.group_id: r.teams for r in rosters}
        # La URL del PDF que ganó, por grupo: el calendario tiene que salir del
        # **mismo** fichero que el plantel. Antes lo sacaba de
        # `groups_url_pattern`, que es la ruta sin temporada dentro.
        pdfs = {r.group_id: r.url for r in rosters if r.url}
        if shorts or groups:
            out[div_id] = {"shorts": shorts, "groups": groups, "pdfs": pdfs}
            print(f"  [rfef-web] {div_id}: {len(shorts)} equipos en la página, "
                  f"{len(groups)} grupo/s con PDF de {season}")

        # Un desajuste entre la página y los PDF significa que una de las dos
        # va por detrás. No se elige en silencio: se publica lo que digan los
        # PDF (que llevan la temporada dentro) y se avisa del descuadre.
        total_pdf = sum(len(v) for v in groups.values())
        if shorts and total_pdf and total_pdf != len(shorts):
            msg = (f"{div_id}: la página de rfef.es lista {len(shorts)} equipos "
                   f"y los PDF de calendario suman {total_pdf}; alguno de los "
                   f"dos va por detrás")
            print(f"  [rfef-web] AVISO: {msg}")
            warnings.append(msg)
    return out


def _fill_teams(
    out_divisions: list[dict],
    divisions_cfg: list[dict],
    fb_divisions: dict,
    season: str,
    resolve_badges: bool,
    web: dict[str, dict] | None = None,
    warnings: list[str] | None = None,
) -> None:
    """Rellena `teams` de cada división/grupo. Muta `out_divisions` in-place."""
    by_id = {d["id"]: d for d in out_divisions}
    web = web or {}

    for cfg in divisions_cfg:
        div = by_id.get(cfg["id"])
        if div is None:
            continue
        print(f"[rfef] Equipos de {cfg['name']}")
        fb_div = fb_divisions.get(cfg["id"], {})
        comp_code = cfg["competition"]["code"]
        web_div = web.get(cfg["id"], {})
        shorts = web_div.get("shorts", [])
        web_groups = web_div.get("groups", {})

        if cfg["flat"]:
            group = cfg["groups"][0]
            div["teams"], div["teamsSource"] = _teams_for(
                comp=comp_code, grupo=group.code,
                calendar=div.get("calendar", []),
                fb_teams=fb_div.get("teams", []),
                cfg=cfg, group_n=None, season=season,
                label=cfg["id"], resolve_badges=resolve_badges,
                web_names=_web_names_for(web_groups, None),
                web_shorts=shorts,
            )
            continue

        fb_groups = {g.get("id"): g for g in fb_div.get("groups", [])}
        out_groups = {g["id"]: g for g in div.get("groups", [])}
        for i, group in enumerate(cfg["groups"]):
            out_group = out_groups.get(group.id)
            if out_group is None:
                continue
            if i > 0:
                time.sleep(10)
            n_match = re.match(r"g(\d+)$", group.id)
            out_group["teams"], out_group["teamsSource"] = _teams_for(
                comp=comp_code, grupo=group.code,
                calendar=out_group.get("calendar", []),
                fb_teams=fb_groups.get(group.id, {}).get("teams", []),
                cfg=cfg, group_n=int(n_match.group(1)) if n_match else None,
                season=season,
                label=f"{cfg['id']}/{group.id}", resolve_badges=resolve_badges,
                web_names=_web_names_for(web_groups, group.id),
                web_shorts=shorts,
            )


def _web_names_for(web_groups: dict, group_id: str | None) -> list[str]:
    """Plantel oficial que la web da para este grupo.

    Si solo hay un plantel y viene sin grupo (`None`), vale para cualquier
    grupo de la división. Ese es el caso de Liga Prime: la PNFG la parte en
    "Torneo Apertura" y "Torneo Clausura", que son **fases con los mismos 16
    equipos**, no grupos territoriales.
    """
    if group_id in web_groups:
        return web_groups[group_id]
    if list(web_groups) == [None]:
        return web_groups[None]
    return []


def _drop_unverified_teams(out_divisions: list[dict], season: str,
                           warnings: list[str]) -> None:
    """Vacía los equipos cuya procedencia no permita afirmar la temporada.

    Es el cinturón, no el tirante: la cascada de `_teams_for` ya solo devuelve
    fuentes fechables. Esto existe porque las dos veces que este scraper publicó
    la liga del año pasado, lo hizo **sin un solo error** — un camino nuevo que
    nadie fechó, y el run terminando en verde. Un guard que recorre lo que se va
    a publicar, y no lo que se cree haber hecho, es lo único que caza eso.

    Vaciar y avisar, no abortar: si una división se queda sin fuente fechable, el
    resto del JSON sigue siendo bueno y la app degrada esa división al formulario
    manual, que es lo que hacía antes de existir el scraper.
    """
    for div in out_divisions:
        nodos = [(div["id"], div)] + [
            (f"{div['id']}/{g['id']}", g) for g in div.get("groups", [])]
        for label, nodo in nodos:
            if not nodo.get("teams"):
                continue
            fuente = nodo.get("teamsSource", SOURCE_NINGUNA)
            if fuente in VERIFIED_SOURCES:
                continue
            msg = (f"{label}: {len(nodo['teams'])} equipos descartados — su "
                   f"fuente ({fuente!r}) no permite afirmar que sean de "
                   f"{season}. Antes que publicar la temporada equivocada, la "
                   f"división se publica vacía")
            print(f"[rfef] AVISO: {msg}")
            warnings.append(msg)
            nodo["teams"] = []


def _teams_for(
    *,
    comp: str,
    grupo: str,
    calendar: list[dict],
    fb_teams: list[dict],
    cfg: dict,
    group_n: int | None,
    season: str,
    label: str,
    resolve_badges: bool,
    web_names: list[str] | None = None,
    web_shorts: list[str] | None = None,
) -> tuple[list[dict], str]:
    """La cascada de fuentes para un grupo concreto: `(equipos, procedencia)`.

    **La procedencia no es telemetría, es el guard.** `scrape()` se niega a
    publicar equipos cuya fuente no esté en `VERIFIED_SOURCES`, y de ahí sale la
    única garantía que se puede dar de verdad: que lo publicado es de la
    temporada pedida y no de la anterior. Ver `SOURCE_*`.
    """
    cal_names = teams_from_calendar(calendar)
    web_names = list(web_names or [])
    web_shorts = list(web_shorts or [])

    # 1. Clasificación: la única fuente que trae escudo oficial. Se le dan
    #    menos reintentos cuando ya tenemos los nombres por otra vía — ahí es
    #    un extra, no la respuesta, y agotar 225s de backoff por grupo en
    #    pretemporada solo sirve para que RFEF nos bloquee la IP.
    scraped = fetch_division_teams(
        comp, grupo, retries=1 if (cal_names or web_names) else 4)
    if scraped:
        print(f"  [rfef] {label}: {len(scraped)} equipos de la clasificación")
        return _with_short_names(_merge_clasificacion(
            fb_teams=fb_teams, scraped=scraped, resolve_badges=resolve_badges,
        ), web_shorts), SOURCE_CLASIFICACION

    # 2. Web pública de la RFEF: el PDF de calendario del grupo, con la
    #    temporada leída de su portada. Va por delante del calendario de la
    #    PNFG porque aquel no dice de qué temporada es —hay que fiarse del
    #    código de competición— y este sí.
    if web_names:
        print(f"  [rfef] {label}: {len(web_names)} equipos del PDF de rfef.es")
        return _with_short_names(
            _teams_from_names(web_names, resolve_badges=resolve_badges),
            web_shorts), SOURCE_PDF_RFEF

    # 3. Calendario de la PNFG: funciona desde que se sortea.
    if cal_names:
        print(f"  [rfef] {label}: {len(cal_names)} equipos del calendario")
        return _with_short_names(
            _teams_from_names(cal_names, resolve_badges=resolve_badges),
            web_shorts), SOURCE_CALENDARIO

    # 4. PDF oficial (legacy), con el mismo guard de temporada que usa
    #    `_fill_missing_from_pdf`. Antes esta rama no lo tenía, y es por donde
    #    entraba el PDF sin temporada en la URL de Segunda Femenina.
    team_names = _legacy_pdf_names(cfg, season, group_n, label)
    if team_names:
        print(f"  [rfef] {label}: {len(team_names)} equipos del PDF")

    # La lista de la página de competición **ya no es fuente de equipos**, y esa
    # es la decisión de fondo de este guard.
    #
    # Es la única de la cascada que no se puede fechar: la página no dice de qué
    # temporada es, así que publicar desde ella era publicar sin poder afirmar el
    # año — exactamente lo que este scraper existe para no volver a hacer. Sigue
    # aportando los nombres cortos (`web_shorts`), que es seguro: un corto solo
    # se le pega a un equipo que ya vino de una fuente fechada, y el emparejado
    # es 1:1.
    #
    # En la práctica no se pierde nada: las cinco divisiones tienen calendario o
    # PDF fechado. Lo que cambia es qué pasa el día que fallen — antes se
    # publicaba sin fecha y con un aviso que nadie iba a leer; ahora la división
    # sale vacía, que es la verdad.

    # 6. Fallback curado — ya viene vacío si es de otra temporada.
    teams = _merge_teams(
        fb_teams=fb_teams, scraped_names=team_names, resolve_badges=resolve_badges,
    )
    if not teams:
        print(f"  [rfef] {label}: sin equipos por ninguna vía")
        return [], SOURCE_NINGUNA
    # El PDF legacy manda sobre el fallback curado cuando ha dado nombres; si no,
    # lo publicado es el fallback, que trae su propia temporada dentro.
    fuente = SOURCE_PDF_LEGACY if team_names else SOURCE_FALLBACK
    return _with_short_names(teams, web_shorts), fuente


def _legacy_pdf_names(
    cfg: dict, season: str, group_n: int | None, label: str,
) -> list[str]:
    """Nombres del PDF oficial, **solo** si su portada declara esta temporada.

    El guard no es decorativo. `groups_url_pattern` apunta a
    `calendario_grupo_N_segunda_femenina_futbol_sala.pdf`, una ruta sin
    temporada dentro que la federación no ha vuelto a tocar desde agosto de
    2025: sin comprobar la portada, esta rama publica el año pasado.
    """
    urls: list[str] = []
    if group_n is not None and cfg.get("groups_url_pattern"):
        urls.append(cfg["groups_url_pattern"].format(n=group_n))
    else:
        urls.extend(_pdf_candidates(cfg, season))
    for url in urls:
        pdf = _download_pdf(url)
        if not pdf:
            continue
        if not _pdf_declares_season(pdf, season):
            print(f"  [rfef] {label}: descartado {url.rsplit('/', 1)[-1]} "
                  f"(no declara la temporada {season})")
            continue
        names = _extract_teams_from_pdf(pdf)
        if names:
            return names
    return []


def _with_short_names(teams: list[dict], shorts: list[str]) -> list[dict]:
    """Cambia el nombre publicado por el corto de rfef.es y guarda el oficial.

    La entrenadora ve "Burela FS" en el desplegable y en el marcador; el
    "REYCO Burela FS" del acta viaja como `officialName` para casar con el
    calendario y con los partidos que ya tenga guardados.

    Lo que no casa con confianza se queda con su nombre oficial. Es feo y es
    cierto, que en un histórico de partidos importa más: renombrar al rival por
    un parecido razonable le rompe las estadísticas contra ese equipo.
    """
    if not shorts or not teams:
        return teams
    pairs = rfef_web.pair_names(shorts, [t["name"] for t in teams])
    out = []
    for t in teams:
        short = pairs.get(t["name"])
        if short and rfef_web.norm(short) != rfef_web.norm(t["name"]):
            t = {**t, "name": short, "officialName": t["name"]}
        out.append(t)
    return out


def _teams_from_names(names: list[str], *, resolve_badges: bool) -> list[dict]:
    """Equipos a partir de nombres del calendario.

    Solo se resuelven escudos de fuentes **fiables** (override curado del
    maintainer y mapa oficial de `futsal.rfef.es`). No se busca en
    fuentes no oficiales: por la misma razón que documenta `_merge_clasificacion`, un
    placeholder genérico es mejor que el escudo equivocado, y aquí no hay una
    fila oficial que confirme nada.
    """
    teams = [{"name": n, "logoUrl": None} for n in names]
    if resolve_badges:
        for t in teams:
            t["logoUrl"] = resolve_logo_url(t["name"], trusted_only=True)
    else:
        for t in teams:
            t["logoUrl"] = lookup_override(t["name"])
    return teams


def _attach_calendars(
    out_divisions: list[dict],
    season: str,
    divisions_cfg: list[dict],
    season_code: str,
    max_jornadas: int | None = None,
    web: dict[str, dict] | None = None,
) -> None:
    """Añade `calendar` a cada división/grupo de `out_divisions`.

    Cascada por división:
    1. Endpoint `NFG_CmpJornada` con los códigos descubiertos. Fuente
       preferente: fechas, horas y `CodActa`.
    2. PDF oficial (`_extract_calendar_from_pdf`), legacy. Recupera jornada +
       fecha + enfrentamientos, sin hora.

    Muta `out_divisions` in-place. Best-effort para el *calendario*: si falla,
    la app cae al formulario manual. Pero ojo — desde este cambio el calendario
    es además la **fuente de equipos** en pretemporada, así que un fallo aquí ya
    no es solo perder el autorrelleno de jornada.

    `season_code` se pasa siempre resuelto (`scrape()` aborta si no lo está).
    Antes esto admitía None y seguía adelante "usando la temporada por defecto
    del servidor", que es la mitad del bug de agosto de 2026.
    """
    by_id = {d["id"]: d for d in out_divisions}
    pdf_cache: dict[str, bytes | None] = {}

    def _pdf_calendar_for(cfg: dict, group_n: int | None = None,
                          group_id: str | None = None) -> list[dict]:
        """Calendario del PDF oficial. `[]` si no hay PDF válido o el parse falla.

        El PDF que descubrió `rfef_web` manda: es el que ya se comprobó que
        declara esta temporada, y es el mismo del que salió el plantel. Las
        rutas escritas a mano se quedan como red de seguridad, y ahora **con el
        guard de temporada** — `groups_url_pattern` es justo la que sigue
        sirviendo el calendario de 2025-26.
        """
        if max_jornadas is not None:
            # En modo solo-equipos el calendario no se publica, así que
            # descargar y parsear un PDF de 30 jornadas no aporta nada.
            return []

        urls: list[str] = []
        discovered = (web or {}).get(cfg["id"], {}).get("pdfs", {})
        for key in (group_id, None):
            if key in discovered:
                urls.append(discovered[key])
                break
        if group_n is not None and cfg.get("groups_url_pattern"):
            urls.append(cfg["groups_url_pattern"].format(n=group_n))
        elif group_id is None and group_n is None:
            # Solo para divisiones planas: el PDF unificado de una división por
            # grupos traería el calendario de otro grupo.
            urls.extend(_pdf_candidates(cfg, season))

        for url in urls:
            if url not in pdf_cache:
                pdf_cache[url] = _download_pdf(url)
            pdf = pdf_cache[url]
            if not pdf:
                continue
            if not _pdf_declares_season(pdf, season):
                print(f"  [rfef-cal] {cfg['id']}: descartado "
                      f"{url.rsplit('/', 1)[-1]} (no declara {season})")
                continue
            calendar = _extract_calendar_from_pdf(pdf)
            if not calendar:
                continue
            roster = (web or {}).get(cfg["id"], {}).get("groups", {}).get(group_id)
            if _calendar_is_sane(calendar, roster, label=f"{cfg['id']}/{group_id}"):
                return calendar
        return []

    for cfg in divisions_cfg:
        div = by_id.get(cfg["id"])
        if div is None:
            continue
        print(f"[rfef-cal] Calendario de {cfg['name']}")
        comp_code = cfg["competition"]["code"]

        if cfg["flat"]:
            group = cfg["groups"][0]
            calendar = fetch_division_calendar(
                comp_code, group.code, temporada_code=season_code,
                max_jornadas=max_jornadas,
            )
            if not calendar:
                calendar = _pdf_calendar_for(cfg)
                if calendar:
                    print(f"  [rfef-cal] {cfg['id']}: "
                          f"fallback PDF -> {len(calendar)} jornadas")
            if calendar:
                _merge_acta_cache(calendar, comp_code, group.code, label=cfg["id"])
                div["calendar"] = calendar
            time.sleep(10)
            continue

        groups = {g["id"]: g for g in div.get("groups", [])}
        for group in cfg["groups"]:
            grp = groups.get(group.id)
            if grp is None:
                continue
            calendar = fetch_division_calendar(
                comp_code, group.code, temporada_code=season_code,
                max_jornadas=max_jornadas,
            )
            if not calendar:
                n_match = re.match(r"g(\d+)$", group.id)
                calendar = _pdf_calendar_for(
                    cfg,
                    group_n=int(n_match.group(1)) if n_match else None,
                    group_id=group.id,
                )
                if calendar:
                    print(f"  [rfef-cal] {cfg['id']}/{group.id}: "
                          f"fallback PDF -> {len(calendar)} jornadas")
            if calendar:
                _merge_acta_cache(calendar, comp_code, group.code,
                                  label=f"{cfg['id']}/{group.id}")
                grp["calendar"] = calendar
            time.sleep(10)


def _merge_acta_cache(
    calendar: list[dict],
    comp: str | int,
    grupo: str | int,
    *,
    label: str,
) -> None:
    """Funde el calendario fresco con la caché persistente de actaUrls.

    - Partidos con `actaUrl` recién extraído → se almacenan en la caché para
      próximos runs (clave: comp|grupo|J{jornada}|home_norm|away_norm).
    - Partidos sin `actaUrl` → se busca en la caché por la misma clave. Si
      hay hit, se rellena. Resultado: cobertura monotónicamente creciente
      aunque RFEF rate-limite en un run concreto.
    """
    stored = 0
    recovered = 0
    for j in calendar:
        jn = j.get("jornada")
        if not isinstance(jn, int):
            continue
        for m in j.get("matches", []):
            home = m.get("home") or ""
            away = m.get("away") or ""
            if not home or not away:
                continue
            cur = m.get("actaUrl")
            if cur:
                calendar_cache.store(comp, grupo, jn, home, away, cur)
                stored += 1
            else:
                cached = calendar_cache.lookup(comp, grupo, jn, home, away)
                if cached:
                    m["actaUrl"] = cached
                    recovered += 1
    if stored or recovered:
        print(f"  [rfef-cal] {label}: acta-cache fresh={stored} recovered={recovered}")


def _merge_clasificacion(
    *,
    fb_teams: list[dict],
    scraped: list[ScrapedTeam],
    resolve_badges: bool,
) -> list[dict]:
    """Construye la lista de equipos a partir de la clasificación scrapeada.

    Reglas:
    - La clasificación es **completamente autoritativa** cuando devuelve
      datos. El fallback (`fb_teams`) se ignora en este camino: añadirlo
      genera duplicados por cambios de patrocinador/temporada (p.ej. un
      equipo que el fallback llama "Real Betis Futsal" y la clasificación
      "Real Betis Tedi" aparecería dos veces). Cuando la clasificación
      falla, el caller cae al camino legacy que sí usa fallback.
    - **No hay resolución automática fuera de las fuentes oficiales**: se
      prefiere `null` (la app muestra placeholder genérico) a un escudo erróneo
      (kit graphic, logo de patrocinador, etc.). Las dos fuentes que sí lo
      hacían —Wikipedia y DuckDuckGo— se retiraron por `IP-004`. Sí se aplica
      la allowlist curada de `data/badges-overrides.json` cuando el maintainer
      lo ha indicado expresamente — gana incluso sobre el escudo de la
      clasificación.
    """
    _ = resolve_badges  # ignorado a propósito en el camino de clasificación
    _ = fb_teams  # autoritativo = clasificación; el fallback no aporta aquí
    teams = [{"name": t.name, "logoUrl": t.logo_url} for t in scraped]
    for t in teams:
        override = lookup_override(t["name"])
        if override:
            t["logoUrl"] = override
    return teams


def _merge_teams(
    *,
    fb_teams: list[dict],
    scraped_names: list[str],
    resolve_badges: bool,
) -> list[dict]:
    """Merge dedup-por-normalizado entre fallback y nombres extraídos del PDF.
    Resuelve los escudos que falten contra las fuentes oficiales (allowlist
    curada, portal y caché) si `resolve_badges` está activo."""
    if scraped_names:
        seen = {_norm(t["name"]) for t in fb_teams}
        teams = list(fb_teams)
        for name in scraped_names:
            key = _norm(name)
            if key in seen:
                continue
            seen.add(key)
            teams.append({"name": name, "logoUrl": None})
    else:
        teams = list(fb_teams)

    if resolve_badges:
        for t in teams:
            if not t.get("logoUrl"):
                t["logoUrl"] = resolve_logo_url(t["name"])
    return teams


def _norm(name: str) -> str:
    # Normalizar acentos (NFKD descompone á -> a + diacrítico) y quedarse
    # solo con letras/dígitos en minúscula. Permite que "Peñíscola" y
    # "Peniscola" colapsen a la misma clave.
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", s.lower())
