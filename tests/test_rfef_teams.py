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
from scrapers.rfef_discovery import Competition, Group  # noqa: E402

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
             mock.patch.object(rfef, "fetch_web_sources", return_value={}), \
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


class DivisionesSinAbrir(unittest.TestCase):
    """Las divisiones que la federación aún no ha publicado salen **vacías pero
    presentes**. Omitirlas deja al entrenador sin poder seleccionar la suya,
    y su equipo con `divisionId` nulo: el día que la federación publique, nada
    se engancharía solo."""

    def test_las_cinco_estan_siempre_y_en_orden(self):
        descubiertas = [{"id": "rfef-primera-fs-fem", "name": "1a Fem",
                         "gender": "femenino", "teams": [{"name": "A"}]}]
        out = rfef._with_placeholders(descubiertas)
        self.assertEqual([d["id"] for d in out], list(rfef.DIVISION_ORDER))

    def test_el_placeholder_va_vacio_no_con_equipos_viejos(self):
        out = rfef._with_placeholders([])
        for d in out:
            self.assertEqual(d["teams"], [], d["id"])
            self.assertNotIn("calendar", d)
            self.assertNotIn("groups", d)

    def test_no_pisa_lo_descubierto(self):
        real = {"id": "rfef-primera-fs-fem", "name": "1a Fem",
                "gender": "femenino", "teams": [{"name": "A"}],
                "calendar": [{"jornada": 1, "matches": []}]}
        out = rfef._with_placeholders([real])
        got = [d for d in out if d["id"] == "rfef-primera-fs-fem"][0]
        self.assertIs(got, real)

    def test_nombre_y_genero_del_placeholder(self):
        out = {d["id"]: d for d in rfef._with_placeholders([])}
        self.assertEqual(out["rfef-primera-fs-masc"]["name"], "Primera División FS")
        self.assertEqual(out["rfef-primera-fs-masc"]["gender"], "masculino")
        self.assertEqual(out["rfef-segunda-fs-fem"]["gender"], "femenino")


