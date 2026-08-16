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

from scrapers import calendar_cache
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
    "rfef-primera-fs-masc": {"pdf_id": "1Div_Sala"},
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
    JORNADA_HEADER_RE = re.compile(
        r"^Jornada\s+(\d+)\s*(?:\((\d{1,2}/\d{1,2}/\d{2,4})\))?",
        re.IGNORECASE,
    )

    jornadas: dict[int, dict] = {}
    current_jornada: int | None = None

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                for line_words in _group_words_by_line(page):
                    line_text = " ".join(w["text"] for w in line_words).strip()

                    # 1. ¿Cabecera de jornada?
                    mm = JORNADA_HEADER_RE.match(line_text)
                    if mm:
                        current_jornada = int(mm.group(1))
                        date = _normalize_date(mm.group(2)) if mm.group(2) else None
                        if current_jornada not in jornadas:
                            jornadas[current_jornada] = {
                                "jornada": current_jornada,
                                "date": date,
                                "matches": [],
                            }
                        elif date and not jornadas[current_jornada].get("date"):
                            jornadas[current_jornada]["date"] = date
                        continue

                    # 2. ¿Línea de partido?
                    if current_jornada is None:
                        continue
                    pair = _split_match_line(line_words, COLUMN_GAP_THRESHOLD)
                    if pair is None:
                        continue
                    home, away = pair
                    if not (_looks_like_team_name(home) and _looks_like_team_name(away)):
                        continue
                    jornadas[current_jornada]["matches"].append({
                        "home": home,
                        "away": away,
                    })
    except Exception as e:  # noqa: BLE001
        print(f"  [rfef] Error parseando calendario: {e}")
        return []

    return [jornadas[k] for k in sorted(jornadas)]


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
            print(f"  [rfef-disc] {div_id}: sin grupos publicados todavía")
            continue
        cfg = {
            "id": div_id,
            "name": DIVISION_NAMES[div_id],
            "gender": gender,
            "competition": {"code": comp.code, "name": comp.name},
            # Una división con un solo grupo se publica plana: es cómo la ve la
            # entrenador (no hay nada que elegir) y cómo la esperan los equipos
            # que ya tienen `groupId` nulo.
            "flat": len(groups) == 1,
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
    _attach_calendars(out_divisions, season, divisions_cfg, season_code,
                      max_jornadas=2 if teams_only else None)

    # 3. Equipos.
    _fill_teams(out_divisions, divisions_cfg, fb_divisions, season, resolve_badges)

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


def _fill_teams(
    out_divisions: list[dict],
    divisions_cfg: list[dict],
    fb_divisions: dict,
    season: str,
    resolve_badges: bool,
) -> None:
    """Rellena `teams` de cada división/grupo. Muta `out_divisions` in-place."""
    by_id = {d["id"]: d for d in out_divisions}

    for cfg in divisions_cfg:
        div = by_id.get(cfg["id"])
        if div is None:
            continue
        print(f"[rfef] Equipos de {cfg['name']}")
        fb_div = fb_divisions.get(cfg["id"], {})
        comp_code = cfg["competition"]["code"]

        if cfg["flat"]:
            group = cfg["groups"][0]
            div["teams"] = _teams_for(
                comp=comp_code, grupo=group.code,
                calendar=div.get("calendar", []),
                fb_teams=fb_div.get("teams", []),
                cfg=cfg, group_n=None, season=season,
                label=cfg["id"], resolve_badges=resolve_badges,
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
            out_group["teams"] = _teams_for(
                comp=comp_code, grupo=group.code,
                calendar=out_group.get("calendar", []),
                fb_teams=fb_groups.get(group.id, {}).get("teams", []),
                cfg=cfg, group_n=int(n_match.group(1)) if n_match else None,
                season=season,
                label=f"{cfg['id']}/{group.id}", resolve_badges=resolve_badges,
            )


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
) -> list[dict]:
    """La cascada de fuentes para un grupo concreto. Ver `scrape()`."""
    cal_names = teams_from_calendar(calendar)

    # 1. Clasificación: la única fuente que trae escudo oficial. Se le dan
    #    menos reintentos cuando el calendario ya nos ha dado los nombres —
    #    ahí es un extra, no la respuesta, y agotar 225s de backoff por grupo
    #    en pretemporada solo sirve para que RFEF nos bloquee la IP.
    scraped = fetch_division_teams(comp, grupo, retries=1 if cal_names else 4)
    if scraped:
        print(f"  [rfef] {label}: {len(scraped)} equipos de la clasificación")
        return _merge_clasificacion(
            fb_teams=fb_teams, scraped=scraped, resolve_badges=resolve_badges,
        )

    # 2. Calendario: funciona desde que se sortea, que es lo que importa en
    #    agosto.
    if cal_names:
        print(f"  [rfef] {label}: {len(cal_names)} equipos del calendario")
        return _teams_from_names(cal_names, resolve_badges=resolve_badges)

    # 3. PDF oficial (legacy).
    team_names: list[str] = []
    if group_n is not None and cfg.get("groups_url_pattern"):
        pdf = _download_pdf(cfg["groups_url_pattern"].format(n=group_n))
        if pdf:
            team_names = _extract_teams_from_pdf(pdf)
    elif cfg.get("pdf_id"):
        pdf = _download_pdf(_pdf_url(cfg["pdf_id"], season))
        if pdf:
            team_names = _extract_teams_from_pdf(pdf)
    if team_names:
        print(f"  [rfef] {label}: {len(team_names)} equipos del PDF")

    # 4. Fallback curado — ya viene vacío si es de otra temporada.
    teams = _merge_teams(
        fb_teams=fb_teams, scraped_names=team_names, resolve_badges=resolve_badges,
    )
    if not teams:
        print(f"  [rfef] {label}: sin equipos por ninguna vía")
    return teams


def _teams_from_names(names: list[str], *, resolve_badges: bool) -> list[dict]:
    """Equipos a partir de nombres del calendario.

    Solo se resuelven escudos de fuentes **fiables** (override curado del
    maintainer y mapa oficial de `futsal.rfef.es`). No se busca en
    Wikipedia/DDG: por la misma razón que documenta `_merge_clasificacion`, un
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

    def _pdf_calendar_for(cfg: dict, group_n: int | None = None) -> list[dict]:
        """Descarga el PDF oficial y extrae el calendario por jornadas. `[]` si
        no hay PDF descargable o el parse falla."""
        if max_jornadas is not None:
            # En modo solo-equipos el calendario no se publica, así que
            # descargar y parsear un PDF de 30 jornadas no aporta nada.
            return []
        if group_n is not None:
            pattern = cfg.get("groups_url_pattern")
            if not pattern:
                return []
            url = pattern.format(n=group_n)
        else:
            pdf_id = cfg.get("pdf_id")
            if not pdf_id:
                return []
            url = _pdf_url(pdf_id, season)
        if url not in pdf_cache:
            pdf_cache[url] = _download_pdf(url)
        pdf = pdf_cache[url]
        return _extract_calendar_from_pdf(pdf) if pdf else []

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
                if n_match:
                    calendar = _pdf_calendar_for(cfg, group_n=int(n_match.group(1)))
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
    - **NO** se hace resolución automática vía Wikipedia/DDG: prefiero
      `null` (la app muestra placeholder genérico) que un escudo erróneo
      (kit graphic, logo de patrocinador, etc.). Sí se aplica el override
      curado de `data/badges-overrides.json` cuando el maintainer lo ha
      indicado expresamente — gana incluso sobre el escudo de la
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
    Resuelve escudos faltantes via Wikipedia si `resolve_badges` está activo."""
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
