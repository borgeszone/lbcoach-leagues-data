"""Tests del cortacircuitos del rate-limit y de la caché de jornadas.

Las dos piezas resuelven el mismo problema por los dos lados: la PNFG bloquea
devolviendo 200 con el cuerpo vacío, y hasta ahora eso costaba horas de
reintentos **y** pérdida de datos.

  - `RateLimitBreaker` deja de pegarle al servidor cuando está claro que niega.
  - `calendar_cache` hace que abandonar no signifique publicar menos.

Ninguno toca la red ni el fichero real de caché.

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

from scrapers import calendar_cache, rfef  # noqa: E402
from scrapers import rfef_calendario  # noqa: E402
from scrapers.rfef_calendario import RateLimitBreaker  # noqa: E402


class ElCortacircuitos(unittest.TestCase):
    """La aritmética, sin red."""

    def test_corta_el_grupo_tras_n_fallos_seguidos(self):
        b = RateLimitBreaker(per_group=3, run_budget=99)
        b.start_group("x")
        b.note_failure()
        b.note_failure()
        self.assertFalse(b.group_tripped)
        b.note_failure()
        self.assertTrue(b.group_tripped)

    def test_una_respuesta_buena_reinicia_la_racha(self):
        """Lo que corta es la racha, no el total.

        Sin esto, un grupo con un fallo aislado cada diez jornadas acabaría
        abandonado a mitad aunque el servidor esté contestando bien.
        """
        b = RateLimitBreaker(per_group=3, run_budget=99)
        b.start_group("x")
        b.note_failure()
        b.note_failure()
        b.note_success()
        b.note_failure()
        b.note_failure()
        self.assertFalse(b.group_tripped)

    def test_el_presupuesto_es_del_run_y_no_del_grupo(self):
        """Doce grupos con dos fallos describen el mismo servidor enfadado que
        un grupo con veinticuatro."""
        b = RateLimitBreaker(per_group=3, run_budget=6)
        for _ in range(3):
            b.start_group("g")
            b.note_failure()
            b.note_failure()
        self.assertTrue(b.blocked)

    def test_los_reintentos_se_hunden_tras_un_fallo_y_vuelven_solos(self):
        """Es lo que evita pagar 225 s por jornada en un run ya bloqueado."""
        b = RateLimitBreaker()
        self.assertEqual(b.retries_for(4), 4)
        b.note_failure()
        self.assertEqual(b.retries_for(4), 1)
        b.note_success()
        self.assertEqual(b.retries_for(4), 4)

    def test_sin_fallos_no_hay_nada_que_avisar(self):
        self.assertIsNone(RateLimitBreaker().summary())

    def test_el_aviso_distingue_recortado_de_agotado(self):
        """Un run recortado se parece demasiado a uno normal; el texto tiene que
        decir cuál de los dos fue."""
        parcial = RateLimitBreaker(run_budget=10)
        parcial.note_failure()
        self.assertIn("caché", parcial.summary())
        self.assertNotIn("presupuesto", parcial.summary())

        agotado = RateLimitBreaker(run_budget=2)
        agotado.note_failure()
        agotado.note_failure()
        self.assertIn("presupuesto", agotado.summary())


class ElCortacircuitosEnLaDescarga(unittest.TestCase):
    """`fetch_division_calendar` conducido contra un servidor de mentira."""

    def _run(self, respuestas, *, breaker=None, jornadas=15):
        """`respuestas` mapea nº de jornada -> html o None. Devuelve
        `(calendario, jornadas_pedidas)`."""
        pedidas = []

        def fake_fetch(session, comp, grupo, jornada, temporada, retries):
            pedidas.append(jornada)
            return respuestas.get(jornada)

        with mock.patch.object(rfef_calendario, "_fetch_jornada_html", fake_fetch), \
             mock.patch.object(rfef_calendario, "_parse_jornada_numbers",
                               return_value=list(range(1, jornadas + 1))), \
             mock.patch.object(rfef_calendario, "_parse_matches",
                               return_value=[{"home": "A", "away": "B", "date": None}]), \
             mock.patch.object(rfef_calendario.time, "sleep"):
            cal = rfef_calendario.fetch_division_calendar(
                "C", "G", temporada_code="22", session=object(),
                breaker=breaker,
            )
        return cal, pedidas

    def test_abandona_el_grupo_y_deja_de_pedir(self):
        """Tres seguidas sin respuesta y no se piden las doce restantes.

        Es el ahorro que motiva todo: a 225 s por jornada fallida, seguir hasta
        el final del grupo son 45 minutos de reintentos contra un servidor que
        ya ha dicho que no.
        """
        respuestas = {1: "<html>", 2: "<html>"}  # de la 3 en adelante, nada
        cal, pedidas = self._run(respuestas, breaker=RateLimitBreaker(per_group=3))
        self.assertEqual(max(pedidas), 5, f"pidió hasta la J{max(pedidas)}")
        self.assertEqual(len(cal), 2)

    def test_un_hueco_aislado_no_corta_el_grupo(self):
        respuestas = {n: "<html>" for n in range(1, 16)}
        del respuestas[7]
        cal, pedidas = self._run(respuestas, breaker=RateLimitBreaker(per_group=3))
        self.assertEqual(max(pedidas), 15)
        self.assertEqual(len(cal), 14)

    def test_con_el_presupuesto_agotado_no_se_pide_ni_la_j1(self):
        """El corte global tiene que ahorrar el grupo **entero**, no su cola."""
        b = RateLimitBreaker(run_budget=1)
        b.note_failure()
        cal, pedidas = self._run({1: "<html>"}, breaker=b)
        self.assertEqual(pedidas, [])
        self.assertEqual(cal, [])

    def test_el_grupo_que_falla_del_todo_cuenta_como_abandonado(self):
        b = RateLimitBreaker()
        self._run({}, breaker=b)
        self.assertEqual(b.groups_abandoned, 1)
        self.assertEqual(b.failures, 1)


class LaCacheDeJornadas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = pathlib.Path(self._tmp.name) / "calendar-cache.json"
        patcher = mock.patch.object(calendar_cache, "CACHE_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = path
        self._reset()
        self.addCleanup(self._reset)

    def _reset(self):
        calendar_cache._cache = None
        calendar_cache._calendars = None

    def _cal(self, nums, *, hora=True):
        return [{"jornada": n,
                 "matches": [{"home": f"A{n}", "away": f"B{n}",
                              "date": f"2026-09-{n:02d}T19:30:00" if hora
                              else f"2026-09-{n:02d}"}]}
                for n in nums]

    def test_round_trip(self):
        calendar_cache.store_jornadas("C", "G", self._cal([1, 2]))
        calendar_cache.save_cache()
        self._reset()
        got = calendar_cache.lookup_jornadas("C", "G")
        self.assertEqual(sorted(got), [1, 2])
        self.assertEqual(got[1][0]["home"], "A1")

    def test_guardar_dos_jornadas_no_borra_las_treinta(self):
        """El cron rápido baja dos jornadas seis días de cada siete.

        Si `store_jornadas` reemplazara el grupo, la caché quedaría con dos de
        treinta cada lunes por la mañana — o sea, se vaciaría sola.
        """
        calendar_cache.store_jornadas("C", "G", self._cal(range(1, 31)))
        calendar_cache.store_jornadas("C", "G", self._cal([1, 2]))
        self.assertEqual(len(calendar_cache.lookup_jornadas("C", "G")), 30)

    def test_una_jornada_sin_partidos_no_se_guarda(self):
        """Es lo que devuelve una página que contestó pero no traía nada;
        cachearla convertiría un fallo en un dato."""
        calendar_cache.store_jornadas("C", "G", [{"jornada": 1, "matches": []}])
        self.assertEqual(calendar_cache.lookup_jornadas("C", "G"), {})

    def test_devuelve_copias(self):
        """Quien fusiona muta los partidos para añadirles el actaUrl."""
        calendar_cache.store_jornadas("C", "G", self._cal([1]))
        got = calendar_cache.lookup_jornadas("C", "G")
        got[1][0]["actaUrl"] = "http://x"
        self.assertNotIn("actaUrl", calendar_cache.lookup_jornadas("C", "G")[1][0])

    def test_las_actas_siguen_funcionando_con_la_seccion_nueva(self):
        """Las dos cachés comparten fichero: añadir una no puede romper la otra."""
        calendar_cache.store("C", "G", 3, "Casa", "Fuera", "http://acta")
        calendar_cache.store_jornadas("C", "G", self._cal([1]))
        calendar_cache.save_cache()
        self._reset()
        self.assertEqual(
            calendar_cache.lookup("C", "G", 3, "Casa", "Fuera"), "http://acta")
        self.assertEqual(len(calendar_cache.lookup_jornadas("C", "G")), 1)

    def test_lee_un_fichero_del_formato_viejo(self):
        """El fichero en producción tiene 229 actas planas y ninguna jornada."""
        self.path.write_text(json.dumps({
            "_comment": "lo de siempre",
            "C|G|J3|casa|fuera": "http://acta",
        }), encoding="utf-8")
        self._reset()
        self.assertEqual(
            calendar_cache.lookup("C", "G", 3, "Casa", "Fuera"), "http://acta")
        self.assertEqual(calendar_cache.lookup_jornadas("C", "G"), {})

    def test_una_entrada_corrupta_no_se_lleva_a_las_buenas(self):
        self.path.write_text(json.dumps({
            "_calendars": {
                "C|G": {"1": [{"home": "A", "away": "B"}],
                        "2": "esto no es una lista",
                        "tres": [{"home": "C", "away": "D"}]},
                "roto": [],
            }
        }), encoding="utf-8")
        self._reset()
        self.assertEqual(sorted(calendar_cache.lookup_jornadas("C", "G")), [1])

    def test_un_fichero_ilegible_no_rompe_el_run(self):
        self.path.write_text("{ esto no es json", encoding="utf-8")
        self._reset()
        self.assertEqual(calendar_cache.lookup_jornadas("C", "G"), {})


class LaFusionConLaCache(unittest.TestCase):
    """`_merge_calendar_cache`: qué gana a qué."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(
            calendar_cache, "CACHE_PATH",
            pathlib.Path(self._tmp.name) / "c.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        calendar_cache._cache = None
        calendar_cache._calendars = None
        self.addCleanup(self._reset)

    def _reset(self):
        calendar_cache._cache = None
        calendar_cache._calendars = None

    def _j(self, n, *, home="A", date="2026-09-01T19:30:00", phase=None):
        j = {"jornada": n, "matches": [{"home": home, "away": "B", "date": date}]}
        if phase:
            j["phase"] = phase
        return j

    def test_rellena_la_jornada_que_falta(self):
        """El caso real: Primera masculina publicó 14 de 15 el 2026-08-31.

        El fallback al PDF es `if not calendar` —un booleano sobre la lista— así
        que con 14 jornadas nunca se consulta, y la J12 se perdía.
        """
        calendar_cache.store_jornadas("C", "G", [self._j(12)])
        fresco = [self._j(n) for n in range(1, 16) if n != 12]
        out = rfef._merge_calendar_cache(fresco, "C", "G", label="x")
        self.assertEqual([j["jornada"] for j in out], list(range(1, 16)))

    def test_lo_fresco_gana_a_la_cache(self):
        """Sin esta regla, un aplazamiento se quedaría con la fecha vieja para
        siempre."""
        calendar_cache.store_jornadas(
            "C", "G", [self._j(1, date="2026-09-01T19:30:00")])
        fresco = [self._j(1, date="2026-11-20T21:00:00")]
        out = rfef._merge_calendar_cache(fresco, "C", "G", label="x")
        self.assertEqual(out[0]["matches"][0]["date"], "2026-11-20T21:00:00")

    def test_la_cache_con_hora_gana_al_pdf_sin_hora(self):
        """Un run que cae al PDF trae las 30 jornadas con el día pelado.

        Sin la excepción, ese run tiraría todas las horas que ya teníamos y la
        cobertura iría hacia atrás en cada bloqueo — justo lo contrario de lo
        que la caché promete.
        """
        calendar_cache.store_jornadas(
            "C", "G", [self._j(1, date="2026-09-19T18:00:00")])
        del_pdf = [self._j(1, date="2026-09-19")]
        out = rfef._merge_calendar_cache(del_pdf, "C", "G", label="x")
        self.assertEqual(out[0]["matches"][0]["date"], "2026-09-19T18:00:00")

    def test_el_calendario_por_fases_no_se_fusiona(self):
        """En Liga Prime hay dos J1 (Apertura y Clausura) dentro del mismo PDF,
        así que el número de jornada no identifica un partido."""
        calendar_cache.store_jornadas("C", "G", [self._j(1, home="CACHE")])
        fases = [self._j(1, home="APERTURA", phase="Apertura"),
                 self._j(1, home="CLAUSURA", phase="Clausura")]
        out = rfef._merge_calendar_cache(fases, "C", "G", label="x")
        self.assertEqual(len(out), 2)
        self.assertEqual([j["matches"][0]["home"] for j in out],
                         ["APERTURA", "CLAUSURA"])

    def test_sin_cache_devuelve_lo_mismo(self):
        fresco = [self._j(1)]
        self.assertIs(rfef._merge_calendar_cache(fresco, "C", "G", label="x"),
                      fresco)

    def test_el_grupo_que_falla_del_todo_se_publica_desde_la_cache(self):
        calendar_cache.store_jornadas("C", "G", [self._j(n) for n in (1, 2, 3)])
        out = rfef._merge_calendar_cache([], "C", "G", label="x")
        self.assertEqual([j["jornada"] for j in out], [1, 2, 3])


