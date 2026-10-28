"""Scraper del calendario RFEF (`resultados.rfef.es`, endpoint `NFG_CmpJornada`).

Complementa a `rfef_clasificacion` (que aporta equipos + escudos). Aquí
extraemos, por división/grupo, el **calendario**: para cada jornada, la lista
de enfrentamientos (local, visitante, fecha+hora). Lo consume la app para
autorrellenar el partido al seleccionar una jornada.

Plataforma: misma PNFG que la clasificación, **mismos códigos** de competición
y grupo que ya están en `rfef.py` (`comp`/`grupo`). Diferencias respecto a la
clasificación:
- Endpoint `NFG_CmpJornada` (no `NFG_VisClasificacion`).
- Parámetros **capitalizados**: `CodCompeticion`, `CodGrupo`, `CodJornada`,
  `CodTemporada` (mapea "YYYY-YYYY" a un código numérico vía el `<select>`).

Estructura HTML observada (validada en vivo, Primera FS Femenina 2025-2026):
- `<select name=jornada>` con opciones `"N - DD-MM-YYYY"` → nº total de jornadas.
- Cada partido es un `<tr>` con exactamente un `div.font_widgetL` (local),
  un `div.font_widgetV` (visitante) y un sello `DD-MM-YYYY HH:MM`.

Tolerante: ante rate-limit / cambio de estructura devuelve `[]`. El calendario
es best-effort: si falla, la división se publica sin él y la app cae al
formulario manual.

**Desde octubre de 2026 la fuente principal es otra**: `NFG_VisCalendario_Vis`
con `CDetalle=1` (`fetch_season_calendar`), que trae el grupo entero —las 30
jornadas, con fecha, hora y pabellón de cada partido— en **una** petición.
Una nota anterior la daba por descartada ("un shell que carga los datos por
AJAX"): no lo es. Sin la cookie de sesión la primera respuesta es un 302, y
probablemente eso fue lo que se vio. Comprobado el 2026-10-25 en los 12 grupos
de 2026-27 (CLAUDE.md §6.86). `NFG_CmpJornada` se queda para lo único que esa
vista no trae —el `CodActa`— y como respaldo si la vista falla.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

from scrapers.rfef_clasificacion import (
    BASE_URL,
    COD_PRIMARIA,
    make_session,
    _norm,
)
from scrapers import pnfg_pacer
from scrapers.rfef_discovery import resolve_season_code

CAL_PATH = "/pnfg/NPcd/NFG_CmpJornada"
# Endpoint del acta oficial (PDF) que la PNFG genera por partido. Cada partido
# de `NFG_CmpJornada` viene con un `CodActa` numérico único; con él se construye
# esta URL y la federación devuelve el acta firmada por el árbitro como PDF.
ACTA_URL_TMPL = (
    "https://resultados.rfef.es/pnfg/NPcd/NFG_CMP_Alineacion"
    "?cod_primaria=1000121&codacta={cod_acta}&NPcd_Pdf=1"
)

# "DD-MM-YYYY HH:MM" dentro de la fila del partido.
_DT_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})\s+(\d{2}):(\d{2})")
# Opción del <select name=jornada>: "1 - 06-09-2025".
_JORNADA_OPT_RE = re.compile(r"^\s*(\d+)\s*-\s*\d{2}-\d{2}-\d{4}")
# `<a href="...NFG_CmpPartido?...CodActa=NNN...">` o `cod_acta=NNN`.
_COD_ACTA_RE = re.compile(r"[Cc]od[_]?[Aa]cta=(\d+)")

_BACKOFFS = [15, 30, 60, 120]


class RateLimitBreaker:
    """Cortacircuitos para el rate-limit de la PNFG.

    **El problema que resuelve.** Un run completo pide ~360 páginas (12 grupos
    x ~30 jornadas). Cuando la PNFG bloquea —200 con el cuerpo vacío—, cada
    jornada agota sus cuatro backoffs: 15+30+60+120 = 225 s **por jornada**, y
    después el bucle sigue con la siguiente como si nada. Un run bloqueado de
    verdad se pasa horas reintentando, alimentando el bloqueo que causó el
    reintento, y muere en el corte de 6 h de Actions **sin llegar a publicar**.

    Así que aquí el fallo no se reintenta indefinidamente: se cuenta.

    - `per_group` jornadas seguidas sin respuesta → se abandona ese grupo. A
      225 s cada una, tres ya son once minutos de evidencia; la cuarta no
      informa de nada nuevo.
    - `run_budget` fallos en total → no se pide **ni una página más** en todo el
      run. El presupuesto es del run entero y no por grupo a propósito: doce
      grupos con dos fallos cada uno describen el mismo servidor enfadado que
      un grupo con veinticuatro, y el objetivo es dejar de pegarle.

    **Abandonar no es perder.** Lo que este cortacircuitos deja a medias lo
    rellena `calendar_cache` con lo que trajeron los runs anteriores, y por
    encima `inherit_calendars` conserva el calendario publicado si es más
    completo. Sin esas dos piezas, cortar antes sería sencillamente publicar
    menos: no lo actives sin ellas.

    **El contador de reintentos se hunde tras el primer fallo, y se recupera
    solo.** Ese primer fallo ya demostró que el servidor está negando; volver a
    pagar 225 s en la jornada siguiente es lo que convierte un run de 26 min en
    uno de seis horas. Una sola respuesta buena lo restaura, así que un corte
    transitorio no degrada el resto del run.

    **Desde octubre de 2026 esto es la segunda red, no la primera.** El corte
    tiene una causa medida —un cupo de ~12 peticiones por IP, vayan al ritmo que
    vayan— y lo que lo atraviesa es esperarlo en silencio
    (`pnfg_pacer.wait_out_block`). Así un corte normal se resuelve dentro de la
    misma jornada y no cuenta aquí. Si este cortacircuitos vuelve a saltar con
    eso puesto, lo que hay que mirar es si la federación ha alargado el corte.
    """

    def __init__(
        self,
        *,
        per_group: int = 3,
        run_budget: int = 12,
        time_budget_s: float | None = None,
        clock=time.monotonic,
    ) -> None:
        self.per_group = per_group
        self.run_budget = run_budget
        self.failures = 0
        self.groups_abandoned = 0
        self._consecutive = 0
        self._label = ""
        # **El tope de tiempo.** Con el ritmo que exige la PNFG cada petición
        # cuesta casi un minuto, y un run completo puede acercarse al corte de
        # 6 h de Actions — que mata el job **sin publicar nada**. Mejor parar a
        # tiempo y publicar lo que haya: lo que falte lo rellenan la caché y el
        # JSON anterior, y como se pide primero lo que falta, el run siguiente
        # sigue por donde se quedó.
        self._clock = clock
        self._deadline = None if time_budget_s is None else clock() + time_budget_s
        self.time_budget_s = time_budget_s

    @property
    def out_of_time(self) -> bool:
        return self._deadline is not None and self._clock() >= self._deadline

    @property
    def blocked(self) -> bool:
        """El run agotó su presupuesto —de fallos o de tiempo— o la PNFG ya
        cortó: no se pide nada más."""
        return (self.failures >= self.run_budget or self.out_of_time
                or pnfg_pacer.stopped())

    @property
    def group_tripped(self) -> bool:
        """Demasiadas jornadas seguidas sin respuesta en el grupo en curso."""
        return self._consecutive >= self.per_group

    def start_group(self, label: str) -> None:
        self._label = label
        self._consecutive = 0

    def note_success(self) -> None:
        self._consecutive = 0

    def note_failure(self) -> None:
        self.failures += 1
        self._consecutive += 1

    def note_group_abandoned(self) -> None:
        self.groups_abandoned += 1

    def retries_for(self, retries: int) -> int:
        """Reintentos que tocan ahora. Ver la nota de la clase: tras un fallo sin
        éxito de por medio, uno solo."""
        return 1 if self._consecutive else retries

    def summary(self) -> str | None:
        """Una línea para `warnings` del JSON publicado, o None si no hubo nada
        que contar.

        Va al JSON y no solo al log porque un run recortado se parece
        demasiado a un run normal: sin esto, la única diferencia visible entre
        "la federación no lo ha publicado" y "nos bloquearon" es un recuento de
        jornadas que nadie mira.
        """
        if pnfg_pacer.stopped():
            return ("la PNFG cortó a mitad de run y no se le pidió nada más; lo "
                    "que falta sale de la caché y del JSON anterior, y lo pide el "
                    "run siguiente")
        if self.out_of_time:
            return (f"se acabó el tiempo de la fase de calendario "
                    f"({int(self.time_budget_s // 60)} min) con "
                    f"{self.failures} jornada/s rate-limitada/s; lo que no se pidió "
                    f"sale de la caché y del JSON anterior, y el próximo run "
                    f"empieza por lo que falta")
        if not self.failures:
            return None
        parte = (f"la PNFG rate-limitó {self.failures} jornada/s"
                 f"{f' y se abandonaron {self.groups_abandoned} grupo/s' if self.groups_abandoned else ''}")
        if self.blocked:
            return (f"{parte}; agotado el presupuesto de {self.run_budget} fallos, "
                    f"el resto del calendario no se pidió. Lo publicado sale de la "
                    f"caché y del JSON anterior")
        return f"{parte}; esas jornadas salen de la caché o del JSON anterior"


def resolve_temporada_code(season: str, *, session: requests.Session | None = None) -> str | None:
    """Alias histórico de `rfef_discovery.resolve_season_code`.

    La implementación se mudó a `rfef_discovery` porque allí el `<select
    name=temporada>` no es un detalle del calendario sino el primer paso del
    descubrimiento de competiciones. Se mantiene el nombre para no tocar los
    callers.
    """
    return resolve_season_code(season, session=session)


def teams_from_calendar(calendar: list[dict]) -> list[str]:
    """Nombres de equipo deducidos de un calendario (unión de local y visitante).

    Es la **fuente primaria de equipos antes de que arranque la liga**, y ese
    "antes" es justo cuando el scraper hace falta. La tabla de clasificación no
    existe hasta que se juega la J1: en agosto está siempre vacía, el scraper se
    quedaba sin equipos y caía al fallback curado de la temporada anterior. El
    calendario, en cambio, existe en cuanto se sortea.

    Se recorren **todas** las jornadas y no solo la primera: con un número impar
    de equipos hay uno que descansa cada jornada, y mirando solo la J1 ese se
    perdería.

    Devuelve los nombres ordenados alfabéticamente. El orden importa poco para
    la app —el selector los reordena— pero uno estable evita que el JSON
    publicado cambie de un run a otro sin que haya cambiado nada.
    """
    seen: dict[str, str] = {}
    for jornada in calendar:
        for match in jornada.get("matches", []):
            for name in (match.get("home"), match.get("away")):
                name = (name or "").strip()
                if not name:
                    continue
                key = _norm(name)
                # Se conserva la primera grafía vista: las siguientes solo
                # varían en espacios o comillas.
                if key and key not in seen:
                    seen[key] = name
    return [seen[k] for k in sorted(seen)]


def fetch_division_calendar(
    cod_competicion: str | int,
    cod_grupo: str | int,
    *,
    temporada_code: str | None = None,
    session: requests.Session | None = None,
    retries: int = 4,
    jornada_delay: float = 4.0,
    max_jornadas: int | None = None,
    breaker: "RateLimitBreaker | None" = None,
    meta: dict | None = None,
    have: "set[int] | frozenset[int] | None" = None,
    skip: "set[int] | frozenset[int] | None" = None,
) -> list[dict]:
    """Devuelve `[{"jornada": N, "matches": [{"home","away","date"}]}]`.

    `have` son las jornadas que ya están en `calendar_cache`. Se piden **las
    que faltan primero** y después el resto. Hasta ahora se pedía siempre de la
    J1 en adelante, y como la PNFG corta tras ~12 peticiones, cada run volvía a
    bajar J1-J12 —que ya teníamos— y no llegaba nunca a la J13: la caché
    prometía cobertura creciente y se quedaba clavada. Pidiendo primero lo que
    falta, un run cortado sigue añadiendo jornadas nuevas. La lista que se
    devuelve va igualmente ordenada por número.

    `skip` son jornadas que no se piden en absoluto, porque ya no pueden cambiar
    (`calendar_cache.settled_jornadas`). No salen en lo que se devuelve: quien
    llama las rellena desde la caché. La J1 se pide siempre igual, porque es la
    que trae el `<select>` con cuántas jornadas hay.

    `max_jornadas` corta tras las N primeras. Existe para el modo "solo
    equipos" (`scrape.py --teams-only`): la plantilla de un grupo se deduce de
    dos jornadas, y bajarse las ~30 cuesta hora y media contra el rate-limit de
    la PNFG. Dos, y no una, porque con un número impar de equipos hay uno que
    descansa cada jornada y con la J1 sola se perdería.

    `breaker` corta cuando la PNFG está bloqueando (ver `RateLimitBreaker`). Se
    comparte entre grupos para que el presupuesto sea del run entero; sin él se
    usa uno propio, que da el corte por grupo pero no el global.

    `date` en ISO-8601 (`YYYY-MM-DDTHH:MM:00`) o None si la fila no trae fecha.
    Estrategia: descarga la jornada 1 para leer el `<select>` de jornadas
    (cuántas hay), parsea sus partidos, y luego itera el resto de jornadas con
    una pausa entre cada una. Ante fallo de una jornada concreta, la salta.
    """
    s = session or make_session()
    label = f"{cod_competicion}/{cod_grupo}"
    br = breaker or RateLimitBreaker()
    br.start_group(label)

    if br.blocked:
        print(f"  [rfef-cal] {label}: omitido, el run agotó su presupuesto de fallos")
        return []

    first_html = _fetch_jornada_html(
        s, cod_competicion, cod_grupo, 1, temporada_code, br.retries_for(retries))
    if first_html is None:
        br.note_failure()
        br.note_group_abandoned()
        print(f"  [rfef-cal] {label}: sin respuesta para J1; calendario vacío")
        return []
    br.note_success()

    jornada_nums = _parse_jornada_numbers(first_html)
    if not jornada_nums:
        # Página sin <select> de jornadas poblado: intentar parsear J1 sola.
        matches = _parse_matches(first_html)
        return [{"jornada": 1, "matches": matches}] if matches else []

    # **El total, antes de recortar.** Es lo único que dice cuántas jornadas
    # tiene la competición de verdad, y se sabe aquí aunque el resto del bucle
    # se lo coma el rate-limit: sale del `<select>` de la J1.
    #
    # Se anota antes del recorte de `max_jornadas` a propósito. En modo
    # solo-equipos se piden dos jornadas y se publican cero, pero el total sigue
    # siendo el de la competición — y publicarlo permite que el panel distinga
    # "faltan 7 de 30" de "hay 23 y no sabemos si son todas".
    if meta is not None:
        meta["total"] = len(jornada_nums)

    if max_jornadas is not None:
        # Modo solo-equipos: J1 y J2 seguidas, por el equipo que descansa. El
        # orden de "lo que falta primero" no aplica aquí.
        jornada_nums = jornada_nums[:max_jornadas]
    else:
        if skip:
            saltadas = [n for n in jornada_nums if n != 1 and n in skip]
            if saltadas:
                print(f"  [rfef-cal] {label}: {len(saltadas)} jornadas cerradas "
                      f"no se piden (salen de la caché)")
            jornada_nums = [n for n in jornada_nums if n == 1 or n not in skip]
        if have:
            jornada_nums = ([n for n in jornada_nums if n not in have]
                            + [n for n in jornada_nums if n in have])

    out: list[dict] = []
    for num in jornada_nums:
        if num == 1:
            html = first_html
        else:
            if br.blocked:
                print(f"  [rfef-cal] {label}: cortado en J{num}, el run agotó su "
                      f"presupuesto de fallos")
                br.note_group_abandoned()
                break
            time.sleep(jornada_delay)
            html = _fetch_jornada_html(
                s, cod_competicion, cod_grupo, num, temporada_code,
                br.retries_for(retries))
            if html is None:
                br.note_failure()
                if br.group_tripped:
                    print(f"  [rfef-cal] {label}: {br.per_group} jornadas seguidas "
                          f"sin respuesta; se abandona el grupo en J{num}")
                    br.note_group_abandoned()
                    break
                print(f"  [rfef-cal] {label}: J{num} sin respuesta, saltando")
                continue
            br.note_success()
        matches = _parse_matches(html)
        if matches:
            out.append({"jornada": num, "matches": matches})
        # Los escudos se acumulan de TODAS las jornadas y no solo de la primera:
        # con un número impar de equipos hay uno que descansa cada jornada, y
        # mirando una sola ese se quedaría sin escudo. Es el mismo motivo por el
        # que `teams_from_calendar` recorre el calendario entero.
        if meta is not None:
            meta.setdefault("badges", {}).update(parse_jornada_badges(html))

    out.sort(key=lambda j: j["jornada"])
    print(f"  [rfef-cal] {label}: {len(out)} jornadas con partidos")
    return out


def _fetch_jornada_html(
    session: requests.Session,
    cod_competicion: str | int,
    cod_grupo: str | int,
    jornada: int,
    temporada_code: str | None,
    retries: int,
) -> str | None:
    """GET de una jornada. Devuelve el HTML o None.

    **Los dos fallos se tratan distinto, porque son cosas distintas:**

    - **200 con el cuerpo vacío es el corte de la PNFG**: la IP agotó su cupo
      (medido, ver `pnfg_pacer`). No se reintenta: `pnfg_pacer.wait_out_block`
      para el run entero, porque cada petición dentro del corte lo alarga.
    - **Error de red** (RFEF cierra la conexión bajo carga): backoff corto y
      sesión nueva, como siempre — ahí sí puede estar rota la conexión.

    `retries` acota los reintentos por error de red."""
    params = {
        "cod_primaria": COD_PRIMARIA,
        "CodCompeticion": str(cod_competicion),
        "CodGrupo": str(cod_grupo),
        "CodJornada": str(jornada),
    }
    if temporada_code:
        params["CodTemporada"] = temporada_code

    url = BASE_URL + CAL_PATH
    s = session
    block_waits = 0
    net_attempt = 0
    while True:
        try:
            r = s.get(url, params=params, timeout=20)
        except requests.RequestException as e:
            if net_attempt < retries:
                backoff = _BACKOFFS[min(net_attempt, len(_BACKOFFS) - 1)]
                net_attempt += 1
                time.sleep(backoff)
                s = make_session()
                continue
            print(f"  [rfef-cal] J{jornada}: error de red agotados reintentos ({e})")
            return None

        if r.status_code != 200:
            print(f"  [rfef-cal] J{jornada}: HTTP {r.status_code}")
            return None
        if r.content:
            return r.content.decode("iso-8859-15", errors="replace")
        # 200 con body vacío → la IP agotó su cupo.
        if not pnfg_pacer.wait_out_block(block_waits, label=f"J{jornada}",
                                         limit=retries):
            return None
        block_waits += 1


def _parse_jornada_numbers(html: str) -> list[int]:
    """Lee el `<select name=jornada>` y devuelve los números de jornada
    ordenados (las opciones son `"N - DD-MM-YYYY"`)."""
    soup = BeautifulSoup(html, "html.parser")
    sel = soup.find("select", attrs={"name": "jornada"})
    if sel is None:
        return []
    nums: set[int] = set()
    for opt in sel.find_all("option"):
        m = _JORNADA_OPT_RE.match(opt.get_text(strip=True))
        if m:
            nums.add(int(m.group(1)))
    return sorted(nums)


def _match_rows(html: str):
    """Las filas de partido de una jornada, con sus dos nombres ya limpios.

    Criterio: un `<tr>` con **exactamente** un `div.font_widgetL` (local) y un
    `div.font_widgetV` (visitante). Vive aquí y no duplicado en cada parser
    porque lo consumen dos —los partidos y los escudos—, y dos copias de este
    criterio son dos cosas que un día discrepan sobre qué es un partido.

    Deduplica por (local, visitante) normalizados: un `<tr>` ancestro puede
    envolver al de cada partido y colar duplicados.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[tuple[str, str]] = set()
    for tr in soup.find_all("tr"):
        locals_ = tr.select("div.font_widgetL")
        visitors = tr.select("div.font_widgetV")
        if len(locals_) != 1 or len(visitors) != 1:
            continue
        home = _clean(locals_[0].get_text(strip=True))
        away = _clean(visitors[0].get_text(strip=True))
        if not home or not away:
            continue
        key = (_norm(home), _norm(away))
        if key in seen:
            continue
        seen.add(key)
        yield tr, home, away


