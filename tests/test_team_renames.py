# -*- coding: utf-8 -*-
"""Un equipo que cambia de nombre a mitad de temporada es un equipo.

En Segunda Femenina G2 de 2026-27 "La Villa Móstoles" juega la J1 y la J2 y
"La Villa" de la J3 en adelante, y el grupo se publicaba con 17 equipos para 8
partidos por jornada.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scrapers.team_renames import merge_renamed_teams  # noqa: E402


def _cal(*jornadas):
    return [
        {"jornada": n + 1, "matches": [{"home": h, "away": a} for h, a in js]}
        for n, js in enumerate(jornadas)
    ]


class UnEquipoConDosNombres(unittest.TestCase):
    def _grupo(self):
        return {
            "teams": [
                {"name": "La Villa"},
                {"name": "La Villa Móstoles", "logoUrl": "https://x/v.png"},
                {"name": "Sabadell"},
                {"name": "Rubí"},
            ],
            "calendar": _cal(
                [("La Villa Móstoles", "Sabadell")],
                [("Rubí", "La Villa Móstoles")],
                [("La Villa", "Rubí")],
                [("Sabadell", "La Villa")],
            ),
        }

    def test_se_juntan_con_el_nombre_de_hoy(self):
        g = self._grupo()
        self.assertEqual(merge_renamed_teams(g), ["La Villa Móstoles → La Villa"])
        nombres = [t["name"] for t in g["teams"]]
        self.assertEqual(nombres, ["La Villa", "Sabadell", "Rubí"])
        villa = g["teams"][0]
        self.assertEqual(villa["aliases"], ["La Villa Móstoles"])
        self.assertEqual(villa["logoUrl"], "https://x/v.png")

    def test_dos_equipos_que_coinciden_en_una_jornada_no_se_juntan(self):
        g = self._grupo()
        g["calendar"].append(
            {"jornada": 5, "matches": [{"home": "La Villa", "away": "La Villa Móstoles"}]})
        self.assertEqual(merge_renamed_teams(g), [])
        self.assertEqual(len(g["teams"]), 4)

    def test_nombres_que_no_se_parecen_no_se_juntan(self):
        g = self._grupo()
        g["teams"][1]["name"] = "Pinto FS"
        for j in g["calendar"]:
            for m in j["matches"]:
                for s in ("home", "away"):
                    if m[s] == "La Villa Móstoles":
                        m[s] = "Pinto FS"
        self.assertEqual(merge_renamed_teams(g), [])

    def test_con_dos_jornadas_no_hay_pruebas(self):
        # En un grupo impar, el que descansa la J1 y el que descansa la J2
        # también parecen disjuntos.
        g = self._grupo()
        g["calendar"] = g["calendar"][1:3]
        self.assertEqual(merge_renamed_teams(g), [])


class LasComillas(unittest.TestCase):
    def test_una_sola_grafia_la_del_equipo_publicado(self):
        g = {
            "teams": [{"name": "AE Les Corts B", "officialName": 'Les Corts, A.E. "B"'}],
            "calendar": _cal([('Les Corts, A.E. " B "', "X")],
                             [("Y", 'Les Corts, A.E. "B"')]),
        }
        merge_renamed_teams(g)
        nombres = {m[s] for j in g["calendar"] for m in j["matches"]
                   for s in ("home", "away")}
        self.assertIn('Les Corts, A.E. "B"', nombres)
        self.assertNotIn('Les Corts, A.E. " B "', nombres)


if __name__ == "__main__":
    unittest.main()
