# -*- coding: utf-8 -*-
"""Tests de lo que impide que un run malo borre equipos que estaban bien.

El 1 de septiembre de 2026 el JSON publicado dejó `rfef-segunda-fs-fem` con
cero equipos y cero grupos —48 equipos y 90 jornadas que el día anterior
estaban completos— y el run terminó **en verde**, con un aviso que además
culpaba a la federación: "sin publicar en la PNFG para 2026-2027". La PNFG sí
la publicaba; lo que pasó es que no contestó a la consulta de grupos.

Tres piezas:

  - `list_groups` distingue "no se pudo preguntar" (None) de "no hay grupos
    todavía" (`[]`), que es la distinción que el resto del módulo ya hacía.
  - `inherit_teams` conserva los equipos del JSON publicado cuando este run no
    ha traído ninguno.
  - `scrape_order` rota **el orden de pedir**, para que el presupuesto de
    bloqueos del run no se lo coma siempre el mismo final de cola. Y el orden de
    **publicar** sigue siendo fijo, que es la mitad que se rompe sola si alguien
    "unifica" las dos cosas.

Ninguno toca la red.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import datetime
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import scrape as scrape_main  # noqa: E402
from scrapers import rfef, rfef_discovery  # noqa: E402
from scrapers.rfef_discovery import Competition, Group  # noqa: E402


class ElCatalogoDeGruposDistingueAveriaDeVacio(unittest.TestCase):
    """`[]` significaba dos cosas, y una de ellas costaba una división."""

    def setUp(self):
        # Sin sesión, `list_groups` construye un `Fetcher`, y `make_session`
        # hace un GET de verdad a la PNFG para sembrar la JSESSIONID. Sin este
        # parche el test tarda minutos (backoffs de 15/30/60/120 s) y le pega al
        # servidor de la federación.
        p = mock.patch.object(rfef_discovery, "make_session",
                              return_value=mock.Mock())
        p.start()
        self.addCleanup(p.stop)

    def test_none_cuando_no_se_pudo_preguntar(self):
        with mock.patch.object(rfef_discovery.Fetcher, "get", return_value=None):
            self.assertIsNone(rfef_discovery.list_groups("33836181"))

    def test_lista_vacia_cuando_la_pnfg_dice_que_no_hay(self):
        cuerpo = 'var grupos=new Array("0","-- Seleccione --");'
        with mock.patch.object(rfef_discovery.Fetcher, "get", return_value=cuerpo):
            self.assertEqual(rfef_discovery.list_groups("33836181"), [])

    def test_los_grupos_de_verdad_se_siguen_leyendo(self):
        cuerpo = ('var grupos=new Array("0","-- Seleccione --",'
                  '"33836182","Grupo 1","33836183","Grupo 2");')
        with mock.patch.object(rfef_discovery.Fetcher, "get", return_value=cuerpo):
            grupos = rfef_discovery.list_groups("33836181")
        self.assertEqual([g.id for g in grupos], ["g1", "g2"])


class UnaAveriaNoSeCuelaComoDecisionDeLaFederacion(unittest.TestCase):
    """El aviso importa tanto como el dato: "sin publicar en la PNFG" manda a
    mirar donde no es, y en este caso era mentira — la competición se acababa de
    leer del catálogo de esta misma temporada."""

    # Dos competiciones y no una: con todas caídas, `build_category` sale por
    # la rama de "la federación no ha abierto la temporada" y no se llega a
    # calcular `faltan`. Ese caso tiene su propio test más abajo.
    COMPS = [
        Competition(code="1", name="Liga Prime Futsal"),
        Competition(code="33836181",
                    name="Segunda División Fútbol Sala Femenino"),
    ]

    def _groups_for(self, groups):
        def _fake(code, **kw):
            if code == "1":   # la masculina resuelve bien
                return [Group(id="g1", code="11", name="Grupo 1")]
            return groups     # la femenina es la que se cae
        return _fake

    def _scrape(self, groups):
        with mock.patch.object(rfef, "resolve_season", return_value=("22", "ok")), \
             mock.patch.object(rfef, "Fetcher"), \
             mock.patch.object(rfef, "list_competitions", return_value=self.COMPS), \
             mock.patch.object(rfef, "list_groups",
                               side_effect=self._groups_for(groups)), \
             mock.patch.object(rfef, "_group_from_web", return_value=[]), \
             mock.patch.object(rfef, "fetch_web_sources", return_value={}), \
             mock.patch.object(rfef, "fetch_division_teams", return_value=[]), \
             mock.patch.object(rfef, "_attach_calendars"), \
             mock.patch.object(rfef, "_fill_missing_from_pdf"), \
             mock.patch.object(rfef, "time"), \
             mock.patch.object(rfef, "_load_fallback",
                               return_value={"season": "2026-2027",
                                             "divisions": {}}):
            return rfef.scrape("2026-2027", resolve_badges=False)

    def _sin_publicar(self, cat):
        return " ".join(w for w in cat["warnings"]
                        if w.startswith("sin publicar en la PNFG"))

    def test_la_averia_se_denuncia_como_averia(self):
        cat = self._scrape(None)
        self.assertTrue(
            any("no contestó al catálogo de grupos" in w for w in cat["warnings"]),
            cat["warnings"])

    def test_y_no_se_le_echa_la_culpa_a_la_federacion(self):
        cat = self._scrape(None)
        self.assertNotIn("rfef-segunda-fs-fem", self._sin_publicar(cat))

    def test_lo_que_la_pnfg_no_lista_si_es_no_publicado(self):
        """El caso legítimo sigue contándose: hay divisiones que la federación
        publica semanas más tarde y eso tiene que constar."""
        cat = self._scrape(None)
        self.assertIn("rfef-primera-fs-fem", self._sin_publicar(cat))

    def test_si_se_caen_todas_es_averia_y_no_la_ventana_de_verano(self):
        """`seasonPending` decide el código de salida, o sea si llega el email de
        Actions. Un rate-limit total contado como "la federación no ha abierto
        la temporada" es un run verde del que nadie se entera."""
        with mock.patch.object(rfef, "resolve_season", return_value=("22", "ok")), \
             mock.patch.object(rfef, "Fetcher"), \
             mock.patch.object(rfef, "list_competitions", return_value=self.COMPS), \
             mock.patch.object(rfef, "list_groups", return_value=None), \
             mock.patch.object(rfef, "_group_from_web", return_value=[]), \
             mock.patch.object(rfef, "fetch_division_teams", return_value=[]), \
             mock.patch.object(rfef, "time"), \
             mock.patch.object(rfef, "_load_fallback",
                               return_value={"season": "2026-2027",
                                             "divisions": {}}):
            cat = rfef.scrape("2026-2027", resolve_badges=False)
        self.assertFalse(cat["seasonVerified"])
        self.assertFalse(cat["seasonPending"])

    def test_sin_grupos_todavia_no_es_averia(self):
        """Antes del sorteo la PNFG contesta que no hay grupos. Eso es normal y
        no puede sonar igual que un servidor que no responde."""
        cat = self._scrape([])
        self.assertFalse(
            any("no contestó al catálogo de grupos" in w for w in cat["warnings"]),
            cat["warnings"])
        self.assertIn("rfef-segunda-fs-fem", self._sin_publicar(cat))