def parse_jornada_badges(html: str) -> dict[str, str]:
    """`{nombre de equipo: URL de su escudo}` leído de las filas de una jornada.

    ## Por qué existe

    La cobertura de escudos estaba en el 33 % (65 de 192). La única fuente que
    trae el escudo **en la misma fila que el nombre** era la clasificación, y la
    clasificación no existe hasta que se juega la J1: medido el 2026-09-02, con
    la temporada arrancando dos días después, su página respondía 60 KB sin
    tabla.

    Pero el calendario —que este módulo ya descarga entero— trae los dos escudos
    en cada fila, servidos por el servidor de ficheros de la propia federación,
    que es uno de los dominios que la allowlist admite (`IP-004`). Así que esta
    fuente no cuesta ni una petición más, existe desde el sorteo, y la
    asociación nombre → escudo la confirma **la fila**: no es un parecido de
    nombres ni una búsqueda.

    ## El orden de las dos imágenes es el contrato

    Cada fila trae exactamente dos: local y visitante, en ese orden. Con más o
    con menos no se afirma nada y la fila se salta — preferir `None` a un escudo
    que puede ser del equipo de al lado es la regla de todo el resolver, y aquí
    el error sería especialmente difícil de ver: un escudo equivocado se parece
    mucho a un escudo.

    **No se filtra por dominio aquí.** Eso lo hace `logo_resolver` al inyectar,
    que es donde vive la allowlist y donde tiene que quedarse la decisión.

    Los nombres son los **oficiales** (los del acta), que es justo la clave con
    la que `_teams_from_names` resuelve antes de que `_with_short_names` los
    renombre.
    """
    out: dict[str, str] = {}
    for tr, home, away in _match_rows(html):
        imgs = [i.get("src") for i in tr.find_all("img") if i.get("src")]
        if len(imgs) != 2:
            continue
        for nombre, url in ((home, imgs[0]), (away, imgs[1])):
            if _is_placeholder(url):
                continue
            out.setdefault(nombre, url)
    return out


