# -*- coding: utf-8 -*-
"""Tests de que una liga curada y vacía no llega al desplegable del entrenador.

`data/fcf-manual.json` es una plantilla: seis divisiones de Liga Catalana con
`teams: []` que nadie ha rellenado nunca. Se publicaban igual, así que un
entrenador de Segunda Catalana **encontraba su liga en la lista**, la elegía,
guardaba el `divisionId` en su equipo, y no recibía ni rivales, ni calendario,
ni escudos. Nunca. Con el agravante de que la app le decía "la federación
todavía no ha publicado los equipos de esta liga" —cierto para RFEF en agosto,
falso aquí— y de que `RivalsAutoimportService` reintentaba la reconciliación en
cada arranque para siempre, porque una división vacía no se sella a propósito.

Lo que gobierna el fichero es la asimetría con RFEF, y es lo único que hay que
no romper: una división de RFEF vacía está **pendiente** y se rellena sola en
cuanto la federación abra; una curada vacía está **sin rellenar** y no se va a
rellenar sola. Aplicar el filtro a las dos —que es lo que haría cualquiera
"unificando" la regla— rompería la recuperación automática de agosto.

Ninguno toca la red.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import scrape as scrape_main  # noqa: E402


def _cat(cat_id: str, divisions: list[dict]) -> dict:
    return {"id": cat_id, "name": cat_id.upper(), "divisions": divisions}


def _div(div_id: str, teams=None, groups=None) -> dict:
    d = {"id": div_id, "name": div_id, "teams": list(teams or [])}
    if groups is not None:
        d["groups"] = groups
    return d


def _equipo(nombre: str) -> dict:
    return {"name": nombre, "logoUrl": None}


class LaPlantillaSinRellenarNoSePublica(unittest.TestCase):
    def test_division_curada_sin_equipos_se_retira(self):
        cats = [_cat("fcf", [_div("fcf-1cat"), _div("fcf-2cat", [_equipo("A")])])]
        quitadas = scrape_main.drop_unfilled_divisions(cats)
        self.assertEqual([d["id"] for d in cats[0]["divisions"]], ["fcf-2cat"])
        self.assertEqual(len(quitadas), 1)
        self.assertIn("fcf-1cat", quitadas[0])

    def test_division_curada_con_equipos_se_queda_intacta(self):
        cats = [_cat("fcf", [_div("fcf-1cat", [_equipo("A"), _equipo("B")])])]
        self.assertEqual(scrape_main.drop_unfilled_divisions(cats), [])
        self.assertEqual(len(cats[0]["divisions"][0]["teams"]), 2)

    def test_division_con_todos_los_grupos_vacios_se_retira(self):
        # El caso literal de `fcf-2cat-fs-masc`: sin teams y con dos grupos
        # esqueleto. El cliente ya esconde los grupos vacíos, así que esta
        # división llegaba al móvil como una lista plana sin un solo equipo.
        # Con una división viva al lado, para que lo que se mide sea la caída
        # de ésta y no la retirada de la categoría entera.
        cats = [_cat("fcf", [
            _div("fcf-2cat", groups=[
                {"id": "g1", "name": "Grup 1", "teams": []},
                {"id": "g2", "name": "Grup 2", "teams": []},
            ]),
            _div("fcf-3cat", [_equipo("A")]),
        ])]
        scrape_main.drop_unfilled_divisions(cats)
        self.assertEqual([d["id"] for d in cats[0]["divisions"]], ["fcf-3cat"])

    def test_un_grupo_con_equipos_salva_la_division_y_el_vacio_se_cae(self):
        cats = [_cat("fcf", [_div("fcf-2cat", groups=[
            {"id": "g1", "name": "Grup 1", "teams": [_equipo("A")]},
            {"id": "g2", "name": "Grup 2", "teams": []},
        ])])]
        scrape_main.drop_unfilled_divisions(cats)
        grupos = cats[0]["divisions"][0]["groups"]
        self.assertEqual([g["id"] for g in grupos], ["g1"])

    def test_categoria_sin_ninguna_division_viva_se_retira_entera(self):
        # Dejarla deja un desplegable de "División" cuya única opción es
        # "selecciona división": un callejón sin salida, peor que no estar.
        cats = [_cat("rfef", [_div("rfef-1", [_equipo("A")])]),
                _cat("fcf", [_div("fcf-1"), _div("fcf-2")])]
        quitadas = scrape_main.drop_unfilled_divisions(cats)
        self.assertEqual([c["id"] for c in cats], ["rfef"])
        self.assertTrue(any("la categoría" in q for q in quitadas))

    def test_lo_retirado_se_nombra_para_que_salga_en_warnings(self):
        # Sin esto las plantillas desaparecen del JSON y con ellas el único
        # recordatorio de que hay divisiones esperando a que alguien las llene.
        cats = [_cat("fcf", [_div("fcf-1cat"), _div("fcf-3cat", [_equipo("A")])])]
        quitadas = scrape_main.drop_unfilled_divisions(cats)
        self.assertIn("fcf-manual.json", quitadas[0])

    def test_una_division_sin_clave_groups_no_revienta(self):
        cats = [_cat("fcf", [_div("fcf-1cat", [_equipo("A")])])]
        scrape_main.drop_unfilled_divisions(cats)
        self.assertNotIn("groups", cats[0]["divisions"][0])


class RfefNoEntraEnEstaRegla(unittest.TestCase):
    """La asimetría es el fichero entero: sin ella se rompe el agosto de RFEF."""

    def test_una_division_de_rfef_vacia_se_publica_igual(self):
        # Segunda B en agosto: la PNFG aún no la ha abierto. Tiene que seguir
        # en el desplegable para que quien la elija hoy reciba sus rivales
        # solo el día que la federación publique (§6.46) — retirarla obliga a
        # volver a editar el equipo, que es justo lo que aquello arregló.
        cats = [_cat("rfef", [_div("rfef-segunda-b")])]
        self.assertEqual(scrape_main.drop_unfilled_divisions(cats), [])
        self.assertEqual([d["id"] for d in cats[0]["divisions"]],
                         ["rfef-segunda-b"])

    def test_a_rfef_no_se_le_tocan_ni_los_grupos_vacios(self):
        cats = [_cat("rfef", [_div("rfef-segunda-fem", groups=[
            {"id": "g1", "name": "Grupo 1", "teams": []},
        ])])]
        scrape_main.drop_unfilled_divisions(cats)
        self.assertEqual(len(cats[0]["divisions"][0]["groups"]), 1)


class ElOrdenRespectoALaHerencia(unittest.TestCase):
    """Heredar primero es lo único que protege un `fcf-manual.json` vaciado."""

    def test_la_division_curada_que_hereda_equipos_sobrevive(self):
        # Alguien vacía el JSON manual sin querer. `inherit_teams` restaura los
        # equipos de ayer y la división NO debe caerse: dropear una división
        # que ayer tenía 16 equipos es la regresión silenciosa que §6.55 existe
        # para impedir.
        cats = [_cat("fcf", [_div("fcf-1cat")])]
        publicado = {"categories": [
            _cat("fcf", [_div("fcf-1cat", [_equipo("A"), _equipo("B")])])]}

        scrape_main.inherit_teams(cats, publicado)
        quitadas = scrape_main.drop_unfilled_divisions(cats)

        self.assertEqual(quitadas, [])
        self.assertEqual(len(cats[0]["divisions"][0]["teams"]), 2)

    def test_sin_nada_que_heredar_la_plantilla_si_se_retira(self):
        cats = [_cat("fcf", [_div("fcf-1cat")])]
        publicado = {"categories": [_cat("fcf", [_div("fcf-1cat")])]}

        scrape_main.inherit_teams(cats, publicado)
        scrape_main.drop_unfilled_divisions(cats)

        self.assertEqual(cats, [])


if __name__ == "__main__":
    unittest.main()