class HerenciaDeCalendarios(unittest.TestCase):
    """El run rápido (`--teams-only`) no descarga calendarios: los hereda del
    publicado. Es el sitio donde un descuido borra el autorrelleno por jornada
    de toda la app, o —peor— mete el calendario del año pasado."""

    PUBLICADO = {
        "version": "2026-2027",
        "categories": [{
            "id": "rfef",
            "divisions": [
                {"id": "d-plana", "calendar": [{"jornada": 1, "matches": []}]},
                {"id": "d-grupos", "groups": [
                    {"id": "g1", "calendar": [{"jornada": 1, "matches": []}]},
                    {"id": "g2", "calendar": [{"jornada": 2, "matches": []}]},
                ]},
            ],
        }],
    }

    def test_hereda_por_id_en_divisiones_y_grupos(self):
        cats = [{"id": "rfef", "divisions": [
            {"id": "d-plana", "teams": []},
            {"id": "d-grupos", "groups": [{"id": "g1"}, {"id": "g2"}]},
        ]}]
        n = scrape_main.inherit_calendars(cats, self.PUBLICADO)
        self.assertEqual(n, 3)
        self.assertEqual(cats[0]["divisions"][0]["calendar"][0]["jornada"], 1)
        self.assertEqual(
            cats[0]["divisions"][1]["groups"][1]["calendar"][0]["jornada"], 2)

    def test_no_pisa_un_calendario_recien_scrapeado(self):
        fresco = [{"jornada": 99, "matches": []}]
        cats = [{"id": "rfef", "divisions": [
            {"id": "d-plana", "calendar": fresco}]}]
        self.assertEqual(scrape_main.inherit_calendars(cats, self.PUBLICADO), 0)
        self.assertEqual(cats[0]["divisions"][0]["calendar"], fresco)

    def test_empareja_por_id_no_por_posicion(self):
        """Una división que la federación aún no ha abierto no está en este run.
        Emparejando por índice, su calendario se le pegaría a otra división."""
        cats = [{"id": "rfef", "divisions": [{"id": "d-grupos", "groups": [
            {"id": "g2"}]}]}]
        scrape_main.inherit_calendars(cats, self.PUBLICADO)
        self.assertEqual(
            cats[0]["divisions"][0]["groups"][0]["calendar"][0]["jornada"], 2)

    def test_id_desconocido_se_queda_sin_calendario(self):
        cats = [{"id": "rfef", "divisions": [{"id": "division-nueva"}]}]
        self.assertEqual(scrape_main.inherit_calendars(cats, self.PUBLICADO), 0)
        self.assertNotIn("calendar", cats[0]["divisions"][0])

    def test_publicado_de_otra_temporada_no_se_hereda(self):
        """El guard que impide reintroducir el bug original por la puerta de
        atrás: heredar el calendario de 2025-26 dentro de un JSON de 2026-27."""
        with mock.patch.object(scrape_main.urllib.request, "urlopen") as u:
            u.return_value.__enter__.return_value.read.return_value = \
                json.dumps({"version": "2025-2026", "categories": []}).encode()
            self.assertIsNone(scrape_main.fetch_published("2026-2027"))

    def test_sin_red_no_se_hereda_y_por_tanto_no_se_publica(self):
        with mock.patch.object(scrape_main.urllib.request, "urlopen",
                               side_effect=OSError("sin red")):
            self.assertIsNone(scrape_main.fetch_published("2026-2027"))

    def test_teams_only_no_publica_calendario_parcial(self):
        """Con 2 jornadas descargadas, publicarlas dejaría a la app ofreciendo
        un calendario de dos jornadas. Se retiran y las pone la herencia."""
        cfg = {"id": "rfef-primera-fs-fem", "name": "1a Fem", "gender": "femenino",
               "competition": {"code": "1", "name": "X"}, "flat": True,
               "groups": [Group(id="g1", code="2", name="G")]}
        cal = [{"jornada": 1, "matches": [{"home": "A", "away": "B"}]},
               {"jornada": 2, "matches": [{"home": "B", "away": "A"}]}]
        with mock.patch.object(rfef, "resolve_season", return_value=("22", "ok")), \
             mock.patch.object(rfef, "Fetcher"), \
             mock.patch.object(rfef, "discover_divisions", return_value=[cfg]), \
             mock.patch.object(rfef, "fetch_division_calendar", return_value=cal), \
             mock.patch.object(rfef, "fetch_division_teams", return_value=[]), \
             mock.patch.object(rfef, "fetch_web_sources", return_value={}), \
             mock.patch.object(rfef, "time"), \
             mock.patch.object(rfef, "_load_fallback",
                               return_value={"season": "2026-2027", "divisions": {}}):
            cat = rfef.scrape("2026-2027", resolve_badges=False, teams_only=True)
        # Por id, no por posición: desde que hay placeholders, `divisions[0]`
        # es la primera del orden canónico y no la que se ha descubierto.
        div = [d for d in cat["divisions"] if d["id"] == "rfef-primera-fs-fem"][0]
        self.assertEqual([t["name"] for t in div["teams"]], ["A", "B"])
        self.assertNotIn("calendar", div)   # <- lo importante
        self.assertTrue(cat["teamsOnly"])


class ElPdfLegacyTambienPasaElGuardDeTemporada(unittest.TestCase):
    """La rama del PDF legacy dentro de la cascada de equipos **no tenía** el
    guard, y era por donde entraba el fallo.

    `groups_url_pattern` de Segunda Femenina apunta a
    `calendario_grupo_N_segunda_femenina_futbol_sala.pdf`: una ruta sin la
    temporada dentro, que sigue devolviendo 200 y sigue sirviendo el fichero de
    2025-26 que la federación subió en agosto de aquel año.
    """

    CFG = {"groups_url_pattern": "https://ejemplo/grupo_{n}.pdf"}

    def test_rechaza_el_pdf_de_otra_temporada(self):
        viejo = (FIXTURES /
                 "calendario_grupo_2_segunda_femenina_2025-2026.pdf").read_bytes()
        with mock.patch.object(rfef, "_download_pdf", return_value=viejo):
            names = rfef._legacy_pdf_names(self.CFG, "2026-2027", 2, "seg-fem/g2")
        self.assertEqual(names, [])

    def test_acepta_el_de_la_temporada_pedida(self):
        nuevo = (FIXTURES / "calendario_2af_g2_2026-2027.pdf").read_bytes()
        with mock.patch.object(rfef, "_download_pdf", return_value=nuevo):
            names = rfef._legacy_pdf_names(self.CFG, "2026-2027", 2, "seg-fem/g2")
        self.assertEqual(len(names), 16)
        self.assertIn("AECS L Hospitalet", names)


