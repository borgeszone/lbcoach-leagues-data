"""Tests del calendario completo en una petición (`NFG_VisCalendario_Vis`).

Las fixtures son páginas reales de la PNFG, bajadas el 2026-10-25 con
`CDetalle=1`: Segunda Femenina G1 y las dos fases de Liga Prime. Ninguno toca
la red ni el fichero real de caché.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from scrapers import calendar_cache, pnfg_pacer, rfef, rfef_calendario  # noqa: E402
from scrapers.rfef_calendario import (  # noqa: E402
    RateLimitBreaker,
    acta_jornadas,
    attach_actas,
    fetch_season_calendar,
    parse_season_calendar,
)
from scrapers.rfef_discovery import Group  # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"


def _html(nombre: str) -> str:
    return (FIX / f"calendario-completo_{nombre}_2026-2027.html").read_bytes() \
        .decode("iso-8859-15")


class _Resp:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.content = body
        self.status_code = status


class LaPaginaSeLee(unittest.TestCase):
    """`parse_season_calendar` sobre la página real."""

    @classmethod
    def setUpClass(cls):
        cls.cal = parse_season_calendar(_html("segunda-fem-g1"))

    def test_las_30_jornadas_con_sus_8_partidos(self):
        self.assertEqual([j["jornada"] for j in self.cal], list(range(1, 31)))
        self.assertTrue(all(len(j["matches"]) == 8 for j in self.cal))

    def test_los_16_equipos_y_ningun_numero_colado(self):
        """El marcador va entre los dos nombres y está ofuscado: no puede acabar
        pegado a ninguno."""
        equipos = {m[k] for j in self.cal for m in j["matches"] for k in ("home", "away")}
        self.assertEqual(len(equipos), 16)
        self.assertIn("At. Arnoia", equipos)
        self.assertFalse(any(e[0].isdigit() or e[-1].isdigit() for e in equipos))

    def test_fecha_hora_y_pabellon_de_un_partido(self):
        m = self.cal[0]["matches"][0]
        self.assertEqual((m["home"], m["away"]), ("ENCE Marín Futsal", "REYCO Burela FS"))
        self.assertEqual(m["date"], "2026-09-19T17:45:00")
        self.assertEqual(m["venue"], "Pab. A Raña (PIS)")
        self.assertEqual(self.cal[0]["date"], "2026-09-19")

    def test_sin_pabellon_ni_hora_no_se_inventan(self):
        """J8: "Pendiente de asignar" y el día a secas."""
        j8 = self.cal[7]["matches"]
        pendiente = [m for m in j8 if "venue" not in m]
        self.assertTrue(pendiente)
        self.assertTrue(all("T" not in m["date"] for m in pendiente))

    def test_las_00_00_son_sin_hora(self):
        """El Clausura entero va a "00:00": eso es "sin hora", no medianoche."""
        clausura = parse_season_calendar(_html("liga-prime-clausura"))
        fechas = [m["date"] for j in clausura for m in j["matches"]]
        self.assertEqual(len(fechas), 120)
        self.assertFalse(any("T" in f for f in fechas))
        self.assertEqual(clausura[0]["matches"][0]["date"], "2027-01-09")

    def test_liga_prime_usa_los_nombres_de_la_lista_de_equipos(self):
        """Los del calendario viejo eran otros ("C.A. Osasuna Magna", "FS
        García"), y por eso la app no reconocía al equipo propio."""
        apertura = parse_season_calendar(_html("liga-prime-apertura"))
        equipos = {m[k] for j in apertura for m in j["matches"] for k in ("home", "away")}
        self.assertIn("CD Xota FS", equipos)
        self.assertIn("Catgas Energía Santa Coloma", equipos)
        self.assertEqual(len(apertura), 15)


class LaPeticion(unittest.TestCase):
    """`fetch_season_calendar`: una petición y nunca un reintento."""

    def setUp(self):
        pnfg_pacer.reset()
        self.addCleanup(pnfg_pacer.reset)

    def _get(self, *respuestas, breaker=None):
        s = mock.Mock()
        s.get.side_effect = list(respuestas)
        out = fetch_season_calendar("C", "G", temporada_code="22", session=s,
                                    breaker=breaker)
        return out, s

    def test_la_pagina_buena(self):
        out, s = self._get(_Resp(_html("segunda-fem-g1").encode("iso-8859-15")))
        self.assertEqual(len(out), 30)
        params = s.get.call_args.kwargs["params"]
        self.assertEqual(params["CDetalle"], "1")
        self.assertEqual(params["codtemporada"], "22")

    def test_el_corte_para_el_run(self):
        out, s = self._get(_Resp(b""), _Resp(b"<html>"))
        self.assertIsNone(out)
        self.assertEqual(s.get.call_count, 1)
        self.assertTrue(pnfg_pacer.stopped())

    def test_una_pagina_sin_jornadas_cae_al_respaldo(self):
        out, _ = self._get(_Resp(b"<html><body>otra cosa</body></html>"))
        self.assertIsNone(out)
        self.assertFalse(pnfg_pacer.stopped())

    def test_un_error_de_red_no_se_reintenta(self):
        out, s = self._get(requests.ConnectionError("x"), _Resp(b"<html>"))
        self.assertIsNone(out)
        self.assertEqual(s.get.call_count, 1)

    def test_con_el_run_cortado_no_se_pide(self):
        pnfg_pacer.wait_out_block()
        out, s = self._get(_Resp(b"<html>"), breaker=RateLimitBreaker())
        self.assertIsNone(out)
        s.get.assert_not_called()


class LasActas(unittest.TestCase):
    """Solo se pide `NFG_CmpJornada` para lo jugado, reciente y sin acta."""

    HOY = date(2026, 10, 28)

    def _j(self, num, dias, acta=None):
        m = {"home": "A", "away": "B",
             "date": (self.HOY + timedelta(days=dias)).isoformat()}
        if acta:
            m["actaUrl"] = acta
        return {"jornada": num, "matches": [m]}

    def test_jugadas_y_recientes_la_ultima_primero(self):
        cal = [self._j(1, -30), self._j(2, -16), self._j(3, -2), self._j(4, 5)]
        self.assertEqual(acta_jornadas(cal, lambda n, m: False, today=self.HOY),
                         [3, 2, 1])

    def test_ni_hoy_ni_futuras(self):
        cal = [self._j(1, 0), self._j(2, 7)]
        self.assertEqual(acta_jornadas(cal, lambda n, m: False, today=self.HOY), [])

    def test_las_que_ya_tienen_acta_no(self):
        cal = [self._j(1, -9, acta="u"), self._j(2, -2)]
        self.assertEqual(acta_jornadas(cal, lambda n, m: n == 2, today=self.HOY), [])

    def test_un_aplazado_viejo_no_se_pide_para_siempre(self):
        cal = [self._j(1, -60)]
        self.assertEqual(acta_jornadas(cal, lambda n, m: False, today=self.HOY), [])

    def test_se_casa_por_los_dos_nombres(self):
        ms = [{"home": "A", "away": "B"}, {"home": "C", "away": "D"}]
        n = attach_actas(ms, [{"home": "C", "away": "D", "actaUrl": "cd"},
                              {"home": "A", "away": "B", "actaUrl": "ab"}])
        self.assertEqual((n, ms[0]["actaUrl"], ms[1]["actaUrl"]), (2, "ab", "cd"))

    def test_y_si_un_nombre_cambia_por_el_otro(self):
        """"At. Arnoia" en la vista completa, "At. Arnoya" en la de jornada."""
        ms = [{"home": "CDE Muslera", "away": "At. Arnoia"}]
        attach_actas(ms, [{"home": "CDE Muslera", "away": "At. Arnoya", "actaUrl": "u"},
                          {"home": "X", "away": "Y", "actaUrl": "v"}])
        self.assertEqual(ms[0]["actaUrl"], "u")

    def test_si_un_lado_casa_con_dos_no_se_afirma_nada(self):
        ms = [{"home": "A", "away": "Z"}]
        attach_actas(ms, [{"home": "A", "away": "B", "actaUrl": "u"},
                          {"home": "Q", "away": "Z", "actaUrl": "v"}])
        self.assertNotIn("actaUrl", ms[0])


class _CacheTemporal(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(calendar_cache, "CACHE_PATH",
                              pathlib.Path(tmp.name) / "c.json")
        p.start()
        self.addCleanup(p.stop)
        self._reset()
        self.addCleanup(self._reset)
        pnfg_pacer.reset()
        self.addCleanup(pnfg_pacer.reset)

    def _reset(self):
        calendar_cache._cache = None
        calendar_cache._calendars = None


class ElGrupo(_CacheTemporal):
    """`rfef._calendar_for_group` con la vista completa por delante."""

    def _cal(self, dias_por_jornada):
        hoy = date.today()
        return [{"jornada": n, "matches": [{"home": f"L{n}", "away": f"V{n}",
                 "date": (hoy + timedelta(days=d)).isoformat()}]}
                for n, d in enumerate(dias_por_jornada, start=1)]

    def _run(self, completo, *, max_jornadas=None, actas=None):
        meta: dict = {}
        with mock.patch.object(rfef, "fetch_season_calendar", return_value=completo), \
             mock.patch.object(rfef, "fetch_division_calendar", return_value=[]) as viejo, \
             mock.patch.object(rfef, "fetch_jornada_matches",
                               return_value=actas or {}) as jornadas, \
             mock.patch.object(rfef.time, "sleep"):
            out = rfef._calendar_for_group(
                "C", "G", "22", max_jornadas, RateLimitBreaker(),
                label="x", pdf=lambda: [], meta=meta)
        return out, viejo, jornadas, meta

    def test_con_la_vista_no_se_pide_jornada_a_jornada(self):
        out, viejo, _, meta = self._run(self._cal([10, 17, 24]))
        self.assertEqual(len(out), 3)
        viejo.assert_not_called()
        self.assertEqual(meta["total"], 3)
        self.assertEqual(len(calendar_cache.lookup_jornadas("C", "G")), 3)

    def test_solo_se_piden_las_jugadas_sin_acta(self):
        actas = {2: [{"home": "L2", "away": "V2", "actaUrl": "acta2"}]}
        out, _, jornadas, _ = self._run(self._cal([-9, -2, 5]), actas=actas)
        self.assertEqual(jornadas.call_args.args[2], [2, 1])
        self.assertEqual(out[1]["matches"][0]["actaUrl"], "acta2")

    def test_lo_que_ya_esta_en_la_cache_no_se_vuelve_a_pedir(self):
        calendar_cache.store("C", "G", 1, "L1", "V1", "acta1")
        _, _, jornadas, _ = self._run(self._cal([-9, 5]))
        jornadas.assert_not_called()

    def test_solo_equipos_no_pide_actas(self):
        out, _, jornadas, _ = self._run(self._cal([-9, -2, 5]), max_jornadas=2)
        self.assertEqual(len(out), 2)
        jornadas.assert_not_called()

    def test_sin_la_vista_vuelve_el_camino_de_siempre(self):
        _, viejo, _, _ = self._run(None)
        viejo.assert_called_once()

    def test_el_tope_de_actas_deja_las_viejas_para_otro_dia(self):
        """`NFG_CmpJornada` es la página del cupo de ~11: un atraso grande se
        reparte en varios runs, la más reciente primero."""
        tope = rfef._ActaBudget(total=8, per_group=2)
        with mock.patch.object(rfef, "fetch_season_calendar",
                               return_value=self._cal([-30, -23, -16, -9, -2])), \
             mock.patch.object(rfef, "fetch_jornada_matches", return_value={}) as j, \
             mock.patch.object(rfef.time, "sleep"):
            rfef._calendar_for_group("C", "G", "22", None, RateLimitBreaker(),
                                     label="x", pdf=lambda: [], actas=tope)
        self.assertEqual(j.call_args.args[2], [5, 4])
        self.assertEqual(tope.left, 6)


class ElTopeDeActas(unittest.TestCase):
    def test_por_grupo_y_en_total(self):
        tope = rfef._ActaBudget(total=3, per_group=2)
        self.assertEqual(tope.take([9, 8, 7]), [9, 8])
        self.assertEqual(tope.take([5, 4]), [5])
        self.assertEqual(tope.take([3]), [])

    def test_lo_que_va_en_produccion_cabe_en_el_cupo(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            tope = rfef._acta_budget()
        self.assertLess(tope.left, 11)


class LasRedireccionesFueraNoCuentan(unittest.TestCase):
    """La portada redirige a otros dominios: eso no gasta cupo de la PNFG."""

    def _r(self, url, history=()):
        r = mock.Mock()
        r.url, r.history = url, [mock.Mock(url=u) for u in history]
        return r

    def _cuenta(self, respuesta):
        from scrapers import rfef_clasificacion
        s = rfef_clasificacion.PacedSession()
        with mock.patch.object(pnfg_pacer.PACER, "wait"), \
             mock.patch.object(pnfg_pacer.PACER, "count") as count, \
             mock.patch("requests.Session.request", return_value=respuesta):
            s.get("https://resultados.rfef.es/x")
        return sum(c.args[0] for c in count.call_args_list)

    def test_dos_saltos_dentro_de_la_pnfg_son_dos_mas(self):
        pnfg = "https://resultados.rfef.es/pnfg/"
        self.assertEqual(self._cuenta(self._r(pnfg + "a", [pnfg + "a", pnfg + "NLogin"])), 2)

    def test_un_salto_a_otro_dominio_no_cuenta(self):
        self.assertEqual(self._cuenta(self._r(
            "https://www.rfef.es/", ["https://resultados.rfef.es/"])), 0)


class LigaPrimeConSusDosFases(_CacheTemporal):
    """Antes solo se tomaba el Apertura: desde enero faltaban quince jornadas."""

    def test_apertura_y_clausura_seguidas_y_con_su_fase(self):
        paginas = {"33892849": parse_season_calendar(_html("liga-prime-apertura")),
                   "33892850": parse_season_calendar(_html("liga-prime-clausura"))}
        cfg = {"id": "rfef-primera-fs-masc", "name": "Primera",
               "competition": {"code": "33836159", "name": "Liga Prime"},
               "flat": True,
               "groups": [Group("apertura", "33892849", "Torneo Apertura"),
                          Group("clausura", "33892850", "Torneo Clausura")]}
        div = {"id": "rfef-primera-fs-masc"}
        with mock.patch.object(rfef, "fetch_season_calendar",
                               side_effect=lambda c, g, **kw: paginas[g]), \
             mock.patch.object(rfef, "fetch_jornada_matches", return_value={}), \
             mock.patch.object(rfef, "make_session") as sesion, \
             mock.patch.object(rfef.time, "sleep"):
            rfef._attach_calendars([div], "2026-2027", [cfg], "22")
        cal = div["calendar"]
        self.assertEqual(len(cal), 30)
        self.assertEqual({j["phase"] for j in cal[:15]}, {"Apertura"})
        self.assertEqual({j["phase"] for j in cal[15:]}, {"Clausura"})
        self.assertEqual([j["jornada"] for j in cal[15:]], list(range(1, 16)))
        self.assertEqual(div["jornadasTotal"], 30)
        sesion.assert_not_called()  # todo simulado: ni el GET de siembra

    def test_el_pdf_de_respaldo_se_reparte_por_fase(self):
        pdf = [{"jornada": 1, "phase": "Apertura", "matches": []},
               {"jornada": 1, "phase": "Clausura", "matches": [{"home": "A"}]}]
        self.assertEqual(rfef._only_phase(pdf, "Clausura"),
                         [{"jornada": 1, "matches": [{"home": "A"}]}])
        self.assertEqual(rfef._phase_name("Torneo Clausura"), "Clausura")


class ElDescubrimientoGuardado(unittest.TestCase):
    """Descubrir son 7 peticiones de la familia del cupo: en Actions, el
    2026-10-28, la decimocuarta de esa familia se cortó."""

    CFG = {"id": "rfef-primera-fs-fem", "name": "1a Fem", "gender": "femenino",
           "competition": {"code": "1", "name": "X"}, "flat": True,
           "groups": [Group(id="g1", code="2", name="G")]}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / "discovery-cache.json"

    def test_ida_y_vuelta(self):
        rfef.save_discovery(self.path, "2026-2027", "22", [self.CFG], date(2026, 10, 28))
        code, cfgs = rfef.load_discovery(self.path, "2026-2027", date(2026, 11, 2))
        self.assertEqual(code, "22")
        self.assertEqual(cfgs[0]["groups"][0], Group(id="g1", code="2", name="G"))

    def test_caduca_a_la_semana(self):
        rfef.save_discovery(self.path, "2026-2027", "22", [self.CFG], date(2026, 10, 28))
        self.assertIsNone(rfef.load_discovery(self.path, "2026-2027", date(2026, 11, 4)))

    def test_en_verano_vale_solo_el_mismo_dia(self):
        """En agosto las divisiones van abriendo de una en una."""
        rfef.save_discovery(self.path, "2026-2027", "22", [self.CFG], date(2026, 8, 10))
        self.assertIsNotNone(rfef.load_discovery(self.path, "2026-2027", date(2026, 8, 10)))
        self.assertIsNone(rfef.load_discovery(self.path, "2026-2027", date(2026, 8, 11)))

    def test_otra_temporada_no_vale(self):
        """El guard de temporada de §6.32: nunca códigos de otro año."""
        rfef.save_discovery(self.path, "2025-2026", "21", [self.CFG], date(2026, 10, 28))
        self.assertIsNone(rfef.load_discovery(self.path, "2026-2027", date(2026, 10, 28)))

    def test_roto_o_ausente_se_ignora(self):
        self.assertIsNone(rfef.load_discovery(self.path, "2026-2027", date(2026, 10, 28)))
        self.path.write_text("{no es json", encoding="utf-8")
        self.assertIsNone(rfef.load_discovery(self.path, "2026-2027", date(2026, 10, 28)))

    def _scrape(self, **kw):
        with mock.patch.object(rfef, "resolve_season", return_value=("22", "ok")) as res,              mock.patch.object(rfef, "Fetcher"),              mock.patch.object(rfef, "discover_divisions", return_value=[self.CFG]) as disc,              mock.patch.object(rfef, "_attach_calendars") as cal,              mock.patch.object(rfef, "fetch_division_teams", return_value=[]),              mock.patch.object(rfef, "fetch_web_sources", return_value={}),              mock.patch.object(rfef, "time"),              mock.patch.object(rfef, "_load_fallback",
                               return_value={"season": "2026-2027", "divisions": {}}):
            rfef.scrape("2026-2027", resolve_badges=False, **kw)
        return res, disc, cal

    def test_con_lo_guardado_no_se_pregunta_y_las_actas_tienen_el_cupo(self):
        rfef.save_discovery(self.path, "2026-2027", "22", [self.CFG], date.today())
        res, disc, cal = self._scrape(discovery_cache=self.path)
        res.assert_not_called()
        disc.assert_not_called()
        self.assertIsNone(cal.call_args.kwargs["acta_total"])

    def test_el_dia_que_se_descubre_las_actas_bajan_y_se_guarda(self):
        res, disc, cal = self._scrape(discovery_cache=self.path)
        disc.assert_called_once()
        self.assertEqual(cal.call_args.kwargs["acta_total"], 3)
        self.assertIsNotNone(rfef.load_discovery(self.path, "2026-2027", date.today()))

    def test_el_tope_reducido_no_sube_el_de_produccion(self):
        with mock.patch.dict("os.environ", {"PNFG_ACTAS_MAX": "2"}):
            self.assertEqual(rfef._acta_budget(3).left, 2)


if __name__ == "__main__":
    unittest.main()
