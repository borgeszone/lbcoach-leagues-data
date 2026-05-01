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
from datetime import datetime, timezone
from pathlib import Path

from scrapers import fcf, rfef

ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"
DEFAULT_SEASON = "2025-2026"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", default=DEFAULT_SEASON,
                        help="Temporada en formato YYYY-YYYY")
    parser.add_argument("--no-badges", action="store_true",
                        help="Omite la resolución de escudos via Wikipedia")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)

    print(f"[scrape] Generando leagues.json para temporada {args.season}")

    rfef_cat = rfef.scrape(season=args.season, resolve_badges=not args.no_badges)
    fcf_cat = fcf.load_manual()

    payload = {
        "version": args.season,
        "lastUpdated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "categories": [rfef_cat, fcf_cat],
    }

    out = OUTPUT_DIR / "leagues.json"
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

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
