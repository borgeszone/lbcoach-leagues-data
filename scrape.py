#!/usr/bin/env python3
"""Genera output/leagues.json combinando scraper RFEF + datos manuales FCF.

Uso:
    python scrape.py [--season 2025-2026] [--no-badges]

El JSON resultante se publica en gh-pages via GitHub Actions y la app Flutter
lo descarga al crear/editar un equipo para autorrellenar la lista de rivales
con sus escudos.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from scrapers import calendar_cache, fcf, logo_resolver, rfef, rfef_shields

ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"


def current_season() -> str:
    """Devuelve la temporada vigente en formato `YYYY-YYYY` según la fecha.

    Temporada española de fútbol sala: arranca en julio/agosto y termina
    en junio. Por tanto:
      - meses 7-12 (jul-dic): `{year}-{year+1}` (temporada que acaba de empezar)
      - meses 1-6  (ene-jun): `{year-1}-{year}` (temporada que está acabando)
    """
    today = date.today()
    if today.month >= 7:
        return f"{today.year}-{today.year + 1}"
    return f"{today.year - 1}-{today.year}"


# El JSON que la app está usando ahora mismo. En modo `--teams-only` se lee
# para heredar de él los calendarios, que ese run no descarga.
#
# Tiene que ser la MISMA url que `LeaguesService.remoteJsonUrl` y que
# `website/admin/config.js`. Si se separan, el run rápido heredaría de un
# fichero que no usa nadie.
PUBLISHED_JSON_URL = (
    "https://raw.githubusercontent.com/borgeszone/lbcoach-leagues-data"
    "/gh-pages/leagues.json"
)


def fetch_published(season: str) -> dict | None:
    """Descarga el `leagues.json` publicado, o None si no sirve para heredar.

    Devuelve None —y el caller aborta— en dos casos, y los dos son a propósito:

    - **No se pudo descargar.** Publicar entonces dejaría el fichero sin
      calendarios: el run rápido no los trae. Sería borrar el autorrelleno por
      jornada de toda la app por un fallo de red de treinta segundos.
    - **Es de otra temporada.** Heredar de él metería el calendario del año
      pasado dentro de un JSON con la etiqueta de este — que es, letra por
      letra, el fallo que originó todo esto. Al empezar temporada, el primer
      run tiene que ser completo.
    """
    # `raw.githubusercontent.com` va por CDN y cachea: sin el parámetro
    # variable, justo después de publicar se sirve la versión anterior.
    url = f"{PUBLISHED_JSON_URL}?t={int(datetime.now(timezone.utc).timestamp())}"
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=60) as r:
            published = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[scrape] No se pudo descargar el JSON publicado: {e}")
        return None

    if published.get("version") != season:
        print(f"[scrape] El JSON publicado es de {published.get('version')!r} "
              f"y se pidió {season!r}: no se hereda nada de él.")
        return None
    return published


def _calendar_size(cal: list | None) -> tuple[int, int]:
    """`(jornadas, partidos)`, para comparar dos calendarios del mismo grupo."""
    if not cal:
        return (0, 0)
    return (len(cal), sum(len(j.get("matches", [])) for j in cal))


def inherit_calendars(categories: list[dict], published: dict) -> int:
    """Se queda con el calendario **más completo** entre este run y el publicado.

    Antes solo rellenaba los huecos (`if not div.get("calendar")`), y eso deja
    pasar una regresión silenciosa: el PNFG se corta a media descarga, el run
    trae 11 jornadas donde había 30, y las 11 ganan. Pasó — cuatro grupos de
    Segunda B se publicaron con 11, 12 y 18 jornadas de 30, y no se iban a
    arreglar solos porque el run siguiente heredaba de ese mismo fichero.

    Ahora la cobertura solo puede crecer, que es la regla que este repo ya
    aplica a los escudos (`badges-cache.json`) y a las actas
    (`calendar-cache.json`). No hace falta que un run salga perfecto: basta con
    que cada uno aporte lo que consiga.

    El guard de temporada vive en `fetch_published`: si el publicado es de otro
    año, aquí no llega nada. Se empareja por id y no por posición — una división
    que la federación aún no haya abierto no está en la lista de este run, y por
    índice los calendarios se correrían de sitio.
    """
    src: dict[str, list] = {}
    for cat in published.get("categories", []):
        for div in cat.get("divisions", []):
            if div.get("calendar"):
                src[f"{cat['id']}/{div['id']}"] = div["calendar"]
            for g in div.get("groups", []) or []:
                if g.get("calendar"):
                    src[f"{cat['id']}/{div['id']}/{g['id']}"] = g["calendar"]

    n = 0

    def _mejor(nodo: dict, clave: str, etiqueta: str) -> int:
        publicado = src.get(clave)
        if not publicado:
            return 0
        mio, suyo = _calendar_size(nodo.get("calendar")), _calendar_size(publicado)
        if suyo <= mio:
            return 0
        if mio != (0, 0):
            print(f"[scrape] {etiqueta}: este run trajo {mio[0]} jornadas y el "
                  f"publicado tiene {suyo[0]}; se conserva el publicado")
        nodo["calendar"] = publicado
        return 1

    for cat in categories:
        for div in cat.get("divisions", []):
            key = f"{cat['id']}/{div['id']}"
            n += _mejor(div, key, div["id"])
            for g in div.get("groups", []) or []:
                gkey = f"{key}/{g['id']}"
                n += _mejor(g, gkey, f"{div['id']}/{g['id']}")
    return n


def load_notices() -> dict:
    """Lee data/notices-manual.json (novedades/comunicados de federación).

    Estructura: {"categories": {<catId>: [notice, ...]},
                 "divisions":  {<divId>: [notice, ...]}}.
    Devuelve {} si el fichero no existe.
    """
    path = ROOT / "data" / "notices-manual.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_rules() -> dict:
    """Lee data/rules-manual.json (reglamento/bases de competición por temporada).

    Estructura: {"categories": {<catId>: rules}, "divisions": {<divId>: rules}},
    donde cada `rules` es {"season", "title", "url", "published"?}.
    Devuelve {} si el fichero no existe.
    """
    path = ROOT / "data" / "rules-manual.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def apply_rules(categories: list[dict], rules: dict, season: str) -> int:
    """Inyecta el reglamento en las categorías/divisiones que coincidan por id.

    **Sólo el de la temporada que se está publicando.** El fichero se mantiene a
    mano y acumula los de temporadas anteriores; sin este filtro, el JSON de
    2026-27 saldría con el reglamento de 2025-26 dentro y la app avisaría del
    documento equivocado con la etiqueta de temporada equivocada — que es el
    mismo error que §6.32 costó descubrir con los equipos.

    Modifica `categories` in-place y devuelve cuántos ha puesto.
    """
    cat_rules = rules.get("categories", {})
    div_rules = rules.get("divisions", {})

    def ok(r) -> bool:
        return (
            isinstance(r, dict)
            and r.get("season") == season
            and bool(r.get("url"))
        )

    n = 0
    for cat in categories:
        r = cat_rules.get(cat.get("id"))
        if ok(r):
            cat["rules"] = r
            n += 1
        for div in cat.get("divisions", []):
            dr = div_rules.get(div.get("id"))
            if ok(dr):
                div["rules"] = dr
                n += 1
    return n


def apply_notices(categories: list[dict], notices: dict) -> None:
    """Inyecta las novedades manuales en las categorías/divisiones que coincidan
    por id. La app (LeagueData.noticesFor) lee `notices` a nivel de categoría y
    de división. Modifica `categories` in-place.
    """
    cat_notices = notices.get("categories", {})
    div_notices = notices.get("divisions", {})
    for cat in categories:
        cid = cat.get("id")
        if cid in cat_notices:
            cat["notices"] = cat_notices[cid]
        for div in cat.get("divisions", []):
            did = div.get("id")
            if did in div_notices:
                div["notices"] = div_notices[did]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", default=None,
                        help="Temporada en formato YYYY-YYYY (default: auto-detectada según la fecha)")
    parser.add_argument("--no-badges", action="store_true",
                        help="No consulta el portal oficial de escudos "
                             "(futsal.rfef.es). La allowlist curada de "
                             "data/badges-overrides.json se sigue aplicando: "
                             "no depende de la red.")
    parser.add_argument("--teams-only", action="store_true",
                        help="Solo equipos (2 jornadas por grupo, ~1 min). "
                             "Hereda los calendarios del JSON ya publicado. "
                             "Para el run diario de pretemporada.")
    args = parser.parse_args()

    season = args.season or current_season()
    OUTPUT_DIR.mkdir(exist_ok=True)

    modo = "solo equipos" if args.teams_only else "completo"
    print(f"[scrape] Generando leagues.json para temporada {season} ({modo})")

    # El publicado se descarga ANTES de gastar peticiones, y en los dos modos.
    #
    # En el rápido es obligatorio: no trae calendarios, así que sin él no hay
    # nada que heredar y el run no debe llegar a publicar.
    #
    # En el completo es opcional pero importante igual: el PNFG se corta a media
    # descarga con frecuencia, y sin comparar contra lo publicado un run que
    # traiga 11 jornadas de 30 las publica y se lleva por delante las otras 19.
    # Que no se pueda descargar no aborta el run completo — ese sí trae
    # calendarios propios.
    published = fetch_published(season)
    if published is None and args.teams_only:
        print("[scrape] ABORTADO: sin un JSON publicado de esta temporada "
              "no hay calendarios que heredar, y publicar sin ellos dejaría "
              "a la app sin autorrelleno por jornada.")
        print("[scrape] Lanza primero un run completo (sin --teams-only).")
        return 1

    # Pre-poblar el mapa de escudos del portal oficial (futsal.rfef.es).
    #
    # Tiene techo: el portal solo lista ~12 clubes desde su home, y con nombres
    # de patrocinador de temporadas pasadas (§6.46 del informe). Lo que cubre el
    # resto es la allowlist curada, que no pasa por aquí ni por la red.
    if not args.no_badges:
        shields = rfef_shields.fetch_shield_map()
        print(f"[rfef-shields] {len(shields)} escudos oficiales descubiertos")
        logo_resolver.inject_rfef_shields(shields)

    rfef_cat = rfef.scrape(season=season, resolve_badges=not args.no_badges,
                           teams_only=args.teams_only)
    fcf_cat = fcf.load_manual()

    # ── Guard de temporada ───────────────────────────────────────────────
    #
    # `version` se estampa con la temporada que se ha *pedido*, y esa es la
    # cadena que la app y el panel enseñan. Publicarla sobre datos que nadie ha
    # verificado contra esa temporada es cómo el JSON del 10 de agosto de 2026
    # acabó diciendo "2026-2027" con los equipos y el calendario de 2025-26.
    #
    # Se aborta **sin escribir el fichero**: el paso de publicación del workflow
    # no llega a ejecutarse y gh-pages conserva el JSON anterior. Viejo pero
    # coherente es mejor que nuevo pero falso, y el fallo del run manda el email
    # de Actions, que es la única alerta que hay.
    #
    # Se sale con 0 o con 1 según **de quién** sea el problema, y esa distinción
    # existe para que el email de fallo de Actions siga significando algo. Cada
    # julio hay unas semanas en que la temporada ya cambió en el calendario pero
    # la federación aún no la ha abierto: si eso mandara un fallo diario, en dos
    # veranos nadie mira esos emails — y son también la alerta del keep-alive de
    # Supabase (§10 de CLAUDE.md).
    if not rfef_cat.get("seasonVerified"):
        pending = rfef_cat.get("seasonPending", False)
        print()
        if pending:
            print(f"[scrape] SIN PUBLICAR: la federación todavía no ha abierto "
                  f"{season}.")
        else:
            print(f"[scrape] ABORTADO: no se ha podido verificar RFEF contra "
                  f"{season}.")
        for w in rfef_cat.get("warnings", []):
            print(f"  - {w}")
        print("[scrape] No se escribe output/leagues.json; se conserva el publicado.")
        return 0 if pending else 1

    categories = [rfef_cat, fcf_cat]

    if published is not None:
        n = inherit_calendars(categories, published)
        print(f"[scrape] Calendarios heredados del publicado: {n}")

    # Inyectar novedades/comunicados de federación (data/notices-manual.json).
    apply_notices(categories, load_notices())

    # Y el reglamento de esta temporada, si lo hay (data/rules-manual.json).
    n_rules = apply_rules(categories, load_rules(), season)
    print(f"[scrape] Reglamentos de {season} publicados: {n_rules}")

    # `warnings` es de diagnóstico y no lo consume la app: sube al nivel raíz
    # para que el panel pueda enseñar "faltan las masculinas" sin tener que
    # deducirlo de un recuento de equipos a cero, que se parece demasiado a un
    # scraper roto.
    warnings = [f"{c['id']}: {w}" for c in categories for w in c.get("warnings", [])]
    for c in categories:
        c.pop("warnings", None)
        c.pop("seasonVerified", None)
        c.pop("seasonPending", None)
        c.pop("teamsOnly", None)

    payload = {
        "version": season,
        "lastUpdated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warnings": warnings,
        "categories": categories,
    }

    out = OUTPUT_DIR / "leagues.json"
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Persistir cachés que sobreviven entre runs: escudos (descubiertos por
    # el portal oficial) y actaUrls (descubiertos vía NFG_CmpJornada). Ambas son
    # acumulativas — una entrada cacheada solo se borra a mano si deja de
    # funcionar.
    logo_resolver.save_cache()
    calendar_cache.save_cache()

    def _count_teams(cat: dict) -> int:
        n = 0
        for d in cat.get("divisions", []):
            n += len(d.get("teams", []))
            for g in d.get("groups", []) or []:
                n += len(g.get("teams", []))
        return n

    print(
        f"[scrape] OK -> {out} "
        f"({len(rfef_cat['divisions'])} div RFEF / {_count_teams(rfef_cat)} equipos, "
        f"{len(fcf_cat['divisions'])} div FCF / {_count_teams(fcf_cat)} equipos)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