class ElNombreCortoSustituyeAlOficial(unittest.TestCase):
    """La entrenadora ve "Burela FS"; "REYCO Burela FS" viaja como alias para
    casar con el calendario y con los partidos ya guardados."""

    def test_renombra_y_guarda_el_oficial(self):
        teams = [{"name": "REYCO Burela FS", "logoUrl": "u"}]
        out = rfef._with_short_names(teams, ["Burela FS"])
        self.assertEqual(out[0]["name"], "Burela FS")
        self.assertEqual(out[0]["officialName"], "REYCO Burela FS")
        self.assertEqual(out[0]["logoUrl"], "u", "el escudo no se pierde")

    def test_sin_pareja_se_queda_como_estaba(self):
        """Y **sin** el campo `officialName`, que solo tiene sentido cuando de
        verdad hay dos nombres."""
        teams = [{"name": "Atletico Navalcarnero", "logoUrl": None}]
        out = rfef._with_short_names(teams, ["Futsi Atlético B"])
        self.assertEqual(out[0]["name"], "Atletico Navalcarnero")
        self.assertNotIn("officialName", out[0])

    def test_no_se_marca_alias_si_es_el_mismo_nombre(self):
        teams = [{"name": "Ribera Navarra FS", "logoUrl": None}]
        out = rfef._with_short_names(teams, ["Ribera Navarra FS"])
        self.assertNotIn("officialName", out[0])


class UnCalendarioConEquiposInventadosNoSePublica(unittest.TestCase):
    """El corte de una fila larga en varias líneas físicas deja trozos sueltos
    ("MRB FS", "C.E."). La asimetría de `_calendar_is_sane` es a propósito: el
    mismo fallo visto por un lado inventa un rival y por el otro pierde uno."""

    ROSTER = ["MRB FS Mostoles", "C.D. Melistar", "REYCO Burela FS"]

    def _cal(self, pares):
        return [{"jornada": 1, "date": "2026-09-19",
                 "matches": [{"home": h, "away": a} for h, a in pares]}]

    def test_rechaza_el_que_nombra_a_quien_no_existe(self):
        cal = self._cal([("MRB FS", "C.D. Melistar")])  # <- trozo suelto
        self.assertFalse(rfef._calendar_is_sane(cal, self.ROSTER))

    def test_acepta_el_que_pierde_un_equipo(self):
        """Tirarlo entero dejaría sin autorrelleno a los otros quince para
        proteger a uno."""
        cal = self._cal([("MRB FS Mostoles", "C.D. Melistar")])
        self.assertTrue(rfef._calendar_is_sane(cal, self.ROSTER))

    def test_acepta_el_completo(self):
        cal = self._cal([("MRB FS Mostoles", "C.D. Melistar"),
                         ("REYCO Burela FS", "MRB FS Mostoles")])
        self.assertTrue(rfef._calendar_is_sane(cal, self.ROSTER))

    def test_sin_plantel_no_hay_nada_que_afirmar(self):
        cal = self._cal([("Cualquiera", "Otro")])
        self.assertTrue(rfef._calendar_is_sane(cal, None))
        self.assertTrue(rfef._calendar_is_sane(cal, []))


class FasesPublicadasComoDivisionPlana(unittest.TestCase):
    """Liga Prime se juega como Torneo Apertura + Torneo Clausura, y la PNFG los
    expone por el mismo `<select>` que los grupos territoriales. Publicarlos como
    grupos le pide a la entrenadora que elija entre dos listas idénticas — y en
    el JSON de agosto de 2026 salió publicado solo el Clausura."""

    def _discover(self, group_names):
        # `Competition` y no un Mock: `name` es un atributo reservado de
        # `Mock`, así que un `Mock(name=...)` no lleva dentro el nombre.
        comps = [Competition(code="1", name="Liga Prime Futsal")]
        groups = [Group(id=f"g{i+1}", code=str(i + 10), name=n)
                  for i, n in enumerate(group_names)]
        with mock.patch.object(rfef, "list_competitions", return_value=comps), \
             mock.patch.object(rfef, "list_groups", return_value=groups), \
             mock.patch.object(rfef, "time"):
            return rfef.discover_divisions("22")

    def test_apertura_y_clausura_se_publican_planas(self):
        cfgs = self._discover(["Torneo Apertura", "Torneo Clausura"])
        self.assertTrue(cfgs[0]["flat"])

    def test_los_grupos_territoriales_siguen_siendo_grupos(self):
        cfgs = self._discover(["Grupo 1", "Grupo 2"])
        self.assertFalse(cfgs[0]["flat"])


if __name__ == "__main__":
    unittest.main()
