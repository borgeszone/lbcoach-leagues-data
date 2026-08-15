#!/usr/bin/env python3
"""Genera output/leagues.json combinando scraper RFEF + datos manuales FCF.

Uso:
    python scrape.py [--season 2025-2026] [--no-badges]

El JSON resultante se publica en gh-pages via GitHub Actions y la app Flutter
lo descarga al crear/editar un equipo para autorrellenar la lista de rivales
con sus escudos.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from scrapers import calendar_cache, fcf, logo_resolver, rfef, rfef_shields

ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"


def current_season() -> str:
    """Devuelve la temporada vigente en formato `YYYY-YYYY` según la fecha.

    Temporada española de fútbol sala: arranca en julio/agosto y termina
    en junio. Por tanto:
      - meses 7-12 (jul-dic): `{year}-{year+1}` (temporada que acaba de empezar)
      - meses 1-6  (ene-jun): `{year-1}-{year}` (temporada que está acabando)
    """
    today = date.today()
    if today.month >= 7:
        return f"{today.year}-{today.year + 1}"
    return f"{today.year - 1}-{today.year}"


def load_notices() -> dict:
    """Lee data/notices-manual.json (novedades/comunicados de federación).

    Estructura: {"categories": {<catId>: [notice, ...]},
                 "divisions":  {<divId>: [notice, ...]}}.
    Devuelve {} si el fichero no existe.
    """
    path = ROOT / "data" / "notices-manual.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def apply_notices(categories: list[dict], notices: dict) -> None:
    """Inyecta las novedades manuales en las categorías/divisiones que coincidan
    por id. La app (LeagueData.noticesFor) lee `notices` a nivel de categoría y
    de división. Modifica `categories` in-place.
    """
    cat_notices = notices.get("categories", {})
    div_notices = notices.get("divisions", {})
    for cat in categories:
        cid = cat.get("id")
        if cid in cat_notices:
            cat["notices"] = cat_notices[cid]
        for div in cat.get("divisions", []):
            did = div.get("id")
            if did in div_notices:
                div["notices"] = div_notices[did]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", default=None,
                        help="Temporada en formato YYYY-YYYY (default: auto-detectada según la fecha)")
    parser.add_argument("--no-badges", action="store_true",
                        help="Omite la resolución de escudos via Wikipedia")
    args = parser.parse_args()

    season = args.season or current_season()
    OUTPUT_DIR.mkdir(exist_ok=True)

    print(f"[scrape] Generando leagues.json para temporada {season}")

    # Pre-poblar el mapa de escudos oficiales RFEF (extraídos de futsal.rfef.es)
    # Esto da escudo a casi todos los clubes RFEF sin depender de Wikipedia.
    if not args.no_badges:
        shields = rfef_shields.fetch_shield_map()
        print(f"[rfef-shields] {len(shields)} escudos oficiales descubiertos")
        logo_resolver.inject_rfef_shields(shields)

    rfef_cat = rfef.scrape(season=season, resolve_badges=not args.no_badges)
    fcf_cat = fcf.load_manual()

    # ── Guard de temporada ───────────────────────────────────────────────
    #
    # `version` se estampa con la temporada que se ha *pedido*, y esa es la
    # cadena que la app y el panel enseñan. Publicarla sobre datos que nadie ha
    # verificado contra esa temporada es cómo el JSON del 10 de agosto de 2026
    # acabó diciendo "2026-2027" con los equipos y el calendario de 2025-26.
    #
    # Se aborta **sin escribir el fichero**: el paso de publicación del workflow
    # no llega a ejecutarse y gh-pages conserva el JSON anterior. Viejo pero
    # coherente es mejor que nuevo pero falso, y el fallo del run manda el email
    # de Actions, que es la única alerta que hay.
    #
    # Se sale con 0 o con 1 según **de quién** sea el problema, y esa distinción
    # existe para que el email de fallo de Actions siga significando algo. Cada
    # julio hay unas semanas en que la temporada ya cambió en el calendario pero
    # la federación aún no la ha abierto: si eso mandara un fallo diario, en dos
    # veranos nadie mira esos emails — y son también la alerta del keep-alive de
    # Supabase (§10 de CLAUDE.md).
    if not rfef_cat.get("seasonVerified"):
        pending = rfef_cat.get("seasonPending", False)
        print()
        if pending:
            print(f"[scrape] SIN PUBLICAR: la federación todavía no ha abierto "
                  f"{season}.")
        else:
            print(f"[scrape] ABORTADO: no se ha podido verificar RFEF contra "
                  f"{season}.")
        for w in rfef_cat.get("warnings", []):
            print(f"  - {w}")
        print("[scrape] No se escribe output/leagues.json; se conserva el publicado.")
        return 0 if pending else 1

    categories = [rfef_cat, fcf_cat]

    # Inyectar novedades/comunicados de federación (data/notices-manual.json).
    apply_notices(categories, load_notices())

    # `warnings` es de diagnóstico y no lo consume la app: sube al nivel raíz
    # para que el panel pueda enseñar "faltan las masculinas" sin tener que
    # deducirlo de un recuento de equipos a cero, que se parece demasiado a un
    # scraper roto.
    warnings = [f"{c['id']}: {w}" for c in categories for w in c.get("warnings", [])]
    for c in categories:
        c.pop("warnings", None)
        c.pop("seasonVerified", None)
        c.pop("seasonPending", None)

    payload = {
        "version": season,
        "lastUpdated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warnings": warnings,
        "categories": categories,
    }

    out = OUTPUT_DIR / "leagues.json"
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Persistir cachés que sobreviven entre runs: escudos (descubiertos por
    # Wikipedia/DDG) y actaUrls (descubiertos vía NFG_CmpJornada). Ambas son
    # acumulativas — una entrada cacheada solo se borra a mano si deja de
    # funcionar.
    logo_resolver.save_cache()
    calendar_cache.save_cache()

    def _count_teams(cat: dict) -> int:
        n = 0
        for d in cat.get("divisions", []):
            n += len(d.get("teams", []))
            for g in d.get("groups", []) or []:
                n += len(g.get("teams", []))
        return n

    print(
        f"[scrape] OK -> {out} "
        f"({len(rfef_cat['divisions'])} div RFEF / {_count_teams(rfef_cat)} equipos, "
        f"{len(fcf_cat['divisions'])} div FCF / {_count_teams(fcf_cat)} equipos)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