class ElOrdenDePedirRotaYElDePublicarNo(unittest.TestCase):
    """Todo run tiene un presupuesto finito de bloqueos, y con un orden fijo se lo
    come siempre el mismo final de cola.

    Lo que se prueba aquí es la distinción entre los dos ejes, que es lo único
    que alguien podría "unificar" por limpieza rompiendo el fichero publicado.
    """

    ORDEN = ("a", "b", "c", "d", "e")

    def test_el_mismo_dia_da_el_mismo_orden(self):
        """Reproducible a propósito: un run manual para depurar tiene que
        comportarse como el del cron, y no hay estado que guardar."""
        d = datetime.date(2026, 9, 7)
        self.assertEqual(rfef.scrape_order(self.ORDEN, d),
                         rfef.scrape_order(self.ORDEN, d))

    def test_en_cinco_runs_semanales_le_toca_ir_primero_a_cada_uno(self):
        # 7 % 5 = 2, así que el desplazamiento avanza 0-2-4-1-3 y vuelve: las
        # cinco posiciones sin repetir.
        lunes = [datetime.date(2026, 9, 7) + datetime.timedelta(days=7 * n)
                 for n in range(5)]
        primeros = [rfef.scrape_order(self.ORDEN, d)[0] for d in lunes]
        self.assertEqual(sorted(primeros), sorted(self.ORDEN), primeros)

    def test_rotar_no_pierde_ni_duplica_ninguna(self):
        for n in range(400):
            d = datetime.date(2026, 1, 1) + datetime.timedelta(days=n)
            o = rfef.scrape_order(self.ORDEN, d)
            self.assertEqual(sorted(o), sorted(self.ORDEN), d)

    def test_una_lista_vacia_no_revienta(self):
        self.assertEqual(rfef.scrape_order(()), ())

    def test_discover_divisions_LA_USA_de_verdad(self):
        """El caso que faltaba, y sin él todo lo de arriba era decorativo:
        probaban `scrape_order` aislada, así que quitar la rotación del código
        dejaba los cinco en verde. Esto mira lo que sale de `discover_divisions`,
        que es lo que alimenta las tres llamadas caras."""
        comps = [Competition(code=str(i), name=n) for i, n in enumerate([
            "Liga Prime Futsal",
            "Segunda División Fútbol Sala Masculino",
            "Primera División Fútbol Sala Femenino",
        ])]

        def _orden(dia):
            with mock.patch.object(rfef, "list_competitions", return_value=comps), \
                 mock.patch.object(rfef, "list_groups",
                                   return_value=[Group(id="g1", code="9",
                                                       name="Grupo 1")]), \
                 mock.patch.object(rfef, "time"), \
                 mock.patch.object(rfef, "date") as hoy:
                hoy.today.return_value = dia
                return [c["id"] for c in rfef.discover_divisions("22")]

        # Dos días cuyo desplazamiento no coincide: yday 250 % 5 = 0 y 251 % 5 = 1.
        a = _orden(datetime.date(2026, 9, 7))
        b = _orden(datetime.date(2026, 9, 8))
        self.assertEqual(sorted(a), sorted(b), 'mismas divisiones')
        self.assertNotEqual(a, b, f'el orden de scrapeo no rota: {a}')

    def test_EL_ORDEN_PUBLICADO_NO_ROTA(self):
        """La mitad que importa. Si el JSON cambiara de orden cada semana sin que
        hubiera cambiado un dato, los diffs de gh-pages serían ilegibles y el
        árbol del panel bailaría — y es lo que dice el comentario de
        `DIVISION_ORDER` desde que existe."""
        comps = [Competition(code=str(i), name=n) for i, n in enumerate([
            "Liga Prime Futsal",
            "Segunda División Fútbol Sala Masculino",
            "Primera División Fútbol Sala Femenino",
        ])]
        publicados = []
        for dia in (datetime.date(2026, 9, 7), datetime.date(2026, 9, 14),
                    datetime.date(2026, 9, 21)):
            with mock.patch.object(rfef, "resolve_season",
                                   return_value=("22", "ok")), \
                 mock.patch.object(rfef, "Fetcher"), \
                 mock.patch.object(rfef, "list_competitions",
                                   return_value=comps), \
                 mock.patch.object(rfef, "list_groups",
                                   return_value=[Group(id="g1", code="9",
                                                       name="Grupo 1")]), \
                 mock.patch.object(rfef, "fetch_web_sources", return_value={}), \
                 mock.patch.object(rfef, "fetch_division_teams", return_value=[]), \
                 mock.patch.object(rfef, "_attach_calendars"), \
                 mock.patch.object(rfef, "_fill_missing_from_pdf"), \
                 mock.patch.object(rfef, "time"), \
                 mock.patch.object(rfef, "date") as hoy, \
                 mock.patch.object(rfef, "_load_fallback",
                                   return_value={"season": "2026-2027",
                                                 "divisions": {}}):
                hoy.today.return_value = dia
                cat = rfef.scrape("2026-2027", resolve_badges=False)
            publicados.append([d["id"] for d in cat["divisions"]])

        for orden in publicados:
            self.assertEqual(orden, list(rfef.DIVISION_ORDER), orden)


