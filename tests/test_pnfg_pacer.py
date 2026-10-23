"""Tests del ritmo contra la PNFG y de cómo se espera un bloqueo.

El límite que motiva todo se midió el 2026-10-04 (ver `scrapers/pnfg_pacer.py`):
~12 peticiones por IP cada 5 minutos, y el bloqueo es de la IP, no de la sesión.
Por eso cada grupo del calendario moría siempre entre la J12 y la J15.

Ninguno toca la red ni duerme de verdad.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import json  # noqa: E402
import tempfile  # noqa: E402
from datetime import date  # noqa: E402

from scrapers import calendar_cache, pnfg_pacer, rfef_calendario, rfef_clasificacion  # noqa: E402
from scrapers.pnfg_pacer import Pacer  # noqa: E402


class _Reloj:
    def __init__(self) -> None:
        self.t = 1000.0
        self.dormido: list[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.dormido.append(s)
        self.t += s


class ElRitmo(unittest.TestCase):
    def setUp(self) -> None:
        self.r = _Reloj()
        self.p = Pacer(26, clock=self.r.clock, sleep=self.r.sleep)

    def test_la_primera_peticion_sale_ya(self):
        self.p.wait()
        self.assertEqual(self.r.dormido, [])

    def test_dos_seguidas_quedan_separadas_por_el_intervalo(self):
        self.p.wait()
        self.r.t += 4  # el `jornada_delay` de siempre
        self.p.wait()
        self.assertEqual(self.r.dormido, [22])

    def test_si_ya_paso_el_intervalo_no_se_espera(self):
        self.p.wait()
        self.r.t += 60
        self.p.wait()
        self.assertEqual(self.r.dormido, [])

    def test_tras_esperar_un_bloqueo_no_se_suma_el_intervalo(self):
        self.p.wait()
        self.p.note_block()
        self.p.wait()
        self.assertEqual(self.r.dormido, [])



class LaEsperaDelCorte(unittest.TestCase):
    """`wait_out_block`: escalonada, en silencio, y con tope."""

    def test_escalones_y_tope(self):
        with mock.patch.object(pnfg_pacer, "_sleep") as dormir:
            for n in range(len(pnfg_pacer.BLOCK_WAITS)):
                self.assertTrue(pnfg_pacer.wait_out_block(n))
            self.assertFalse(pnfg_pacer.wait_out_block(len(pnfg_pacer.BLOCK_WAITS)))
        self.assertEqual([c.args[0] for c in dormir.call_args_list],
                         list(pnfg_pacer.BLOCK_WAITS))

    def test_el_limite_de_quien_llama_manda(self):
        """Tras un fallo el cortacircuitos baja `retries` a 1: una espera."""
        with mock.patch.object(pnfg_pacer, "_sleep") as dormir:
            self.assertTrue(pnfg_pacer.wait_out_block(0, limit=1))
            self.assertFalse(pnfg_pacer.wait_out_block(1, limit=1))
            self.assertFalse(pnfg_pacer.wait_out_block(0, limit=0))
        self.assertEqual(dormir.call_count, 1)

    def test_la_primera_espera_cubre_el_corte_medido(self):
        """Medido: a los 295 s seguía cerrado y a los 356 abría."""
        self.assertGreater(pnfg_pacer.BLOCK_WAITS[0], 356)

    def test_las_esperas_crecen(self):
        """El corte escala con la reincidencia: esperar lo mismo otra vez es
        volver a pedir dentro del mismo corte."""
        w = pnfg_pacer.BLOCK_WAITS
        self.assertEqual(list(w), sorted(set(w)))


class ElTopeDeTiempo(unittest.TestCase):
    """El job de Actions muere a las 6 h sin publicar: mejor parar a tiempo."""

    def test_pasado_el_tope_no_se_pide_nada_mas(self):
        reloj = _Reloj()
        b = rfef_calendario.RateLimitBreaker(time_budget_s=600, clock=reloj.clock)
        self.assertFalse(b.blocked)
        reloj.t += 601
        self.assertTrue(b.blocked)
        self.assertIn("se acabó el tiempo", b.summary())

    def test_sin_tope_no_caduca(self):
        b = rfef_calendario.RateLimitBreaker()
        self.assertFalse(b.out_of_time)
        self.assertIsNone(b.summary())


class LaSesionLlevaElRitmo(unittest.TestCase):
    """El cupo es de la IP: tiene que pagarlo cualquier petición al host, de
    cualquier módulo, y ninguna de otro host."""

    def _peticion(self, url):
        s = rfef_clasificacion.PacedSession()
        with mock.patch.object(pnfg_pacer.PACER, "wait") as wait, \
             mock.patch("requests.Session.request", return_value="ok"):
            s.get(url)
        return wait.call_count

    def test_la_pnfg_paga(self):
        self.assertEqual(self._peticion(
            "https://resultados.rfef.es/pnfg/NPcd/NFG_CmpJornada"), 1)

    def test_otro_host_no_paga(self):
        self.assertEqual(self._peticion("https://rfef.es/competiciones"), 0)
        self.assertEqual(self._peticion("https://rfef.filesnovanet.es/x.png"), 0)

    def test_un_host_que_solo_lo_contiene_no_cuenta(self):
        self.assertFalse(pnfg_pacer.is_pnfg(
            "https://resultados.rfef.es.malo.example.com/"))


class _Resp:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.content = body
        self.status_code = status


class ElBloqueoSeEspera(unittest.TestCase):
    """`_fetch_jornada_html` ante 200 con el cuerpo vacío."""

    def _fetch(self, respuestas, retries=4):
        sesion = mock.Mock()
        sesion.get.side_effect = respuestas
        with mock.patch.object(pnfg_pacer, "_sleep") as dormir,              mock.patch.object(rfef_calendario.time, "sleep"),              mock.patch.object(rfef_calendario, "make_session") as nueva:
            html = rfef_calendario._fetch_jornada_html(
                sesion, "C", "G", 12, "22", retries)
        return html, sesion, dormir, nueva

    def test_espera_en_silencio_y_con_la_misma_sesion(self):
        html, sesion, dormir, nueva = self._fetch([_Resp(b""), _Resp(b"<html>")])
        self.assertEqual(html, "<html>")
        dormir.assert_called_once_with(pnfg_pacer.BLOCK_WAITS[0])
        nueva.assert_not_called()  # la sesión no importa: el cupo es de la IP
        self.assertEqual(sesion.get.call_count, 2)

    def test_si_sigue_cerrado_escala_una_vez_y_se_rinde(self):
        """Una petición entre cada espera, no cinco reintentos seguidos: cada
        petición dentro del corte lo alarga."""
        html, sesion, dormir, _ = self._fetch([_Resp(b"")] * 5)
        self.assertIsNone(html)
        self.assertEqual(dormir.call_count, len(pnfg_pacer.BLOCK_WAITS))
        self.assertEqual(sesion.get.call_count, len(pnfg_pacer.BLOCK_WAITS) + 1)

    def test_tras_un_fallo_solo_una_espera(self):
        html, sesion, dormir, _ = self._fetch([_Resp(b"")] * 5, retries=1)
        self.assertIsNone(html)
        self.assertEqual(dormir.call_count, 1)

    def test_un_error_de_red_si_cambia_de_sesion(self):
        import requests
        sesion = mock.Mock()
        sesion.get.side_effect = requests.ConnectionError("cerrada")
        otra = mock.Mock()
        otra.get.return_value = _Resp(b"<html>")
        with mock.patch.object(rfef_calendario.time, "sleep"), \
             mock.patch.object(rfef_calendario, "make_session", return_value=otra):
            html = rfef_calendario._fetch_jornada_html(sesion, "C", "G", 3, "22", 4)
        self.assertEqual(html, "<html>")


class LosOtrosModulosTambienEsperan(unittest.TestCase):
    """Descubrimiento y clasificación gastan del mismo cupo. Si reintentan con
    sesión nueva a los 15 s, alargan el corte para el calendario que viene
    después."""

    def test_el_descubrimiento(self):
        from scrapers import rfef_discovery
        sesion = mock.Mock()
        sesion.get.side_effect = [_Resp(b""), _Resp(b"<html>")]
        with mock.patch.object(pnfg_pacer, "_sleep") as dormir,              mock.patch.object(rfef_discovery.time, "sleep"),              mock.patch.object(rfef_discovery, "make_session") as nueva:
            html = rfef_discovery.Fetcher(sesion).get("/x", {})
        self.assertEqual(html, "<html>")
        dormir.assert_called_once_with(pnfg_pacer.BLOCK_WAITS[0])
        nueva.assert_not_called()

    def test_la_clasificacion(self):
        sesion = mock.Mock()
        sesion.get.side_effect = [_Resp(b"")] * 5
        with mock.patch.object(pnfg_pacer, "_sleep") as dormir,              mock.patch.object(rfef_clasificacion.time, "sleep"),              mock.patch.object(rfef_clasificacion, "make_session") as nueva:
            equipos = rfef_clasificacion.fetch_division_teams(
                "C", "G", session=sesion, retries=1)
        self.assertEqual(equipos, [])
        self.assertEqual(dormir.call_count, 1)
        nueva.assert_not_called()


class LoQueFaltaPrimero(unittest.TestCase):
    """Con la caché llena hasta la J12, un run no puede volver a gastar el cupo
    en J1-J12 antes de llegar a la J13."""

    def _run(self, *, have, jornadas=30, max_jornadas=None, skip=None):
        pedidas = []

        def fake_fetch(session, comp, grupo, jornada, temporada, retries):
            pedidas.append(jornada)
            return f"<J{jornada}>"

        with mock.patch.object(rfef_calendario, "_fetch_jornada_html", fake_fetch), \
             mock.patch.object(rfef_calendario, "_parse_jornada_numbers",
                               return_value=list(range(1, jornadas + 1))), \
             mock.patch.object(rfef_calendario, "_parse_matches",
                               return_value=[{"home": "A", "away": "B", "date": None}]), \
             mock.patch.object(rfef_calendario.time, "sleep"):
            cal = rfef_calendario.fetch_division_calendar(
                "C", "G", temporada_code="22", session=object(),
                have=have, max_jornadas=max_jornadas, skip=skip,
            )
        return cal, pedidas

    def test_pide_primero_las_que_no_estan_en_cache(self):
        _, pedidas = self._run(have=set(range(1, 13)))
        # La J1 va siempre la primera: trae el <select> con el total.
        self.assertEqual(pedidas[:3], [1, 13, 14])
        self.assertEqual(pedidas[18], 30)
        self.assertEqual(sorted(pedidas), list(range(1, 31)))

    def test_el_calendario_sale_ordenado_igualmente(self):
        cal, _ = self._run(have={2, 3, 4})
        self.assertEqual([j["jornada"] for j in cal], list(range(1, 31)))

    def test_solo_equipos_sigue_pidiendo_j1_y_j2(self):
        """El equipo que descansa: hacen falta dos jornadas seguidas."""
        _, pedidas = self._run(have={1, 2}, max_jornadas=2)
        self.assertEqual(pedidas, [1, 2])


    def test_las_cerradas_no_se_piden_pero_la_j1_si(self):
        """La J1 trae el <select> con el total: se pide aunque esté cerrada."""
        _, pedidas = self._run(have=set(range(1, 6)), skip={1, 2, 3, 4})
        self.assertEqual(pedidas[0], 1)
        self.assertNotIn(2, pedidas)
        self.assertNotIn(4, pedidas)
        self.assertIn(5, pedidas)
        self.assertEqual(len(pedidas), 27)


class LasJornadasCerradas(unittest.TestCase):
    """`calendar_cache.settled_jornadas`: qué ya no hace falta volver a pedir."""

    HOY = date(2026, 10, 20)

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = pathlib.Path(tmp.name) / "calendar-cache.json"
        path.write_text(json.dumps({}), encoding="utf-8")
        p = mock.patch.object(calendar_cache, "CACHE_PATH", path)
        p.start()
        self.addCleanup(p.stop)
        calendar_cache._cache = None
        calendar_cache._calendars = None
        self.addCleanup(setattr, calendar_cache, "_cache", None)
        self.addCleanup(setattr, calendar_cache, "_calendars", None)

    def _jornada(self, num, fecha, *, acta=True):
        partidos = [{"home": "A", "away": "B", "date": fecha},
                    {"home": "C", "away": "D", "date": fecha}]
        calendar_cache.store_jornadas("C", "G", [{"jornada": num, "matches": partidos}])
        if acta:
            for m in partidos:
                calendar_cache.store("C", "G", num, m["home"], m["away"], f"acta-{num}")

    def _cerradas(self):
        return calendar_cache.settled_jornadas("C", "G", today=self.HOY)

    def test_jugada_hace_semanas_y_con_actas_esta_cerrada(self):
        self._jornada(1, "2026-09-05T18:00:00")
        self.assertEqual(self._cerradas(), {1})

    def test_sin_acta_se_sigue_pidiendo(self):
        """El acta llega después del partido, y es por lo que se vuelve a pedir."""
        self._jornada(1, "2026-09-05T18:00:00", acta=False)
        self.assertEqual(self._cerradas(), set())

    def test_reciente_se_sigue_pidiendo(self):
        """Hace menos de una semana todavía puede corregirse algo."""
        self._jornada(1, "2026-10-17T18:00:00")
        self.assertEqual(self._cerradas(), set())

    def test_futura_se_sigue_pidiendo(self):
        """Lo que no se ha jugado se puede aplazar: no se deja de mirar nunca."""
        self._jornada(1, "2026-12-05T18:00:00")
        self.assertEqual(self._cerradas(), set())

    def test_sin_fecha_se_sigue_pidiendo(self):
        self._jornada(1, None)
        self.assertEqual(self._cerradas(), set())

    def test_basta_un_partido_abierto_para_pedir_la_jornada(self):
        self._jornada(1, "2026-09-05T18:00:00")
        calendar_cache.store_jornadas("C", "G", [{"jornada": 1, "matches": [
            {"home": "A", "away": "B", "date": "2026-09-05T18:00:00"},
            {"home": "C", "away": "D", "date": "2026-11-05T18:00:00"},
        ]}])
        self.assertEqual(self._cerradas(), set())


if __name__ == "__main__":
    unittest.main()
