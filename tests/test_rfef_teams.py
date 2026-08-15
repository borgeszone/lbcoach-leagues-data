"""Tests de la fuente de equipos y del guard de temporada.

Cubren los dos cambios que arreglan el fallo de agosto de 2026:

  - los equipos salen del **calendario** cuando la clasificación aún no existe;
  - nada se publica sin haber verificado contra qué temporada se scrapeó.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import scrape as scrape_main  # noqa: E402
from scrapers import rfef  # noqa: E402
from scrapers.rfef_calendario import _parse_matches, teams_from_calendar  # noqa: E402
from scrapers.rfef_discovery import Group  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class TeamsFromCalendar(unittest.TestCase):
    def test_jornada_real_de_2026_2027(self):
        """La J1 real del Grupo 1 de Segunda Femenina 2026-27.

        Es el caso que motivó el cambio: en esa fecha la clasificación estaba
        vacía (liga sin empezar) y el scraper caía al fallback de 2025-26.
        """
        html = (FIXTURES / "jornada1_segunda-fem-g1_2026-2027.html").read_text(
            encoding="utf-8")
        matches = _parse_matches(html)
        self.assertEqual(len(matches), 8)

        teams = teams_from_calendar([{"jornada": 1, "matches": matches}])
        self.assertEqual(len(teams), 16)
        # Equipos que en el JSON publicado del 10-08-2026 NO estaban en este
        # grupo, porque ese JSON era la composición de 2025-26.
        self.assertIn("REYCO Burela FS", teams)
        self.assertIn("At. Arnoya", teams)
        self.assertIn("CDE Muslera GSW \"A\"", teams)

    def test_recorre_todas_las_jornadas_por_el_equipo_que_descansa(self):
        """Con número impar de equipos uno descansa cada jornada. Mirando solo
        la J1, ese equipo no existiría."""
        calendar = [
            {"jornada": 1, "matches": [{"home": "A", "away": "B"},
                                       {"home": "C", "away": "D"}]},
            {"jornada": 2, "matches": [{"home": "E", "away": "A"},
                                       {"home": "B", "away": "C"}]},
        ]
        self.assertEqual(teams_from_calendar(calendar), ["A", "B", "C", "D", "E"])

    def test_dedup_por_normalizado_y_orden_estable(self):
        calendar = [
            {"jornada": 1, "matches": [{"home": "Peñíscola FS", "away": "Zamora"}]},
            {"jornada": 2, "matches": [{"home": "Peniscola  FS", "away": "Alagón"}]},
        ]
        teams = teams_from_calendar(calendar)
        self.assertEqual(len(teams), 3)
        self.assertIn("Peñíscola FS", teams)      # gana la primera grafía
        self.assertEqual(teams, sorted(teams, key=lambda s: teams.index(s)))
        # Dos runs seguidos dan exactamente lo mismo (nada de churn en el JSON).
        self.assertEqual(teams, teams_from_calendar(calendar))

    def test_ignora_vacios_y_calendario_vacio(self):
        self.assertEqual(teams_from_calendar([]), [])
        self.assertEqual(
            teams_from_calendar([{"jornada": 1, "matches": [
                {"home": "", "away": "B"}, {"home": None, "away": None}]}]),
            ["B"])


class CascadaDeEquipos(unittest.TestCase):
    """`_teams_for`: quién gana cuando hay varias fuentes."""

    CFG = {"id": "rfef-segunda-fs-fem", "name": "X", "gender": "femenino",
           "competition": {"code": "1", "name": "X"}, "flat": False,
           "groups": [Group(id="g1", code="2", name="Grupo 1")]}

    def _call(self, calendar, fb_teams=(), scraped=()):
        with mock.patch.object(rfef, "fetch_division_teams", return_value=list(scraped)), \
             mock.patch.object(rfef, "_download_pdf", return_value=None), \
             mock.patch.object(rfef, "resolve_logo_url", return_value=None), \
             mock.patch.object(rfef, "lookup_override", return_value=None):
            return rfef._teams_for(
                comp="1", grupo="2", calendar=calendar, fb_teams=list(fb_teams),
                cfg=self.CFG, group_n=1, season="2026-2027",
                label="test", resolve_badges=True)

    def test_sin_clasificacion_usa_el_calendario(self):
        cal = [{"jornada": 1, "matches": [{"home": "A", "away": "B"}]}]
        teams = self._call(cal)
        self.assertEqual([t["name"] for t in teams], ["A", "B"])

    def test_la_clasificacion_gana_al_calendario(self):
        """Cuando responde trae escudo oficial, que el calendario no tiene."""
        from scrapers.rfef_clasificacion import ScrapedTeam
        cal = [{"jornada": 1, "matches": [{"home": "A", "away": "B"}]}]
        teams = self._call(cal, scraped=[ScrapedTeam("A oficial", "http://e/a.png")])
        self.assertEqual([t["name"] for t in teams], ["A oficial"])
        self.assertEqual(teams[0]["logoUrl"], "http://e/a.png")

    def test_sin_nada_devuelve_vacio_no_el_fallback_de_otra_temporada(self):
        """El gate de temporada vacía `fb_teams` aguas arriba, así que aquí lo
        que llega es una lista vacía y el resultado tiene que ser vacío —
        nunca los equipos del año pasado."""
        self.assertEqual(self._call([], fb_teams=[]), [])

    def test_el_fallback_de_la_temporada_correcta_si_se_usa(self):
        teams = self._call([], fb_teams=[{"name": "Curado", "logoUrl": None}])
        self.assertEqual([t["name"] for t in teams], ["Curado"])


class GuardDeTemporada(unittest.TestCase):
    """Nada se publica sin verificar. Pero **el motivo importa**: `seasonPending`
    separa "la federación aún no ha abierto la temporada" (normal cada julio) de
    "no se pudo consultar" (avería). De ahí sale que Actions te mande email o no.
    """

    def _scrape(self, *, season_result, competitions=None):
        with mock.patch.object(rfef, "resolve_season", return_value=season_result), \
             mock.patch.object(rfef, "Fetcher"), \
             mock.patch.object(rfef, "list_competitions", return_value=competitions):
            return rfef.scrape("2026-2027", resolve_badges=False)

    def test_temporada_no_creada_todavia_es_pendiente_no_averia(self):
        cat = self._scrape(season_result=(None, "not_published"))
        self.assertFalse(cat["seasonVerified"])
        self.assertTrue(cat["seasonPending"])
        self.assertEqual(cat["divisions"], [])

    def test_no_poder_consultar_si_es_averia(self):
        cat = self._scrape(season_result=(None, "unavailable"))
        self.assertFalse(cat["seasonVerified"])
        self.assertFalse(cat["seasonPending"])

    def test_catalogo_ilegible_es_averia(self):
        """`list_competitions` devuelve None cuando no se pudo preguntar."""
        cat = self._scrape(season_result=("22", "ok"), competitions=None)
        self.assertFalse(cat["seasonVerified"])
        self.assertFalse(cat["seasonPending"])

    def test_catalogo_vacio_es_pendiente(self):
        """`[]` es "la PNFG dice que aún no hay ninguna". Distinto de None."""
        cat = self._scrape(season_result=("22", "ok"), competitions=[])
        self.assertFalse(cat["seasonVerified"])
        self.assertTrue(cat["seasonPending"])

    def test_fallback_de_otra_temporada_se_ignora(self):
        """El fichero curado declara 2025-2026. Pidiendo 2026-2027 no puede
        aportar ni un equipo, y tiene que quedar dicho en `warnings`."""
        fb = {"season": "2025-2026",
              "divisions": {"rfef-primera-fs-masc": {"teams": [{"name": "Viejo"}]}}}
        with mock.patch.object(rfef, "_load_fallback", return_value=fb):
            cat = self._scrape(season_result=("22", "ok"), competitions=[])
        self.assertTrue(any("rfef-fallback.json" in w for w in cat["warnings"]))

    def test_fallback_de_la_temporada_pedida_no_avisa(self):
        fb = {"season": "2026-2027", "divisions": {}}
        with mock.patch.object(rfef, "_load_fallback", return_value=fb):
            cat = self._scrape(season_result=(None, "unavailable"))
        self.assertFalse(any("rfef-fallback.json" in w for w in cat["warnings"]))


class AbortaSinEscribir(unittest.TestCase):
    """`scrape.py` no debe dejar un `leagues.json` sin verificar: el workflow
    publica lo que haya en `output/`, así que escribirlo ya es publicarlo."""

    def _run(self, rfef_cat):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = pathlib.Path(tmp) / "output"
            with mock.patch.object(scrape_main, "OUTPUT_DIR", out_dir), \
                 mock.patch.object(scrape_main.rfef, "scrape", return_value=rfef_cat), \
                 mock.patch.object(scrape_main.rfef_shields, "fetch_shield_map",
                                   return_value={}), \
                 mock.patch.object(scrape_main.logo_resolver, "save_cache"), \
                 mock.patch.object(scrape_main.calendar_cache, "save_cache"), \
                 mock.patch.object(sys, "argv", ["scrape.py", "--season", "2026-2027",
                                                 "--no-badges"]):
                code = scrape_main.main()
            path = out_dir / "leagues.json"
            return code, (json.loads(path.read_text(encoding="utf-8"))
                          if path.exists() else None)

    def test_averia_aborta_con_error_y_no_escribe(self):
        code, payload = self._run({
            "id": "rfef", "name": "Liga Española", "source": "rfef.es",
            "divisions": [], "seasonVerified": False, "seasonPending": False,
            "warnings": ["no se pudo consultar el CodTemporada"]})
        self.assertEqual(code, 1)   # falla el step -> email de Actions
        self.assertIsNone(payload)

    def test_temporada_no_abierta_sale_en_verde_y_tampoco_escribe(self):
        """Julio: la temporada ya cambió en el calendario y la federación aún no
        la ha creado. No se publica —no hay nada— pero tampoco se avisa: un
        fallo diario que siempre es normal entrena a ignorar los emails."""
        code, payload = self._run({
            "id": "rfef", "name": "Liga Española", "source": "rfef.es",
            "divisions": [], "seasonVerified": False, "seasonPending": True,
            "warnings": ["la PNFG todavía no ha creado la temporada"]})
        self.assertEqual(code, 0)
        self.assertIsNone(payload)

    def test_verificado_escribe_con_la_temporada_pedida(self):
        code, payload = self._run({
            "id": "rfef", "name": "Liga Española", "source": "rfef.es",
            "divisions": [{"id": "rfef-primera-fs-fem", "name": "1ª FS Fem",
                           "gender": "femenino",
                           "teams": [{"name": "A", "logoUrl": None}]}],
            "seasonVerified": True,
            "warnings": ["sin publicar en la PNFG: rfef-primera-fs-masc"]})
        self.assertEqual(code, 0)
        self.assertEqual(payload["version"], "2026-2027")
        # Los avisos suben a la raíz prefijados por categoría…
        self.assertTrue(any("rfef:" in w for w in payload["warnings"]))
        # …y no se quedan dentro de la categoría, que es contrato de la app.
        self.assertNotIn("warnings", payload["categories"][0])
        self.assertNotIn("seasonVerified", payload["categories"][0])


if __name__ == "__main__":
    unittest.main()