def _equipos(*nombres):
    return [{"name": n} for n in nombres]


class LaHerenciaDeEquiposRellenaHuecos(unittest.TestCase):
    """`inherit_teams` **no** es `inherit_calendars`: aquí sólo se rellena lo que
    viene a cero. Un calendario sólo crece; un plantel puede encogerse de
    verdad."""

    def _publicado(self, **kw):
        div = {"id": "rfef-segunda-fs-fem", "name": "2a Fem",
               "groups": [
                   {"id": "g1", "name": "Grupo 1", "teams": _equipos("A", "B"),
                    "teamsSource": "clasificacion",
                    "calendar": [{"jornada": 1,
                                  "matches": [{"home": "A", "away": "B"}]}]},
                   {"id": "g2", "name": "Grupo 2",
                    "teams": _equipos("C", "D", "E"),
                    "teamsSource": "calendario"},
               ]}
        div.update(kw)
        return {"version": "2026-2027",
                "categories": [{"id": "rfef", "divisions": [div]}]}

    def _cats(self, div):
        return [{"id": "rfef", "divisions": [div]}]

    def test_el_cascaron_recupera_sus_grupos_con_sus_equipos(self):
        """El caso del 1/9/2026: la división vuelve sin grupos, así que no hay
        ni dónde colgar los equipos."""
        cats = self._cats({"id": "rfef-segunda-fs-fem", "name": "2a Fem",
                           "teams": []})
        n = scrape_main.inherit_teams(cats, self._publicado())
        div = cats[0]["divisions"][0]
        self.assertEqual(n, 2)
        self.assertEqual([g["id"] for g in div["groups"]], ["g1", "g2"])
        self.assertEqual([t["name"] for t in div["groups"][1]["teams"]],
                         ["C", "D", "E"])
        self.assertEqual(div["groups"][0]["teamsSource"], "clasificacion")

    def test_los_grupos_restaurados_heredan_tambien_su_calendario(self):
        """Por eso `inherit_teams` va antes: si los grupos no existen cuando la
        herencia de calendarios los busca por id, la división se queda con sus
        equipos y sin jornadas."""
        publicado = self._publicado()
        cats = self._cats({"id": "rfef-segunda-fs-fem", "name": "2a Fem",
                           "teams": []})
        scrape_main.inherit_teams(cats, publicado)
        scrape_main.inherit_calendars(cats, publicado)
        g1 = cats[0]["divisions"][0]["groups"][0]
        self.assertEqual(len(g1["calendar"]), 1)

    def test_un_grupo_a_cero_se_rellena(self):
        cats = self._cats({"id": "rfef-segunda-fs-fem", "name": "2a Fem",
                           "groups": [
                               {"id": "g1", "name": "Grupo 1",
                                "teams": _equipos("A", "B")},
                               {"id": "g2", "name": "Grupo 2", "teams": []},
                           ]})
        n = scrape_main.inherit_teams(cats, self._publicado())
        self.assertEqual(n, 1)
        self.assertEqual(
            [t["name"] for t in cats[0]["divisions"][0]["groups"][1]["teams"]],
            ["C", "D", "E"])

    def test_un_plantel_que_encoge_se_respeta(self):
        """El sabotaje que este fichero existe para cazar: copiar de
        `inherit_calendars` la regla de "gana el más completo". Un equipo que se
        retira en noviembre deja el grupo en 2, y conservar los 3 publicados lo
        mantendría como rival fantasma el resto de la temporada."""
        cats = self._cats({"id": "rfef-segunda-fs-fem", "name": "2a Fem",
                           "groups": [
                               {"id": "g2", "name": "Grupo 2",
                                "teams": _equipos("C", "D")},
                           ]})
        n = scrape_main.inherit_teams(cats, self._publicado())
        self.assertEqual(n, 0)
        self.assertEqual(
            [t["name"] for t in cats[0]["divisions"][0]["groups"][0]["teams"]],
            ["C", "D"])

    def test_una_division_plana_vacia_se_rellena(self):
        publicado = self._publicado(groups=[], teams=_equipos("X", "Y"),
                                    teamsSource="calendario")
        cats = self._cats({"id": "rfef-segunda-fs-fem", "name": "2a Fem",
                           "teams": []})
        n = scrape_main.inherit_teams(cats, publicado)
        self.assertEqual(n, 1)
        self.assertEqual([t["name"] for t in cats[0]["divisions"][0]["teams"]],
                         ["X", "Y"])

    def test_una_division_que_no_estaba_publicada_no_se_inventa(self):
        cats = self._cats({"id": "rfef-primera-fs-fem", "name": "1a Fem",
                           "teams": []})
        self.assertEqual(scrape_main.inherit_teams(cats, self._publicado()), 0)
        self.assertEqual(cats[0]["divisions"][0]["teams"], [])

    def test_la_herencia_queda_por_escrito(self):
        """Un dato heredado y uno recién scrapeado se ven igual en el JSON. El
        aviso es lo único que permite saber que el run de hoy no trajo esa
        división."""
        cats = self._cats({"id": "rfef-segunda-fs-fem", "name": "2a Fem",
                           "teams": []})
        scrape_main.inherit_teams(cats, self._publicado())
        self.assertTrue(any("se conservan los" in w
                            for w in cats[0]["warnings"]), cats[0])


if __name__ == "__main__":
    unittest.main()
