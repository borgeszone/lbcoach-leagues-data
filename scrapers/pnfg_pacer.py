"""Cómo se le pide a la PNFG (`resultados.rfef.es`) sin que nos cierre la puerta.

## El límite, medido y no supuesto (2026-10-04)

Medido desde una IP de casa con el código del scraper, contra
`NFG_CmpJornada`. Cuatro ritmos distintos y el mismo resultado:

    cada  4,5 s   11 jornadas bien, la 12ª vuelve 200 con el cuerpo VACÍO
    cada 26   s   11 bien, corta en la 13ª petición
    cada 33   s   11 bien, corta en la 13ª petición
    cada 50   s   11 bien, la 12ª vacía — tras 9 minutos de peticiones

    sesión nueva justo tras el corte   vacía también → es de la IP, no de la sesión
    primer corte, preguntando cada 60 s  se levanta a los ~5-6 minutos
    pidiendo cada 30 s durante él        no se levanta en 17 minutos: se alarga
    al levantarse                        vuelve el cupo entero (otras 11)
    quinto corte de la noche             sigue cerrado a los 15 min: escala

O sea: **no es un límite de ritmo, es un cupo**. Unas 11-12 peticiones,
vayan a la velocidad que vayan, y después un corte de varios minutos que
**se alarga si se sigue pidiendo durante él y escala con cada reincidencia**.

## Y lo que sí funciona: no llegar nunca al corte (octubre de 2026)

Esperar el corte en silencio no bastaba: la prueba de punta a punta del
2026-10-05 se quedó en 11 de 30 jornadas, porque el castigo escala con cada
corte y un run viejo provocaba unos treinta. Lo medido después:

    25 oct, casa      ~20 peticiones en 25 min (una pausa de 15)       sin corte
    28 oct, casa      4 · 15 min · 8 · 15 min · 3                      corte en la 15ª
    28 oct, Actions   ~20 en 50 min, dos pausas de 15     corte en una página de jornada
    28 oct, Actions   ~20 en 50 min, dos pausas de 15     corte en una vista completa

O sea: **cada IP aguanta unas 15-20 peticiones, sean de la página que sean, y
las pausas de 15 minutos apenas las recargan**. Un día entero son unas 35, así
que no caben en un run, pero cada run de Actions sale por una IP nueva. De ahí:

1. **Pocas peticiones**: el calendario de un grupo es una sola
   (`rfef_calendario.fetch_season_calendar`), las actas tienen tope (2 por grupo
   y 8 por run, `rfef._acta_budget`), y las divisiones se descubren una vez por
   semana (`rfef.load_discovery`).
2. **Varios runs al día que se reparten el trabajo**: cada uno se salta los
   grupos que otro bajó en las últimas 20 h (`calendar_cache.fetched_within`).
   El cron es cada 4 horas.
3. Tandas de `PNFG_BURST` (6) y `PNFG_REST` (900 s) de silencio entre ellas: no
   es lo que evita el corte, pero no cuesta nada y aleja las peticiones entre
   sí. Se cuentan también las redirecciones que se quedan en la PNFG.
4. **Al primer corte, el run deja de pedir.** Si llega uno es que el cupo ha
   cambiado, y cualquier petición dentro de él lo alarga y lo hace escalar.
   Desde ese momento `PacedSession` contesta "vacío" sin salir a la red, así
   que los tres módulos que piden —descubrimiento, clasificación y calendario—
   lo tratan como el corte que es y se rinden por sus caminos de siempre. Lo
   que falte lo ponen la caché y el JSON anterior, y lo pide el run siguiente.
5. Un **intervalo corto** entre peticiones (`PNFG_INTERVAL`), por cortesía.

Vive en la sesión (`make_session()` devuelve una `PacedSession`) porque el cupo
es de la IP: descubrimiento, clasificación y calendario gastan del mismo, y un
corte provocado por uno lo sufren los demás.
"""
from __future__ import annotations

import os
import time
from typing import Callable
from urllib.parse import urlparse

PNFG_HOST = "resultados.rfef.es"

# Cortesía, no límite: ver la tabla de arriba.
DEFAULT_INTERVAL = 6.0
# Ver la cabecera: 16 seguidas de la vista completa pasaron sin corte; con
# `NFG_CmpJornada` el cupo es de ~11 y no se recarga en 15 min.
DEFAULT_BURST = 6
DEFAULT_REST = 900.0


class Pacer:
    """Ritmo de las peticiones: intervalo entre dos, y tandas con descanso.

    `burst=None` desactiva las tandas. El reloj y la espera son inyectables
    para probarlo sin dormir.
    """

    def __init__(
        self,
        interval: float,
        *,
        burst: int | None = None,
        rest: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = interval
        self.burst = burst
        self.rest = rest
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._in_burst = 0

    def wait(self) -> None:
        if self.burst and self._in_burst >= self.burst:
            print(f"[pnfg] {self._in_burst} peticiones en esta tanda: "
                  f"{int(self.rest)} s sin pedir nada para no agotar el cupo")
            self._sleep(self.rest)
            self._in_burst = 0
            self._last = None
        if self._last is not None and self.interval > 0:
            remaining = self._last + self.interval - self._clock()
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()
        self._in_burst += 1

    def count(self, n: int) -> None:
        """Peticiones que salieron sin pasar por `wait`: las redirecciones."""
        self._in_burst += n


def _env_number(name: str, default: float, cast=float):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(cast(0), cast(raw))
    except ValueError:
        print(f"[pnfg] {name}={raw!r} no es un número; se usa {default}")
        return default


# Uno para todo el proceso: el cupo es de la IP, no de la sesión.
PACER = Pacer(
    _env_number("PNFG_INTERVAL", DEFAULT_INTERVAL),
    burst=_env_number("PNFG_BURST", DEFAULT_BURST, int) or None,
    rest=_env_number("PNFG_REST", DEFAULT_REST),
)

_stopped = False


def stopped() -> bool:
    """¿Ha cortado la PNFG en este run? Si sí, no se le pide nada más."""
    return _stopped


def reset() -> None:
    """Para los tests: un proceso nuevo empieza sin corte."""
    global _stopped
    _stopped = False


def is_pnfg(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() == PNFG_HOST


def wait_out_block(waits_done: int = 0, *, label: str = "",
                   limit: int | None = None) -> bool:
    """La PNFG ha cortado: el run deja de pedirle nada. Devuelve siempre False.

    Se llamaba así porque antes esperaba el corte en silencio y reintentaba; se
    conserva el nombre y la firma para que los tres módulos que piden sigan
    igual —todos tratan el False como "petición perdida"—. Esperar no
    atravesaba un corte escalado (ver la cabecera), y cada petición dentro de
    él lo alargaba.
    """
    global _stopped
    if not _stopped:
        print(f"  [pnfg] {label + ': ' if label else ''}la PNFG ha cortado (cupo "
              f"de la IP agotado). Este run no le pide nada más: lo que falte "
              f"sale de la caché y del JSON anterior, y lo pide el run siguiente")
    _stopped = True
    return False


def empty_response(url: str):
    """La respuesta que da `PacedSession` tras un corte, sin salir a la red:
    200 con el cuerpo vacío, que es justo como corta la PNFG."""
    import requests

    r = requests.Response()
    r.status_code = 200
    r._content = b""
    r.url = url
    return r
