"""Scraper de RFEF (Real Federación Española de Fútbol).

Estrategia:
1. Para cada división de fútbol sala configurada, intenta descargar el PDF
   oficial del calendario desde rfef.es y extraer los equipos con pdfplumber.
2. Si el download o el parseo falla, cae al fallback hardcodeado en
   data/rfef-fallback.json.
3. Los escudos se resuelven via scrapers.badges (Wikipedia Commons).

URL pattern observado (estable entre temporadas):
    https://rfef.es/sites/default/files/{YEAR}-07/Calendario_{COMP}_{SEASON}.pdf

Donde:
    YEAR = primer año de la temporada (2025-2026 -> 2025)
    COMP = identificador de la competición ("1Div_Sala", "2Div_Sala", etc.)
    SEASON = "2025-2026"
"""
from __future__ import annotations

import io
import json
import re
import unicodedata
from pathlib import Path
from typing import Iterable

import requests

from scrapers.logo_resolver import resolve_logo_url

DATA_DIR = Path(__file__).parent.parent / "data"

# Configuración de divisiones RFEF.
#
# Una división puede ser:
#   - **Unificada**: tiene `pdf_id`. Se descarga un único PDF de
#     `Calendario_{pdf_id}_{season}.pdf` y se extrae la lista de equipos.
#   - **Por grupos**: tiene `groups_url_pattern`. El scraper prueba grupos
#     1, 2, 3, ... hasta recibir 404, y construye la lista de grupos.
#     En el JSON resultante, la división tendrá un campo `groups` en lugar
#     (o además) de `teams`.
#   - **Manual**: ni `pdf_id` ni `groups_url_pattern`. Se usa solo el fallback.
DIVISIONS = [
    {
        "id": "rfef-primera-fs-masc",
        "name": "Primera División FS",
        "gender": "masculino",
        "pdf_id": "1Div_Sala",
    },
    {
        "id": "rfef-segunda-fs-masc",
        "name": "Segunda División FS",
        "gender": "masculino",
        "pdf_id": "2Div_Sala",
    },
    {
        "id": "rfef-primera-fs-fem",
        "name": "Primera División FS Femenina",
        "gender": "femenino",
        "pdf_id": "1DivFem_Sala",
    },
    {
        # 2ª División Femenina se organiza por grupos territoriales con URLs
        # tipo `calendario_grupo_N_segunda_femenina_futbol_sala.pdf` en la
        # raíz de `sites/default/files/`. El scraper prueba N=1..max_groups
        # hasta encontrar 404.
        "id": "rfef-segunda-fs-fem",
        "name": "Segunda División FS Femenina",
        "gender": "femenino",
        "groups_url_pattern":
            "https://rfef.es/sites/default/files/"
            "calendario_grupo_{n}_segunda_femenina_futbol_sala.pdf",
        "max_groups": 10,
    },
]


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


def scrape(season: str, resolve_badges: bool = True) -> dict:
    """Devuelve la categoría RFEF lista para incluir en leagues.json."""
    fallback = _load_fallback()
    fb_divisions = fallback.get("divisions", {})

    out_divisions = []
    for div_cfg in DIVISIONS:
        div_id = div_cfg["id"]
        print(f"[rfef] Procesando {div_cfg['name']}")
        fb_div = fb_divisions.get(div_id, {})

        if div_cfg.get("groups_url_pattern"):
            # División con grupos territoriales
            groups = _scrape_groups(
                pattern=div_cfg["groups_url_pattern"],
                max_groups=div_cfg.get("max_groups", 10),
                fb_groups=fb_div.get("groups", []),
                resolve_badges=resolve_badges,
            )
            out_divisions.append({
                "id": div_id,
                "name": div_cfg["name"],
                "gender": div_cfg["gender"],
                "teams": [],  # vacío si tiene grupos
                "groups": groups,
            })
        else:
            # División unificada (un único PDF o solo fallback)
            team_names: list[str] = []
            if div_cfg.get("pdf_id"):
                url = _pdf_url(div_cfg["pdf_id"], season)
                pdf = _download_pdf(url)
                if pdf:
                    team_names = _extract_teams_from_pdf(pdf)
                    if team_names:
                        print(f"  [rfef] {len(team_names)} equipos extraídos del PDF")

            teams_payload = _merge_teams(
                fb_teams=fb_div.get("teams", []),
                scraped_names=team_names,
                resolve_badges=resolve_badges,
            )
            out_divisions.append({
                "id": div_id,
                "name": div_cfg["name"],
                "gender": div_cfg["gender"],
                "teams": teams_payload,
            })

    return {
        "id": "rfef",
        "name": "Liga Española",
        "source": "rfef.es",
        "divisions": out_divisions,
    }


def _scrape_groups(
    *,
    pattern: str,
    max_groups: int,
    fb_groups: list[dict],
    resolve_badges: bool,
) -> list[dict]:
    """Itera grupos N=1..max_groups descargando el PDF de cada uno y construye
    la lista [{id, name, teams}, ...]. Para en el primer 404 consecutivo."""
    fb_by_id = {g.get("id", f"g{i + 1}"): g for i, g in enumerate(fb_groups)}
    groups_out: list[dict] = []

    for n in range(1, max_groups + 1):
        url = pattern.format(n=n)
        pdf = _download_pdf(url)
        if pdf is None:
            # Si ya tenemos al menos un grupo, asumimos que se acabaron
            if groups_out:
                break
            continue

        team_names = _extract_teams_from_pdf(pdf)
        print(f"  [rfef] Grupo {n}: {len(team_names)} equipos extraídos")

        gid = f"g{n}"
        fb_group = fb_by_id.get(gid, {})
        teams_payload = _merge_teams(
            fb_teams=fb_group.get("teams", []),
            scraped_names=team_names,
            resolve_badges=resolve_badges,
        )
        groups_out.append({
            "id": gid,
            "name": f"Grupo {n}",
            "teams": teams_payload,
        })

    # Añadir grupos del fallback que el scraper no haya cubierto
    scraped_ids = {g["id"] for g in groups_out}
    for gid, fb_g in fb_by_id.items():
        if gid not in scraped_ids:
            groups_out.append({
                "id": gid,
                "name": fb_g.get("name", gid),
                "teams": list(fb_g.get("teams", [])),
            })

    return groups_out


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
