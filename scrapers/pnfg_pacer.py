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
Pasado el corte, otras 11-12.

Es exactamente el muro que se veía en Actions —cada grupo del calendario se
abandonaba entre la J12 y la J15, siempre a la misma altura y no en el mismo
minuto del run—, y explica por qué nada de lo que había lo arreglaba:

- **Los reintentos lo empeoraban.** 15/30/60/120 s con `JSESSIONID` nueva:
  cinco peticiones más durante el corte, más el GET de la home de cada sesión
  nueva. Es justo lo que alarga el castigo, y la sesión no importa.
- **Ir más despacio no sirve**: a 50 s entre peticiones el cupo dura lo mismo.
  Se probó y se descartó; no lo reintroduzcas sin volver a medir.

## Lo que hace este módulo

1. Un **intervalo corto** entre todas las peticiones al host (`PACER`), por
   cortesía y para no encadenar ráfagas de varios módulos. No es lo que evita
   el corte: el corte llega igual.
2. **`wait_out_block`**: cuando el corte llega, se espera **en silencio** —ni
   una petición, ni una sesión nueva— y en escalones: la ventana normal y, si
   sigue cerrado, una más larga por si el castigo ya venía alargado.

Con eso un run avanza a unas 11 peticiones cada ~7 minutos (~95 por hora).

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

# Esperas ante un corte, en orden. La primera es la ventana medida en limpio
# (~5-6 min). Las otras dos cubren un corte que ya venía alargado: el castigo
# ESCALA con la reincidencia — la noche de la medición, el quinto corte seguía
# cerrado a los 15 minutos preguntando sólo cada 2. Entre una espera y la
# siguiente se hace UNA petición, no más.
BLOCK_WAITS: tuple[float, ...] = (390.0, 600.0, 900.0)


class Pacer:
    """Garantiza al menos `interval` segundos entre dos peticiones.

    El reloj y la espera son inyectables para probarlo sin dormir.
    """

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None and self.interval > 0:
            remaining = self._last + self.interval - self._clock()
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()

    def note_block(self) -> None:
        """Tras esperar un corte, la siguiente petición puede salir ya."""
        self._last = None


def _interval_from_env() -> float:
    raw = os.environ.get("PNFG_INTERVAL")
    if not raw:
        return DEFAULT_INTERVAL
    try:
        return max(0.0, float(raw))
    except ValueError:
        print(f"[pnfg] PNFG_INTERVAL={raw!r} no es un número; se usa "
              f"{DEFAULT_INTERVAL}")
        return DEFAULT_INTERVAL


# Uno para todo el proceso: el cupo es de la IP, no de la sesión.
PACER = Pacer(_interval_from_env())


def is_pnfg(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() == PNFG_HOST


def _sleep(seconds: float) -> None:
    """Indirección para los tests: `time` es el mismo módulo en todos los
    ficheros, y parchear su `sleep` aquí lo parchearía en todos."""
    time.sleep(seconds)


def wait_out_block(waits_done: int, *, label: str = "", limit: int | None = None) -> bool:
    """Espera en silencio a que la PNFG levante el corte.

    `waits_done` es cuántas esperas lleva ya quien llama para **esta** petición;
    `limit` acota cuántas admite (sus `retries`). Devuelve False si no quedan, y
    entonces quien llama da la petición por perdida y la cuenta su
    cortacircuitos.

    **Quien llama no debe crear una sesión nueva** después: la sesión no importa
    y su GET de siembra es una petición más dentro del corte.
    """
    tope = len(BLOCK_WAITS) if limit is None else min(limit, len(BLOCK_WAITS))
    if waits_done >= tope:
        return False
    w = BLOCK_WAITS[waits_done]
    print(f"  [pnfg] {label + ': ' if label else ''}la PNFG ha cortado (cupo de "
          f"la IP agotado); se espera en silencio {int(w)} s")
    _sleep(w)
    PACER.note_block()
    return True