# El escudo genérico que la PNFG sirve cuando un club no tiene el suyo subido.
# Es **la misma URL para todos**, sin ningún id dentro:
#
#   .../pnfg/img/web_responsive_2/ESP/escudo_sm_resultados_.jpg
#
# Hay que descartarlo por lo que es y no por el host que lo sirve. Hoy viene de
# un dominio que la allowlist no admite, así que el filtro de `logo_resolver` lo
# para de rebote — pero eso es una casualidad, no una garantía: el día que lo
# sirvan desde `rfef.filesnovanet.es` entraría, y entonces medio JSON tendría
# "escudo" y sería el mismo dibujo gris para todos. Un placeholder publicado
# como escudo es peor que un hueco: el hueco la app ya lo pinta con su propio
# marcador, y además se ve en el recuento de cobertura.
_PLACEHOLDER_RE = re.compile(r"escudo_sm_resultados|/web_responsive_\d+/", re.I)


def _is_placeholder(url: str) -> bool:
    return bool(_PLACEHOLDER_RE.search(url or ""))


def _parse_matches(html: str) -> list[dict]:
    """Los partidos de una jornada, con fecha+hora y acta si la fila las trae."""
    out: list[dict] = []
    for tr, home, away in _match_rows(html):
        date_iso = None
        tr_text = tr.get_text(" ", strip=True)
        m = _DT_RE.search(tr_text)
        if m:
            dd, mm, yyyy, hh, mn = m.groups()
            date_iso = f"{yyyy}-{mm}-{dd}T{hh}:{mn}:00"

        # CodActa: aparece como `CodActa=NNN` o `codacta=NNN` en los <a href>
        # que apuntan al detalle del partido o al PDF de la alineación. Se
        # construye la URL del acta solo si encontramos el código.
        acta_url = None
        href_blob = " ".join(a.get("href", "") for a in tr.find_all("a", href=True))
        ma = _COD_ACTA_RE.search(href_blob)
        if ma:
            acta_url = ACTA_URL_TMPL.format(cod_acta=ma.group(1))

        out.append({
            "home": home,
            "away": away,
            "date": date_iso,
            "actaUrl": acta_url,
        })
    return out


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


