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

from scrapers import fcf, logo_resolver, rfef, rfef_shields

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

    payload = {
        "version": season,
        "lastUpdated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "categories": [rfef_cat, fcf_cat],
    }

    out = OUTPUT_DIR / "leagues.json"
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Persistir caché de escudos para que el siguiente run arranque caliente.
    logo_resolver.save_cache()

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
