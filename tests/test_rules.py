"""Inyección del reglamento de la temporada en el JSON publicado.

Lo que se protege aquí es **el filtro por temporada**. `data/rules-manual.json`
se mantiene a mano y guarda también los reglamentos de años anteriores; si se
publicaran todos, el JSON de 2026-27 saldría con el documento de 2025-26 dentro
y la app avisaría del reglamento equivocado con la etiqueta de temporada
equivocada — que es, letra por letra, el bug que costó descubrir §6.32 con los
equipos.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scrape import apply_rules  # noqa: E402


def _categories():
    return [
        {
            "id": "rfef",
            "divisions": [
                {"id": "rfef-primera-fs"},
                {"id": "rfef-segunda-fs-fem"},
            ],
        },
        {"id": "fcf", "divisions": []},
    ]


RULES = {
    "categories": {
        "rfef": {
            "season": "2026-2027",
            "title": "Reglamento General",
            "url": "https://rfef.es/r-2627.pdf",
        },
        "fcf": {
            "season": "2025-2026",
            "title": "Reglament",
            "url": "https://fcf.cat/r-2526.pdf",
        },
    },
    "divisions": {
        "rfef-primera-fs": {
            "season": "2026-2027",
            "title": "Bases de competición Primera",
            "url": "https://rfef.es/bases-primera.pdf",
        },
    },
}


class ApplyRulesTest(unittest.TestCase):
    def test_publica_solo_la_temporada_pedida(self):
        cats = _categories()
        n = apply_rules(cats, RULES, "2026-2027")

        self.assertEqual(n, 2)
        self.assertEqual(cats[0]["rules"]["url"], "https://rfef.es/r-2627.pdf")
        self.assertEqual(
            cats[0]["divisions"][0]["rules"]["url"],
            "https://rfef.es/bases-primera.pdf",
        )
        # FCF tiene reglamento, pero es del año pasado: no se publica.
        self.assertNotIn("rules", cats[1])
        # Y una división sin entrada propia no inventa ninguna: la app ya
        # hereda el de su federación.
        self.assertNotIn("rules", cats[0]["divisions"][1])

    def test_sin_url_no_se_publica(self):
        # Un aviso de "hay reglamento nuevo" sin enlace no sirve de nada.
        cats = _categories()
        n = apply_rules(
            cats,
            {"categories": {"rfef": {"season": "2026-2027", "title": "X"}}},
            "2026-2027",
        )
        self.assertEqual(n, 0)
        self.assertNotIn("rules", cats[0])

    def test_fichero_vacio_no_rompe(self):
        cats = _categories()
        self.assertEqual(apply_rules(cats, {}, "2026-2027"), 0)


if __name__ == "__main__":
    unittest.main()