class SoloSeCacheaLoDeLaPnfg(unittest.TestCase):
    """El PDF no puede entrar en la caché: iría hacia atrás."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(
            calendar_cache, "CACHE_PATH",
            pathlib.Path(self._tmp.name) / "c.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        calendar_cache._cache = None
        calendar_cache._calendars = None
        self.addCleanup(self._reset)

    def _reset(self):
        calendar_cache._cache = None
        calendar_cache._calendars = None

    def test_el_fallback_pdf_no_se_guarda(self):
        del_pdf = [{"jornada": 1,
                    "matches": [{"home": "A", "away": "B", "date": "2026-09-19"}]}]
        with mock.patch.object(rfef, "fetch_division_calendar", return_value=[]), \
             mock.patch.object(rfef.time, "sleep"):
            out = rfef._calendar_for_group(
                "C", "G", "22", None, RateLimitBreaker(),
                label="x", pdf=lambda: del_pdf)
        self.assertEqual(len(out), 1)
        self.assertEqual(calendar_cache.lookup_jornadas("C", "G"), {},
                         "el PDF no puede acabar en la caché")

    def test_lo_de_la_pnfg_si_se_guarda(self):
        fresco = [{"jornada": 1,
                   "matches": [{"home": "A", "away": "B",
                                "date": "2026-09-19T19:30:00"}]}]
        with mock.patch.object(rfef, "fetch_division_calendar", return_value=fresco), \
             mock.patch.object(rfef.time, "sleep"):
            rfef._calendar_for_group("C", "G", "22", None, RateLimitBreaker(),
                                     label="x", pdf=lambda: [])
        self.assertEqual(len(calendar_cache.lookup_jornadas("C", "G")), 1)


if __name__ == "__main__":
    unittest.main()
