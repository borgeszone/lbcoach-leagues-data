"""Tests de los parsers de descubrimiento RFEF.

Todas las fixtures de `tests/fixtures/` son respuestas **reales** capturadas de
`resultados.rfef.es` el 2026-08-15, no HTML inventado. Es la diferencia entre
comprobar que el parser hace lo que yo creo y comprobar que entiende lo que el
servidor manda de verdad — que es donde estaba el fallo que originó todo esto.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scrapers.rfef_discovery import (  # noqa: E402
    match_division,
    parse_competitions,
    parse_groups,
    parse_seasons,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class ParseSeasons(unittest.TestCase):
    def test_mapea_nombre_a_codigo(self):
        seasons = parse_seasons(fixture("seasons_select.html"))
        self.assertEqual(seasons["2026-2027"], "22")
        self.assertEqual(seasons["2025-2026"], "21")

    def test_temporada_inexistente_no_revienta(self):
        seasons = parse_seasons(fixture("seasons_select.html"))
        self.assertIsNone(seasons.get("2030-2031"))

    def test_html_sin_select_devuelve_vacio(self):
        self.assertEqual(parse_seasons("<html><body>nada</body></html>"), {})


class ParseCompetitions(unittest.TestCase):
    def test_temporada_2026_2027(self):
        comps = parse_competitions(fixture("competitions_2026-2027.js"))
        by_code = {c.code: c.name for c in comps}
        self.assertEqual(by_code["33836179"], "Primera División Fútbol Sala Femenino")
        self.assertEqual(by_code["33836181"], "Segunda División Fútbol Sala Femenino")
        # El centinela "-- Seleccione --" (value 0) no es una competición.
        self.assertNotIn("0", by_code)

    def test_ignora_subcompeticiones_anidadas(self):
        """El bug que puso Segunda B apuntando a un playoff.

        En 2025-26 los playoffs vienen como `<option>` anidados dentro de la
        competición padre, con la etiqueta empezando por guion. Si se cuelan, un
        `startswith("segunda")` los elige antes que la liga.
        """
        comps = parse_competitions(fixture("competitions_2025-2026.js"))
        codes = {c.code for c in comps}
        self.assertIn("23289365", codes)      # Segunda División B — la liga
        self.assertNotIn("33575532", codes)   # PlayOff de Ascenso a Segunda
        self.assertNotIn("33684246", codes)   # PlayOff por el Título Primera
        for c in comps:
            self.assertFalse(c.name.startswith("-"), c.name)

    def test_conserva_la_etiqueta_del_optgroup(self):
        comps = parse_competitions(fixture("competitions_2025-2026.js"))
        sala = [c for c in comps if c.code == "23289361"][0]
        self.assertEqual(sala.group_label, "Fútbol Sala")


class ParseGroups(unittest.TestCase):
    def test_tres_grupos_con_ids_estables(self):
        groups = parse_groups(fixture("groups_segunda-fem_2026-2027.js"))
        self.assertEqual([g.id for g in groups], ["g1", "g2", "g3"])
        self.assertEqual([g.code for g in groups],
                         ["33836182", "33836183", "33836184"])
        self.assertEqual(groups[1].name, "Grupo 2")

    def test_division_plana_devuelve_un_solo_grupo(self):
        groups = parse_groups(fixture("groups_primera-fem_2026-2027.js"))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].code, "33836180")

    def test_el_id_sale_del_nombre_no_de_la_posicion(self):
        """`Team.groupId` está persistido en el móvil de cada entrenador.

        Si los ids fueran posicionales y la PNFG devolviera los grupos en otro
        orden, cada equipo quedaría en el grupo del vecino — sin que nada
        fallara de forma visible.
        """
        js = ('<script>var grupos=new Array("0","-- Seleccione --",'
              '"777","Grupo 3","888","Grupo 1");</script>')
        groups = parse_groups(js)
        self.assertEqual([(g.id, g.code) for g in groups],
                         [("g3", "777"), ("g1", "888")])

    def test_sin_array_devuelve_vacio(self):
        self.assertEqual(parse_groups("<script>var otra=1;</script>"), [])


class MatchDivision(unittest.TestCase):
    def test_nombres_reales_de_las_dos_temporadas(self):
        casos = {
            # 2025-26 (ojo: "Futbol" sin tilde en la masculina, así lo manda RFEF)
            "Primera División Futbol Sala Masculino": "rfef-primera-fs-masc",
            "Segunda División Fútbol Sala Masculino": "rfef-segunda-fs-masc",
            "Segunda División B Fútbol Sala": "rfef-segunda-b-fs-masc",
            "Primera División Fútbol Sala Femenino": "rfef-primera-fs-fem",
            "Segunda División Fútbol Sala Femenino": "rfef-segunda-fs-fem",
        }
        for name, expected in casos.items():
            with self.subTest(name=name):
                got = match_division(name)
                self.assertIsNotNone(got, name)
                self.assertEqual(got[0], expected)

    def test_genero(self):
        self.assertEqual(match_division("Primera División Fútbol Sala Femenino")[1],
                         "femenino")
        self.assertEqual(match_division("Segunda División B Fútbol Sala")[1],
                         "masculino")

    def test_descarta_lo_que_no_es_liga(self):
        for name in [
            "División de Honor Juvenil Fútbol Sala",
            "Copa de España de Futbol Sala",
            "Supercopa Femenina Sala",
            "Campeonato de España / Copa de S.M. la Reina Fútbol Sala",
            "Campeonato de España de Clubes Base Fútbol Sala Sub-19 Femenino",
            " - PlayOff de Ascenso a Primera División Femenina",
            "Campeonato Nacional de Liga de Primera División",  # fútbol, no sala
            "Campeonato de Primera División de Fútbol Playa Masculino",
        ]:
            with self.subTest(name=name):
                self.assertIsNone(match_division(name), name)

    def test_segunda_b_no_se_confunde_con_segunda(self):
        """Las dos comparten todas las palabras menos la "B", y la masculina es
        la que lleva género. Si Segunda B se evaluara después, "Segunda División
        B Fútbol Sala" no casaría con nada (no dice "masculino") y la división
        desaparecería del JSON."""
        self.assertEqual(match_division("Segunda División B Fútbol Sala")[0],
                         "rfef-segunda-b-fs-masc")
        self.assertEqual(match_division("Segunda División Fútbol Sala Masculino")[0],
                         "rfef-segunda-fs-masc")

    def test_la_marca_nueva_de_la_maxima_masculina(self):
        """2026-27: "Primera División Fútbol Sala Masculino" pasó a llamarse
        **Liga Prime Futsal**, con Apertura y Clausura. No lleva ni "primera",
        ni "sala", ni "masculino": ninguna regla clásica la reconocía y se
        descartaba en silencio."""
        for name in ["Liga Prime Futsal",
                     "Liga Prime Futsal Apertura",
                     "Liga Prime Futsal - Torneo Clausura"]:
            with self.subTest(name=name):
                got = match_division(name)
                self.assertIsNotNone(got, name)
                self.assertEqual(got, ("rfef-primera-fs-masc", "masculino"))

    def test_futsal_vale_igual_que_sala(self):
        self.assertIsNotNone(match_division("Segunda División Futsal Masculino"))

    def test_lo_que_parece_liga_y_no_se_reconoce_se_denuncia(self):
        """La red de seguridad de verdad: si vuelven a rebautizar algo, tiene
        que salir en `warnings` en vez de desaparecer del JSON."""
        from scrapers.rfef_discovery import (
            COMP_IGNORED, COMP_LEAGUE, COMP_UNKNOWN, classify_competition)
        self.assertEqual(
            classify_competition("Superliga Endesa Futsal Masculina")[0],
            COMP_UNKNOWN)
        # Una copa sigue siendo descarte normal, no una alarma.
        self.assertEqual(
            classify_competition("Copa de España de Futbol Sala")[0],
            COMP_IGNORED)
        # Y el fútbol 11 ni se mira.
        self.assertEqual(
            classify_competition("Campeonato Nacional de Liga de Primera División")[0],
            COMP_IGNORED)
        self.assertEqual(
            classify_competition("Liga Prime Futsal")[0], COMP_LEAGUE)

    def test_toda_regla_apunta_a_una_division_conocida(self):
        """Varias reglas pueden apuntar a la misma división —la marca nueva y el
        nombre clásico de Primera masculina conviven— pero **ninguna** puede
        apuntar a un id que no exista: saldría sin nombre en el desplegable de la
        app y sin género."""
        from scrapers.rfef_discovery import (
            DIVISION_GENDER, DIVISION_NAMES, DIVISION_RULES)
        for div_id, gender, _, _ in DIVISION_RULES:
            with self.subTest(div_id=div_id):
                self.assertIn(div_id, DIVISION_NAMES)
                self.assertEqual(DIVISION_GENDER[div_id], gender)

    def test_apertura_y_clausura_casan_con_la_misma_division(self):
        """Los dos torneos son la misma liga con los mismos equipos. Que casen
        igual es lo correcto; que solo uno acabe en el JSON es una limitación
        conocida (el calendario del otro se pierde)."""
        a = match_division("Liga Prime Futsal Torneo Apertura")
        c = match_division("Liga Prime Futsal Torneo Clausura")
        self.assertEqual(a, c)
        self.assertEqual(a[0], "rfef-primera-fs-masc")


if __name__ == "__main__":
    unittest.main()