# ── Calendario completo en una petición (desde octubre de 2026) ─────────────

SEASON_PATH = "/pnfg/NPcd/NFG_VisCalendario_Vis"
_JORNADA_TITLE_RE = re.compile(r"Jornada\s+(\d+)")
# "19-09-2026 - 17:45", o "07-11-2026" a secas si aún no hay hora.
_SEASON_DT_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})(?:\s*-\s*(\d{2}):(\d{2}))?")
_VENUE_PENDING = "pendiente de asignar"


def _season_match_info(text: str) -> tuple[str | None, str | None]:
    """`(fecha, pabellón)` del bloque de la derecha de un partido.

    **`00:00` no es medianoche, es "sin hora"**: la PNFG lo pone en los
    partidos que aún no tienen hora asignada. Esos se publican con el día a
    secas, que es como la app sabe decir "hora por confirmar" (§6.42) y como
    `_merge_calendar_cache` distingue un partido con hora de uno sin ella.
    """
    text = " ".join(text.replace("\xa0", " ").split())
    m = _SEASON_DT_RE.search(text)
    if not m:
        return None, None
    dd, mm, yyyy, hh, mn = m.groups()
    day = f"{yyyy}-{mm}-{dd}"
    when = f"{day}T{hh}:{mn}:00" if hh and (hh, mn) != ("00", "00") else day
    venue = text[:m.start()].strip(" -") or None
    if venue and venue.lower().startswith(_VENUE_PENDING):
        venue = None
    return when, venue


