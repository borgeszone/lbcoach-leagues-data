"""Experimento: ¿a qué ritmo se recarga el cupo de la PNFG sin llegar a cortarlo?

No es parte del scraper ni lo usa nadie. Existe para decidir el ritmo del
scraper con datos (ver `scrapers/pnfg_pacer.py` y CLAUDE.md §6.55).

Lo que ya se sabe (2026-10-04/05, desde una IP de casa):
- tras ~11 peticiones la PNFG devuelve 200 con el cuerpo vacío, vaya el
  scraper a 4,5 s o a 50 s entre peticiones;
- el corte es por IP, y escala con cada exceso.

Lo que falta saber, y es lo que mide esto: si **parando antes** del corte el
cupo se recarga, y cuánto tarda. Cada estrategia corre en su propio job de
Actions —su propia IP, limpia— y se para en el primer corte. Después pregunta
cada 5 min (una sola petición) para medir cuánto dura ese **primer** corte en
una IP sin castigo previo, y termina.

    python tools/pnfg_quota_probe.py ritmo:60:40
    python tools/pnfg_quota_probe.py rafaga:10:600:4
    python tools/pnfg_quota_probe.py ciclos:6:330

`ciclos:N:espera` es el que valida el diseño del scraper: gasta el cupo hasta el
corte, espera `espera` segundos **en silencio**, pregunta UNA vez y, si sigue
cerrado, vuelve a esperar lo mismo. Repite N cortes y apunta cuánto dura cada
uno. Si no escalan, "gastar el cupo y esperar" es viable; si escalan, no.
"""
from __future__ import annotations

import datetime
import sys
import time

import requests

BASE = "https://resultados.rfef.es"
PATH = "/pnfg/NPcd/NFG_CmpJornada"
PARAMS = {"cod_primaria": "1000120", "CodCompeticion": "33836181",
          "CodGrupo": "33836184", "CodTemporada": "22"}  # Segunda Fem G3, 2026-27
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept-Language": "es-ES,es;q=0.9",
}
T0 = time.time()
n_ok = 0
n_total = 0


def log(msg: str) -> None:
    hora = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    print(f"{hora} +{time.time() - T0:6.0f}s {msg}", flush=True)


def pedir(s: requests.Session, jornada: int) -> bool:
    global n_ok, n_total
    n_total += 1
    try:
        r = s.get(BASE + PATH, params={**PARAMS, "CodJornada": str(jornada)}, timeout=30)
        n = len(r.content)
    except requests.RequestException as e:
        log(f"#{n_total} J{jornada} ERROR {e}")
        return True  # un error de red no es el corte
    ok = n > 0
    n_ok += ok
    log(f"#{n_total} J{jornada} {'ok' if ok else 'VACÍA'} {n} bytes")
    return ok


def medir_corte(s: requests.Session) -> None:
    log(f"CORTE tras {n_ok} peticiones buenas ({n_total} en total, contando "
        f"la siembra de sesión)")
    inicio = time.time()
    for _ in range(12):  # hasta 60 min
        time.sleep(300)
        if pedir(s, 1):
            log(f"CORTE LEVANTADO a los {time.time() - inicio:.0f}s")
            return
    log("el corte sigue tras 60 min")


def main() -> None:
    plan = sys.argv[1]
    try:
        ip = requests.get("https://api.ipify.org", timeout=10).text
    except requests.RequestException:
        ip = "?"
    log(f"plan={plan} ip={ip}")

    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(BASE + "/", timeout=20)  # siembra la JSESSIONID: también gasta cupo
    n_total_siembra = 1
    global n_total
    n_total += n_total_siembra

    tipo, *nums = plan.split(":")
    jornada = 0

    def siguiente() -> int:
        nonlocal jornada
        jornada = jornada % 30 + 1
        return jornada

    if tipo == "ritmo":
        intervalo, cuantas = float(nums[0]), int(nums[1])
        for i in range(cuantas):
            if i:
                time.sleep(intervalo)
            if not pedir(s, siguiente()):
                return medir_corte(s)
    elif tipo == "rafaga":
        tam, pausa, rondas = int(nums[0]), float(nums[1]), int(nums[2])
        for ronda in range(rondas):
            if ronda:
                log(f"pausa de {pausa:.0f}s")
                time.sleep(pausa)
            for i in range(tam):
                if i:
                    time.sleep(5)
                if not pedir(s, siguiente()):
                    return medir_corte(s)
    elif tipo == "ciclos":
        ciclos, espera = int(nums[0]), float(nums[1])
        duraciones = []
        for c in range(1, ciclos + 1):
            buenas = 0
            while pedir(s, siguiente()):
                buenas += 1
                time.sleep(5)
            inicio = time.time()
            log(f"CICLO {c}: corte tras {buenas} buenas")
            esperas = 0
            while True:
                time.sleep(espera)
                esperas += 1
                if pedir(s, siguiente()):
                    break
                if esperas >= 8:
                    log(f"CICLO {c}: sigue cerrado tras {esperas} esperas; se para")
                    log(f"RESUMEN cortes(s)={duraciones} + >{time.time() - inicio:.0f}")
                    return
            duraciones.append(round(time.time() - inicio))
            log(f"CICLO {c}: corte levantado a los {duraciones[-1]}s "
                f"({esperas} espera/s)")
        log(f"RESUMEN cortes(s)={duraciones}")
        return
    else:
        raise SystemExit(f"plan desconocido: {plan}")
    log(f"SIN CORTE: {n_ok} peticiones buenas ({n_total} en total)")


if __name__ == "__main__":
    main()
