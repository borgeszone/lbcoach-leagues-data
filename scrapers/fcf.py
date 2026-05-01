"""Loader para datos de la FCF (Federació Catalana de Futbol).

A diferencia de RFEF, la FCF no tiene PDFs estables ni una API navegable. Su
web exige selección interactiva de temporada → disciplina → grupo. Mantener
un scraper para cientos de grupos territoriales no compensa: en su lugar, se
mantiene a mano `data/fcf-manual.json` una vez al año (~30 min) y este módulo
simplemente lo lee y devuelve.

Para las divisiones que el usuario no rellene a mano, los equipos quedan como
una lista vacía y la app permite añadir rivales manualmente como hasta ahora.
"""
from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"


def load_manual() -> dict:
    path = DATA_DIR / "fcf-manual.json"
    if not path.exists():
        return {
            "id": "fcf",
            "name": "Liga Catalana",
            "source": "fcf.cat (manual)",
            "divisions": [],
        }

    raw = json.loads(path.read_text(encoding="utf-8"))

    # Una división puede tener `teams` directamente, o `groups` (para
    # divisiones por zona territorial). Pasamos lo que haya.
    divisions = []
    for d in raw.get("divisions", []):
        out = {
            "id": d["id"],
            "name": d["name"],
            "gender": d.get("gender", "masculino"),
            "teams": [
                {"name": t["name"], "logoUrl": t.get("logoUrl")}
                for t in d.get("teams", [])
            ],
        }
        groups = d.get("groups")
        if groups:
            out["groups"] = [
                {
                    "id": g["id"],
                    "name": g["name"],
                    "teams": [
                        {"name": t["name"], "logoUrl": t.get("logoUrl")}
                        for t in g.get("teams", [])
                    ],
                }
                for g in groups
            ]
        divisions.append(out)

    return {
        "id": "fcf",
        "name": raw.get("name", "Liga Catalana"),
        "source": "fcf.cat (manual)",
        "divisions": divisions,
    }