def parse_season_calendar(html: str) -> list[dict]:
    """Las jornadas de la vista `NFG_VisCalendario_Vis?CDetalle=1`.

    Cada jornada es una tarjeta (`div.card-body`) con su título
    `Jornada N (DD-MM-YYYY)` y una fila por partido: una tabla con local,
    marcador y visitante, y a su derecha el pabellón y la fecha con hora.

    **El marcador no se lee, a propósito**: viene ofuscado con CSS, un `eval` de
    JavaScript y un número señuelo oculto que no es el real. Nada de lo que se
    publica lo necesita, y leerlo mal sería peor que no tenerlo.

    `date` lleva hora (`…T19:30:00`) si la federación la ha asignado, y el día a
    secas si no; sin día en la fila, el de la jornada. `venue` solo si está
    asignado.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for card in soup.find_all("div", class_="card-body"):
        title = card.find("h5", class_="card-title")
        m = _JORNADA_TITLE_RE.search(title.get_text(" ")) if title else None
        if not m:
            continue
        entry: dict = {"jornada": int(m.group(1)), "matches": []}
        jd = _SEASON_DT_RE.search(title.get_text(" "))
        if jd:
            entry["date"] = f"{jd.group(3)}-{jd.group(2)}-{jd.group(1)}"
        seen: set[tuple[str, str]] = set()
        for row in card.find_all("div", class_="row", recursive=False):
            tds = row.find_all("td")
            if len(tds) < 3:
                continue
            home = _clean(tds[0].get_text(" ", strip=True))
            away = _clean(tds[-1].get_text(" ", strip=True))
            if not home or not away:
                continue
            key = (_norm(home), _norm(away))
            if key in seen:
                continue
            seen.add(key)
            info = row.find("div", class_="right")
            when, venue = _season_match_info(info.get_text(" ") if info else "")
            match = {"home": home, "away": away, "date": when or entry.get("date")}
            if venue:
                match["venue"] = venue
            entry["matches"].append(match)
        if entry["matches"]:
            out.append(entry)
    out.sort(key=lambda j: j["jornada"])
    return out


def fetch_season_calendar(
    cod_competicion: str | int,
    cod_grupo: str | int,
    *,
    temporada_code: str | None,
    session: requests.Session | None = None,
    breaker: "RateLimitBreaker | None" = None,
) -> list[dict] | None:
    """El calendario entero de un grupo en una petición. None si no se pudo.

    None cubre tres cosas, y las tres hacen lo mismo río arriba —caer al camino
    de jornada en jornada, que a su vez se para si la PNFG ha cortado—: el
    corte, un error de red, y una página que ya no tiene la forma esperada.
    No se reintenta aquí: el respaldo existe para eso.
    """
    label = f"{cod_competicion}/{cod_grupo}"
    if breaker is not None and breaker.blocked:
        return None
    s = session or make_session()
    params = {
        "cod_primaria": COD_PRIMARIA,
        "codcompeticion": str(cod_competicion),
        "codgrupo": str(cod_grupo),
        "CDetalle": "1",
    }
    if temporada_code:
        params["codtemporada"] = temporada_code
    try:
        r = s.get(BASE_URL + SEASON_PATH, params=params, timeout=30)
    except requests.RequestException as e:
        print(f"  [rfef-cal] {label}: calendario completo, error de red ({e})")
        return None
    if r.status_code != 200:
        print(f"  [rfef-cal] {label}: calendario completo, HTTP {r.status_code}")
        return None
    if not r.content:
        pnfg_pacer.wait_out_block(label=label)
        if breaker is not None:
            breaker.note_failure()
        return None
    calendar = parse_season_calendar(r.content.decode("iso-8859-15", errors="replace"))
    if not calendar:
        print(f"  [rfef-cal] {label}: la vista de calendario completo no trae "
              f"jornadas ({len(r.content)} bytes); ¿ha cambiado la página?")
        return None
    print(f"  [rfef-cal] {label}: calendario completo -> {len(calendar)} jornadas")
    return calendar


def fetch_jornada_matches(
    cod_competicion: str | int,
    cod_grupo: str | int,
    jornadas: list[int],
    *,
    temporada_code: str | None,
    session: requests.Session | None = None,
    breaker: "RateLimitBreaker | None" = None,
    meta: dict | None = None,
) -> dict[int, list[dict]]:
    """Los partidos de `NFG_CmpJornada` de esas jornadas, para sacar sus actas.

    Es la única página que trae el `CodActa`. Se piden **solo** las jornadas que
    lo necesitan (las elige quien llama) y en ese orden. Los escudos de sus
    filas se acumulan en `meta["badges"]`, como en `fetch_division_calendar`.
    """
    s = session or make_session()
    br = breaker or RateLimitBreaker()
    br.start_group(f"{cod_competicion}/{cod_grupo} actas")
    out: dict[int, list[dict]] = {}
    for num in jornadas:
        if br.blocked:
            break
        html = _fetch_jornada_html(s, cod_competicion, cod_grupo, num,
                                   temporada_code, br.retries_for(2))
        if html is None:
            br.note_failure()
            if br.group_tripped:
                break
            continue
        br.note_success()
        out[num] = _parse_matches(html)
        if meta is not None:
            meta.setdefault("badges", {}).update(parse_jornada_badges(html))
    return out


def attach_actas(matches: list[dict], pnfg_matches: list[dict]) -> int:
    """Copia el `actaUrl` de las filas de `NFG_CmpJornada` a los partidos.

    Se casa por local y visitante y, si no, por **un** solo lado: las dos
    páginas de la PNFG no siempre escriben igual al mismo equipo ("At. Arnoya"
    en una y "At. Arnoia" en la otra, 2026-10-25). Con un lado basta porque en
    una jornada cada equipo juega un partido; si un lado casara con dos, no se
    afirma nada.
    """
    by_pair = {(_norm(m["home"]), _norm(m["away"])): m for m in pnfg_matches}
    n = 0
    for m in matches:
        if m.get("actaUrl"):
            continue
        h, a = _norm(m.get("home") or ""), _norm(m.get("away") or "")
        src = by_pair.get((h, a))
        if src is None:
            cands = [p for (ph, pa), p in by_pair.items() if ph == h or pa == a]
            src = cands[0] if len(cands) == 1 else None
        if src and src.get("actaUrl"):
            m["actaUrl"] = src["actaUrl"]
            n += 1
    return n


def acta_jornadas(calendar: list[dict], has_acta, *, today: date,
                  lookback_days: int = 45) -> list[int]:
    """Qué jornadas hay que pedir a `NFG_CmpJornada` para tener sus actas.

    Las que tienen algún partido **ya jugado** —anterior a hoy— y **reciente**
    —de los últimos `lookback_days`— que todavía no tenga acta
    (`has_acta(jornada, partido)`). La más reciente primero, porque es la que
    acaba de publicarse.

    El tope de días no es por prudencia: un partido aplazado se queda con su
    fecha vieja en el pasado y sin acta hasta que se juegue, y sin el tope se
    pediría en cada run para siempre.
    """
    desde = today - timedelta(days=lookback_days)
    out: list[tuple[date, int]] = []
    for j in calendar:
        num = j.get("jornada")
        if not isinstance(num, int):
            continue
        dias = []
        for m in j.get("matches", []):
            raw = (m.get("date") or "")[:10]
            try:
                d = date.fromisoformat(raw)
            except ValueError:
                continue
            if desde <= d < today and not m.get("actaUrl") and not has_acta(num, m):
                dias.append(d)
        if dias:
            out.append((max(dias), num))
    return [num for _, num in sorted(out, reverse=True)]
