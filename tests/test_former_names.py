# -*- coding: utf-8 -*-
"""Los nombres que un club tuvo antes en la temporada viajan como `aliases`.

Un móvil tenía «Industrias Santa Coloma», «Inagroup El Ejido», «Osasuna Magna»
y «FS García» como rivales: nombres de Primera que la federación dejó de usar,
y que la app enseñaba en todos los equipos porque ya no estaban en ninguna liga.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scrapers import former_names as fn  # noqa: E402


def _cat(teams, calendar=None, div="rfef-primera-fs-masc", groups=None):
    d = {"id": div, "teams": teams}
    if calendar is not None:
        d["calendar"] = calendar
    if groups is not None:
        d = {"id": div, "teams": [], "groups": groups}
    return [{"id": "rfef", "divisions": [d]}]


def _j(n, *matches, phase=None):
    j = {"jornada": n, "matches": [{"home": h, "away": a} for h, a in matches]}
    if phase:
        j["phase"] = phase
    return j


def _aliases(categories, name):
    for _, node in fn._nodes(categories):
        for t in node.get("teams") or []:
            if t["name"] == name:
                return t.get("aliases") or []
    raise AssertionError(name)


class ElMismoHuecoDelCalendario(unittest.TestCase):
    """«FS García» jugaba en casa del Inter la J11; hoy ese partido es de Catgas."""

    def _antes(self):
        return _cat(
            [{"name": "Inter FS"}, {"name": "FS García"}, {"name": "Barça"},
             {"name": "Noia"}],
            [_j(11, ("Inter FS", "FS García"), ("Barça", "Noia"), phase="Apertura"),
             _j(12, ("FS García", "Barça"), ("Noia", "Inter FS"), phase="Apertura")])

    def _hoy(self, j12=True):
        cal = [_j(11, ("Inter FS", "Catgas Energia"), ("Barça", "Noia"), phase="Apertura")]
        if j12:
            cal.append(_j(12, ("Catgas Energia", "Barça"), ("Noia", "Inter FS"),
                          phase="Apertura"))
        return _cat([{"name": "Inter FS"}, {"name": "Catgas Energia"},
                     {"name": "Barça"}, {"name": "Noia"}], cal)

    def test_dos_jornadas_iguales_bastan(self):
        store, hoy = {}, self._hoy()
        fn.learn(store, self._antes(), hoy)
        fn.apply(store, hoy)
        self.assertEqual(_aliases(hoy, "Catgas Energia"), ["FS García"])

    def test_una_sola_jornada_no(self):
        store, hoy = {}, self._hoy(j12=False)
        fn.learn(store, self._antes(), hoy)
        fn.apply(store, hoy)
        self.assertEqual(_aliases(hoy, "Catgas Energia"), [])

    def test_si_los_huecos_dicen_dos_equipos_distintos_no(self):
        antes = _cat(
            [{"name": "Inter FS"}, {"name": "FS García"}, {"name": "Barça"},
             {"name": "Noia"}],
            [_j(11, ("Inter FS", "FS García"), ("Barça", "Noia")),
             _j(12, ("FS García", "Barça"), ("Noia", "Inter FS"))])
        hoy = _cat(
            [{"name": "Inter FS"}, {"name": "Catgas Energia"}, {"name": "Barça"},
             {"name": "Noia"}, {"name": "Xota"}],
            [_j(11, ("Inter FS", "Catgas Energia"), ("Barça", "Noia")),
             _j(12, ("Xota", "Barça"), ("Noia", "Inter FS"))])
        store = {}
        fn.learn(store, antes, hoy)
        fn.apply(store, hoy)
        self.assertEqual(_aliases(hoy, "Catgas Energia"), [])
        self.assertEqual(_aliases(hoy, "Xota"), [])


class ElMismoNombreOficial(unittest.TestCase):
    def test_aunque_entre_medias_no_saliera(self):
        """Industrias Santa Coloma entraba y salía de Primera; Catgas apareció
        en un run en que ya no estaba. Comparando sólo cada JSON con el
        anterior, no coincidían nunca."""
        uno = _cat([{"name": "Industrias Santa Coloma",
                     "officialName": "Catgas Energía Santa Coloma"},
                    {"name": "Barça"}])
        dos = _cat([{"name": "Barça"}])
        tres = _cat([{"name": "Catgas Energia",
                      "officialName": "Catgas Energía Santa Coloma"},
                     {"name": "Barça"}])
        store = {}
        fn.learn(store, [], uno)
        fn.learn(store, uno, dos)
        fn.learn(store, dos, tres)
        fn.apply(store, tres)
        self.assertEqual(_aliases(tres, "Catgas Energia"), ["Industrias Santa Coloma"])

    def test_un_equipo_que_sigue_igual_no_lleva_alias(self):
        antes = _cat([{"name": "Barça", "officialName": "FC Barcelona"}])
        hoy = _cat([{"name": "Barça", "officialName": "FC Barcelona"}])
        store = {}
        fn.learn(store, antes, hoy)
        fn.apply(store, hoy)
        self.assertEqual(_aliases(hoy, "Barça"), [])


class NoJuntaDosClubes(unittest.TestCase):
    def test_un_nombre_de_antes_que_toca_a_dos_equipos_de_hoy(self):
        antes = _cat([{"name": "Mataró A", "officialName": "Mataró B"}])
        hoy = _cat([{"name": "Mataró A"}, {"name": "Mataró B"}])
        store = {}
        fn.learn(store, antes, hoy)
        for g in store.get("rfef-primera-fs-masc", []):
            self.assertFalse({"Mataró A", "Mataró B"} <= set(g), g)

    def test_un_grupo_guardado_no_hace_de_puente(self):
        """«Viejo» se guardó con Mataró B; ahora un equipo de antes llamado
        «Viejo» lleva el oficial de Mataró A. Juntarlo uniría A y B."""
        store = {"rfef-primera-fs-masc": [["Viejo", "Mataró B"]]}
        antes = _cat([{"name": "Viejo", "officialName": "Mataró A"}])
        hoy = _cat([{"name": "Mataró A"}, {"name": "Mataró B"}])
        fn.learn(store, antes, hoy)
        for g in store["rfef-primera-fs-masc"]:
            self.assertFalse({"Mataró A", "Mataró B"} <= set(g), g)

    def test_un_almacen_que_junta_dos_equipos_de_hoy_no_se_aplica(self):
        """Por ejemplo, editado a mano: no se publica nada de ese grupo."""
        store = {"rfef-primera-fs-masc": [["Mataró A", "Mataró B", "Mataró viejo"]]}
        hoy = _cat([{"name": "Mataró A"}, {"name": "Mataró B"}])
        fn.apply(store, hoy)
        self.assertEqual(_aliases(hoy, "Mataró A"), [])
        self.assertEqual(_aliases(hoy, "Mataró B"), [])

    def test_ni_entre_grupos(self):
        """Granada FS está en Segunda Femenina G3 y en Segunda B G5: dos equipos."""
        antes = [{"id": "rfef", "divisions": [
            {"id": "fem", "teams": [], "groups": [
                {"id": "g3", "teams": [{"name": "Granada FS",
                                        "officialName": "Fundación UAPO Granada FS Femenino"}]}]},
            {"id": "b", "teams": [], "groups": [
                {"id": "g5", "teams": [{"name": "Granada FS",
                                        "officialName": "Sima Granada FS"}]}]}]}]
        hoy = json.loads(json.dumps(antes))
        hoy[0]["divisions"][0]["groups"][0]["teams"][0]["officialName"] = \
            "Coviran UAPO Granada FS Femenino"
        store = {}
        fn.learn(store, antes, hoy)
        fn.apply(store, hoy)
        fem = hoy[0]["divisions"][0]["groups"][0]["teams"][0]
        masc = hoy[0]["divisions"][1]["groups"][0]["teams"][0]
        self.assertEqual(fem.get("aliases"), ["Fundación UAPO Granada FS Femenino"])
        self.assertIsNone(masc.get("aliases"))

    def test_no_publica_el_nombre_de_otro_equipo_de_hoy(self):
        """Un nombre viejo que hoy lleva otro equipo de otra división no se
        publica como alias: la app los juntaría."""
        store = {"rfef-primera-fs-masc": [["Catgas Energia", "Ripollet"]]}
        hoy = [{"id": "rfef", "divisions": [
            {"id": "rfef-primera-fs-masc", "teams": [{"name": "Catgas Energia"}]},
            {"id": "rfef-segunda-b-fs-masc", "teams": [{"name": "Ripollet"}]}]}]
        fn.apply(store, hoy)
        self.assertEqual(_aliases(hoy, "Catgas Energia"), [])


class ElAlmacen(unittest.TestCase):
    def test_otra_temporada_empieza_de_cero(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "f.json"
            fn.save_store({"x": [["A", "B"]]}, "2025-2026", p)
            self.assertEqual(fn.load_store("2026-2027", p), {})
            self.assertEqual(fn.load_store("2025-2026", p), {"x": [["A", "B"]]})

    def test_sin_fichero_o_roto_es_vacio(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "f.json"
            self.assertEqual(fn.load_store("2026-2027", p), {})
            p.write_text("{no", encoding="utf-8")
            self.assertEqual(fn.load_store("2026-2027", p), {})

    def test_el_fichero_real_es_de_esta_temporada_y_no_junta_dos_equipos(self):
        """El almacén commiteado se sembró del historial publicado."""
        data = json.loads(fn.STORE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["season"], "2026-2027")
        for node, groups in data["nodes"].items():
            keys = [fn._norm(n) for g in groups for n in g]
            self.assertEqual(len(keys), len(set(keys)), node)


if __name__ == "__main__":
    unittest.main()
