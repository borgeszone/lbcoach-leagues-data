# lbcoach-leagues-data

Genera y publica `leagues.json` — la fuente de datos que la app **Pizark** (paquete `lbcoach`; el repo conserva el nombre de antes) descarga para autorrellenar la lista de rivales con sus escudos, el calendario de cada jornada con su hora, pabellón y acta, y el reglamento de la temporada.

## Cómo funciona

```
┌─────────────────────────────────────┐
│ scrape.py                           │
│   ├─ scrapers/rfef_discovery.py     │  Qué competiciones tiene la temporada
│   ├─ scrapers/rfef_web.py           │  Equipos desde rfef.es (pretemporada)
│   ├─ scrapers/rfef.py               │  Equipos + calendario (PNFG, PDF)
│   ├─ scrapers/rfef_calendario.py    │  Calendario y actas de la PNFG
│   ├─ scrapers/pnfg_pacer.py         │  El cupo de la PNFG: ritmo y parada
│   ├─ scrapers/team_renames.py       │  Un equipo con dos nombres en un grupo
│   ├─ scrapers/former_names.py       │  Nombres de antes de cada club → aliases
│   ├─ scrapers/fcf.py                │  Lee data/fcf-manual.json
│   └─ scrapers/logo_resolver.py      │  Allowlist curada → portal → caché
└─────────────────────────────────────┘
              │
              ▼
       output/leagues.json
              │
              ▼
       Branch `gh-pages`
              │
              ▼
  https://raw.githubusercontent.com/borgeszone/lbcoach-leagues-data/gh-pages/leagues.json
              │
              ▼
       App Flutter (lbcoach) y panel de administración
```

## Uso local

```bash
pip install -r requirements.txt
python scrape.py
# Salida: output/leagues.json

python -m unittest discover -s tests -v   # 314 casos (2026-11-14)
```

Opciones:
- `--season 2026-2027` — cambiar la temporada
- `--no-badges` — no consultar el portal oficial de escudos; la allowlist curada
  (`data/badges-overrides.json`) se aplica igual, porque no depende de la red
- `--teams-only` — solo equipos (2 jornadas por grupo); hereda los calendarios
  del JSON publicado y no publica si no puede leerlo

Los tests parsean **HTML real** capturado de la PNFG (`tests/fixtures/`), no
HTML inventado. Se ejecutan también en el workflow, antes del scrape.

> **Problema conocido: los tests no están del todo aislados de la red.** A la
> PNFG no le piden nada (la suite pasa con la red a la PNFG bloqueada, §6.86 del
> informe técnico), pero `test_rfef_teams`,
> `test_season_calendar` y `test_teams_resilience` llegan al camino del PDF
> legacy (`rfef._pdf_url`) y descargan de verdad
> `https://rfef.es/sites/default/files/2026-07/Calendario_{1Div,2Div,1DivFem}_Sala_2026-2027.pdf`.
> Esas URL no existen, así que la salida se llena de `HTTP 404 en https://rfef.es/…`
> y los 314 casos pasan igual (comprobado el 2026-11-14). Sin red, `_download_pdf`
> devuelve `None` y tampoco falla nada; pero cada ejecución del workflow hace
> esas peticiones a rfef.es antes de empezar el scrape, y el día que alguna de
> esas URL sirva un PDF de verdad, los tests leerán datos reales. Sin arreglar.

## Mantenimiento por temporada

### RFEF (automático, sin tocar nada)

En cada run se le pregunta a la PNFG qué competiciones tiene la temporada
pedida y con qué códigos (`scrapers/rfef_discovery.py`). **En el repo no hay
ningún código de competición escrito a mano**, y eso no es cosmético: en la
PNFG los códigos son por temporada, así que tenerlos fijos hacía que el scraper
se trajera la liga del año pasado en cuanto cambiaba la temporada — y la
publicara con el sello de la nueva, sin un solo error. Pasó el 10 de agosto de
2026.

Lo único que se casa a mano es el **nombre** de cada competición con su id de
división estable (`DIVISION_RULES`). Los ids no se pueden generar: la app los
guarda dentro de cada equipo (`Team.divisionId`, `groupId`) y de ellos cuelgan
el calendario, la importación de rivales y las novedades. Los nombres aguantan
de una temporada a otra; los códigos no.

Los equipos salen, por orden: de la **clasificación** (trae escudo oficial, pero
no existe hasta la J1), del **PDF de calendario de rfef.es** (`scrapers/rfef_web.py`,
con la temporada leída de su portada), del **calendario de la PNFG** (existe desde
el sorteo), del PDF oficial legacy y del fallback curado. La **página de
competición** de rfef.es ya no es fuente de equipos (ver «Nada se publica sin
poder fecharlo»): solo aporta los nombres cortos.

Las dos fuentes de `rfef.es` son las que funcionan en pretemporada. Y hay una regla
que no conviene deshacer: **ninguna URL de PDF se escribe a mano**. Se descubren
siguiendo los enlaces de `/es/competiciones/<slug>` a las noticias que los publican,
porque los nombres de fichero cambian sin avisar y no se pueden deducir
(`calendario_2af_g2-1-3.pdf` en 2026-27 donde antes había
`calendario_grupo_2_segunda_femenina_futbol_sala.pdf`). Esa segunda URL, la de siempre,
**sigue devolviendo 200 con el PDF de 2025-26**: es como los 48 equipos del año pasado
acabaron publicados como si fueran de 2026-27. Por eso cada PDF se acepta solo si su
portada declara la temporada pedida, y se atribuye a una división por el nombre de
competición que él mismo imprime — no por qué página lo enlazaba, que trae las noticias
de media federación.

Lo único escrito a mano de esta fuente son los slugs de las páginas de competición
(`rfef_web.COMPETITION_SLUGS`). Describen la competición, no su edición, así que aguantan
entre temporadas — pero el de Primera Femenina lleva el patrocinador dentro
(`primera-futbol-sala-iberdrola`), y ese es el candidato a romperse. Si uno deja de
responder, su división se queda sin esta fuente y el run lo dice; no se rellena con otra
cosa.

**Al cambiar de temporada no hay que hacer nada.** Las divisiones que la
federación aún no haya publicado —las masculinas de LNFS suelen ir semanas por
detrás de las femeninas— salen listadas en `warnings` del JSON y aparecen solas
en el run siguiente.

### El nombre se puede cambiar; el id no

`DIVISION_NAMES` es solo la etiqueta del desplegable y se corrige cuando haga
falta. El id de al lado (`rfef-segunda-fs-masc`) va guardado dentro de cada
equipo de la app (`Team.divisionId`) y de él cuelgan el calendario, la
importación de rivales y las novedades: cambiarlo deja a todos los equipos
existentes sin liga.

Ejemplo real: esa división se llamaba "Segunda División FS **A**" y ahora se
llama "Segunda División FS". No hay ninguna "Segunda A" — la categoría es
Segunda División y la de abajo Segunda División B. El id no se tocó.

### Los códigos de la PNFG también están en la web

La página de competición enlaza su calendario en la PNFG ("Actas, clasificación
y calendario") y ese enlace lleva competición, grupo y temporada dentro. Sirve
para recuperar el código de grupo cuando el catálogo de la PNFG —su llamada más
frágil— se come el rate-limit.

Pero **la mitad de esos enlaces están sin actualizar**: el 29/08/2026, Segunda y
Primera Femenina apuntaban a `CodTemporada=22` (2026-27) y Segunda B a `21`, y
encima al playoff de ascenso del año pasado. Por eso solo se acepta si la
temporada coincide con la resuelta y la competición es la ya descubierta.

### Nada se publica sin poder fecharlo

Cada división anota **de dónde salieron sus equipos** (`teamsSource` en el JSON),
y `_drop_unverified_teams` vacía las que no vengan de una fuente que permita
afirmar la temporada:

| fuente | por qué se puede fechar |
|---|---|
| `clasificacion` | comp/grupo descubiertos para esta temporada |
| `calendario` | el calendario de la PNFG pedido con `CodTemporada` explícito |
| `pdf-rfef`, `pdf-legacy` | la portada imprime "Temporada 2026-2027" |
| `fallback` | `data/rfef-fallback.json` declara su `season` |
| `pagina` | **no se puede** — rfef.es no dice de qué año es |

La página de competición dejó de ser fuente de equipos por eso. Sigue aportando
los nombres cortos, que es seguro: un corto solo se le pega a un equipo que ya
vino de una fuente fechada, y el emparejado es 1:1.

El guard recorre **lo que se va a publicar**, no lo que se cree haber hecho. Las
dos veces que este scraper sacó la liga del año pasado el run terminó en verde,
y las dos por un camino que nadie había fechado. Prefiere una división vacía
—la app degrada al formulario manual— a una llena de la temporada equivocada.

### El calendario del PDF va a dos columnas

La RFEF maqueta la ida a la izquierda y la vuelta a la derecha, con la J1 y la
J16 empezando en la misma línea. Leído como texto plano, cada línea salía como un
partido inventado entre el visitante de la ida y el local de la vuelta: 240
nombres distintos para un grupo de 16 equipos.

`rfef_web.parse_calendar_multicolumn` las separa por la **x del guion**, que es
fija por columna. Ojo con dos cosas si se toca: un guion dentro de un nombre
también está a x fija (por eso el umbral es relativo al separador más frecuente,
no absoluto), y la columna de una cabecera sale de su x y no de su orden en la
línea (en Segunda las dos cabeceras no están alineadas).

Y **nunca se publica un calendario que nombre a un equipo que no está en el
plantel**: las filas con nombres muy largos se parten en varias líneas y dejan
trozos sueltos ("MRB FS", "C.E."). Un nombre de más descarta el calendario
entero; uno de menos se acepta con aviso, porque tirarlo dejaría sin autorrelleno
a los otros quince equipos para proteger a uno.

### El guard de temporada

Si no se puede verificar contra qué temporada se está scrapeando, el run
**aborta sin escribir `output/leagues.json`** y el paso de publicación no llega
a ejecutarse: gh-pages conserva el anterior. Un JSON viejo es incómodo; uno con
datos del año pasado y fecha de hoy es una mentira que nadie detecta.

Ahora bien, **de quién es el problema decide el código de salida**, y eso existe
para que el email de fallo de Actions siga significando algo:

| Situación | Salida |
|---|---|
| La federación aún no ha abierto la temporada (normal cada julio) | **0** — no publica, no avisa |
| La PNFG no tiene todavía ninguna división de sala | **0** — igual |
| No se pudo consultar (rate-limit, red, cambio de HTML) | **1** — falla el run y manda email |

Sin esa distinción, entre julio y que la federación abra habría un fallo diario
que siempre es normal, y en dos veranos nadie mira esos emails — que son también
la única alerta del keep-alive de Supabase.

Por lo mismo, `data/rfef-fallback.json` solo se usa si su campo `season`
coincide con la temporada pedida. Al empezar temporada: o se actualizan los
equipos **y** el `season`, o se deja como está y simplemente no se usa. Subir el
`season` sin cambiar los equipos es exactamente el fallo que esto evita.

### El cupo de la PNFG, y cómo se reparte el trabajo

La PNFG bloquea **devolviendo 200 con el cuerpo vacío**, no un 429, así que un
bloqueo no se distingue de "esta jornada no existe". Durante meses se trató como
un límite de ritmo y se reintentaba —15+30+60+120 = 225 s por jornada—; medido
en octubre de 2026 resultó ser otra cosa (detalle en `scrapers/pnfg_pacer.py`,
§6.55 y §6.86 del informe técnico):

- **Es un cupo, y es de la IP**: unas 15-20 peticiones, sean de la página que
  sean y vayan al ritmo que vayan. Una sesión nueva no lo esquiva.
- Al pasarse viene un corte de varios minutos que **se alarga si se sigue
  pidiendo durante él y escala con cada reincidencia**. Reintentar, que es lo
  que se hacía, era exactamente lo que lo empeoraba.
- Las pausas de 15 minutos apenas lo recargan.

Por eso un run viejo, con ~360 peticiones (una por jornada), se quedaba siempre
en la J12 de cada grupo. Lo que lo resolvió no fue esperar mejor, sino **pedir
mucho menos**:

| Pieza | Qué hace | Dónde |
|---|---|---|
| Vista completa del calendario | `NFG_VisCalendario_Vis?CDetalle=1`: las 30 jornadas de un grupo, con hora y pabellón, en **una** petición | `rfef_calendario.fetch_season_calendar` |
| Actas con tope | `NFG_CmpJornada` solo para jornadas jugadas en los últimos 45 días y sin acta, la más reciente primero; como mucho 2 por grupo y 8 por run | `acta_jornadas`, `rfef._acta_budget` (`PNFG_ACTAS_GROUP`, `PNFG_ACTAS_MAX`) |
| Descubrimiento guardado | La temporada, las competiciones y los grupos se reutilizan 6 días (ninguno en julio-septiembre) si son de la misma temporada | `rfef.load_discovery`, `data/discovery-cache.json` |
| Reparto entre runs | Cada grupo apunta cuándo se bajó entero; si fue en las últimas 20 h sale de la caché y no se pide | `calendar_cache.fetched_within` (`PNFG_CAL_REFRESH_HOURS`) |
| Tandas | 6 peticiones y 900 s de silencio; cuentan también las redirecciones que se quedan en la PNFG | `pnfg_pacer.Pacer` (`PNFG_BURST`, `PNFG_REST`) |
| Parada al primer corte | Desde el primero, `PacedSession` contesta "vacío" sin salir a la red y los tres módulos que piden se rinden por sus caminos de siempre | `pnfg_pacer.stopped()` |

Un día entero son unas 35 peticiones y no caben en un run, pero cada run de
Actions sale por una IP nueva: de ahí el cron cada 4 horas, en el que cada run
hace lo que quepa y el siguiente sigue donde lo dejó el anterior. Con eso los 12
grupos se publican con sus 30 jornadas (antes del cambio, 4).

**El camino de jornada en jornada sigue ahí, como respaldo** de la vista
completa, con sus tres redes, y cada una cubre lo que las otras no:

| Pieza | Qué hace | Dónde |
|---|---|---|
| `RateLimitBreaker` | deja de pedir | dentro del run |
| `calendar_cache` (`_calendars`) | rellena por jornada | entre runs, sin red |
| `inherit_calendars` | conserva el más completo | entre runs, contra gh-pages |

**El cortacircuitos** abandona un grupo tras 3 jornadas seguidas sin respuesta y
el run entero tras 12 fallos, o al agotar el tope de tiempo de la fase de
calendario (`PNFG_CALENDAR_MINUTES`, 250 min: el job muere a las 6 h sin
publicar), o en cuanto la PNFG ha cortado. El presupuesto es del run y no por
grupo a propósito: doce grupos con dos fallos describen el mismo servidor
enfadado que un grupo con veinticuatro. **Abandonar no es perder**: lo que el
breaker deja a medias lo rellena la caché de jornadas. Si algún día se quitan
las otras dos piezas, hay que quitar el breaker con ellas.

**La caché guarda solo lo que viene fresco de la PNFG**, nunca lo derivado del
PDF. Si entrara el PDF, una jornada con hora acabaría pisada por la misma jornada
sin ella y la caché iría hacia atrás. Al fusionar manda lo fresco —así un
aplazamiento se corrige al volver a bajar esa jornada— con una excepción: si lo
fresco no trae hora en ningún partido y la caché sí, gana la caché. Y el PDF
oficial (que viene de rfef.es y no gasta cupo) completa las jornadas que la PNFG
dejó a medias: antes solo se miraba si la PNFG no daba nada (`if not calendar`),
y un grupo con 14 de 15 jornadas se publicaba con 14. Pasó el 2026-08-31 con
Primera masculina.

**Liga Prime publica Apertura y Clausura**, una petición cada una, y cada
jornada lleva su `phase`.

Cuando el run se recorta, el JSON publicado lo dice en `warnings` («la PNFG cortó
a mitad de run…», o el tope de tiempo). Va ahí y no solo al log porque un run
recortado se parece demasiado a uno normal. El workflow, además, lo enseña como
aviso en Actions: si se repite varios días, ver `docs/plan-b-runner-casa.md`.

### FCF (manual, ~30 min/año)
Edita `data/fcf-manual.json`:
1. Para cada división donde juegues, rellena el array `teams` con los rivales de la temporada actual
2. Si juegas en un grupo territorial concreto, duplica la división con un id específico (ej. `fcf-1cat-fs-fem-grup-2`)
3. Los escudos los puedes dejar como `null`. Ojo: el resolver ya **no** busca en Wikipedia (se retiró en agosto de 2026, `IP-004`); solo los rellena si el club está en la allowlist curada (`data/badges-overrides.json`), en el portal oficial o en la caché. Si hace falta uno, se añade a la allowlist con su procedencia.

Push al repo y GitHub Actions lo recoge en el siguiente run (o trigger manual desde la UI).

**Hoy el fichero está sin rellenar** (las seis divisiones con `teams: []`), y por eso **la categoría `fcf` no se publica**: `scrape.py::drop_unfilled_divisions` retira las divisiones curadas vacías —y la categoría si se queda sin ninguna— y lo dice en `warnings`, con el fichero que hay que editar. Antes salían en el desplegable de la app y el entrenador que elegía una no recibía nunca ni rivales ni calendario. A las de RFEF no se les aplica: una división de RFEF vacía está pendiente de que la federación la abra, y debe seguir publicándose para que el móvil que la tenga elegida se traiga sus rivales solo.

### Novedades de federación (manual)
La bandeja de "Novedades" de la app tiene **dos fuentes**, y ésta es solo una:

- **Las novedades del equipo las deduce la propia app** de este calendario, sin que nadie escriba nada: un partido que cambia de hora, un acta que aparece, un calendario que se publica. Es lo que ve el 99 % de los entrenadores, y no requiere mantenimiento (§6.43 del informe técnico).
- **Los comunicados de federación** —una circular, un cambio de normativa— sí se escriben a mano aquí, porque no están en ningún dato que se scrapee.

Se mantienen en `data/notices-manual.json` y `scrape.py` las inyecta en la categoría/división correspondiente:

```jsonc
{
  "categories": {                         // novedad para TODA la federación
    "rfef": [
      { "id": "circ-2026-07",             // id estable (imprescindible para dedup)
        "title": "Nueva normativa de sanciones",
        "body": "…",                      // opcional
        "url": "https://www.rfef.es/…",   // opcional (se abre en el navegador)
        "date": "2026-07-06" }            // opcional (YYYY-MM-DD)
    ]
  },
  "divisions": {                          // novedad solo para una división
    "rfef-segunda-fs-fem": [ { "id": "…", "title": "…" } ]
  }
}
```

- La clave es el `id` de una categoría (`rfef`/`fcf`) o de una división (mira los ids en `output/leagues.json`).
- El `id` de cada novedad debe ser **estable**: la app deduplica por él para no repetir la notificación. Cambiar el `id` = novedad nueva.
- **Nada de avisos de prueba.** Lo que se escriba aquí llega como notificación al móvil de todos los equipos de esa categoría o división, y su `id` queda marcado como notificado para siempre: no se puede retirar publicando de nuevo. El fichero se publicó una vez con dos entradas `ejemplo` — por eso el aviso.

### Reglamento de la temporada (manual, una línea al año)
`data/rules-manual.json` guarda el reglamento o las bases de competición de cada temporada. La app avisa una vez, con enlace al PDF, a los equipos de esa categoría o división.

```jsonc
{
  "categories": {
    "rfef": { "season": "2026-2027",              // OBLIGATORIO, mismo formato que `version`
              "title": "Reglamento General de Fútbol Sala",
              "url": "https://rfef.es/…/reglamento.pdf",   // OBLIGATORIO
              "published": "2026-08-01" }         // opcional
  },
  "divisions": {                                  // bases propias de una división
    "rfef-primera-fs-fem": { "season": "…", "title": "…", "url": "…" }
  }
}
```

- **`scrape.py` sólo publica las entradas cuya `season` coincide con la temporada que se está generando.** Por eso los reglamentos de años anteriores se pueden dejar aquí como histórico: no llegan a nadie. Quitar ese filtro publicaría el documento del año pasado etiquetado como el de éste.
- Si una división tiene bases propias, **mandan sobre** el reglamento general de su federación (la app no suma los dos, elige el más específico).
- Una **revisión a mitad de temporada** se anuncia cambiando la `url`; con el mismo `season` la app lo dice como "Actualizado el reglamento". Cambiar sólo el `title` no genera aviso.
- No está automatizado y no es un olvido: la PNFG no publica el reglamento en ningún endpoint estructurado — cuelga de `rfef.es/sites/default/files/YYYY-MM/` con un nombre impredecible.
- **Estado a 2026-11-14:** el fichero ya lleva las normas reguladoras y bases de competición 2026-27 de la RFEF para las cinco divisiones (tres PDF de `2026-06/`, comprobados leyendo la portada), pero el cambio **está sin commitear**, así que todavía no se publica y la app no avisa de nada. Se publicará en el primer run después de subirlo.

## GitHub Actions

El workflow `.github/workflows/scrape.yml`:
- **Cron:** completo **cada 4 horas, todo el año** (`17 */4 * * *`), con `concurrency` para que dos runs no publiquen ni commiteen las cachés a la vez. Antes era diario en julio-septiembre y semanal el resto del año; cambió con el cupo de la PNFG (ver «El cupo de la PNFG»): cada run sale por una IP nueva, hace lo que quepa y el siguiente sigue donde lo dejó. En la práctica GitHub los lanza con retraso: del 10 al 14 de noviembre de 2026 salieron unos tres al día, de 20 a 50 minutos cada uno (la mayor parte, las pausas de 15 minutos entre tandas).
- **Dos modos**:

  | Modo | Qué trae | Cuándo |
  |---|---|---|
  | completo | equipos + calendario + actas | el cron, y `workflow_dispatch` por defecto |
  | `--teams-only` (`equipos`) | equipos (2 jornadas por grupo) | solo a mano |

  El run rápido **hereda los calendarios del JSON ya publicado**, y no publica si no puede leerlo o si es de otra temporada — sin eso, un fallo de red de treinta segundos dejaría a la app sin autorrelleno por jornada, y heredar de otra temporada sería reintroducir el bug original por la puerta de atrás.
- **Antes del scrape** corren los tests; **después**, si la PNFG cortó, el run sale con un aviso («La PNFG ha cortado») en vez de en silencio.
- **Cachés de vuelta a `main`**: `badges-cache.json`, `calendar-cache.json`, `former-names.json` y `discovery-cache.json`, en un commit `chore: refresh data caches [skip ci]`. Sin ese commit se tirarían con el contenedor y nunca acumularían.
- **Plan B**: con la variable del repo `SCRAPER_RUNNER=self-hosted` el mismo workflow corre en un PC de casa. Ver `docs/plan-b-runner-casa.md`.
- **Manual:** desde la pestaña Actions → "Scrape leagues data" → "Run workflow"
- **Output:** publica `output/leagues.json` en la branch `gh-pages`

Hay un segundo workflow, `pnfg-probe.yml` («Medir cupo de la PNFG»), que es un experimento y solo se lanza a mano: mide el cupo con `tools/pnfg_quota_probe.py`, una estrategia por job para que cada una tenga su propia IP. No toca datos ni publica nada.

URL pública del JSON:
```
https://raw.githubusercontent.com/borgeszone/lbcoach-leagues-data/gh-pages/leagues.json
```

Es la misma que `LeaguesService.remoteJsonUrl` (app), `website/admin/config.js` (panel) y `PUBLISHED_JSON_URL` (`scrape.py`, de donde se hereda). Si se separan, el panel informa sobre un fichero que no usa nadie y el run hereda de otro.

## Setup inicial (primera vez)

```bash
# 1. Crear repo en GitHub (público)
gh repo create lbcoach-leagues-data --public --source=. --push

# 2. Crear la branch gh-pages
git checkout --orphan gh-pages
git rm -rf .
echo "leagues data" > index.html
git add index.html
git commit -m "init gh-pages"
git push origin gh-pages
git checkout main

# 3. Trigger manual del workflow
gh workflow run "Scrape leagues data"

# 4. Verificar
gh run watch
curl https://raw.githubusercontent.com/<owner>/lbcoach-leagues-data/gh-pages/leagues.json | jq .version
# (hoy <owner> es borgeszone)
```

## Estructura del JSON publicado

Recortado del publicado el 2026-11-14:

```json
{
  "version": "2026-2027",
  "lastUpdated": "2026-11-14T15:43:29+00:00",
  "warnings": [
    "rfef: data/rfef-fallback.json es de '2025-2026' y se pidió '2026-2027': se ignora (…)",
    "fcf-1cat-fs-masc: sin equipos curados, no se publica (rellénala en data/fcf-manual.json)",
    "fcf: ninguna división con equipos, la categoría entera no se publica"
  ],
  "categories": [
    {
      "id": "rfef",
      "name": "Liga Española",
      "source": "rfef.es",
      "divisions": [
        {
          "id": "rfef-primera-fs-masc",
          "name": "Primera División FS",
          "gender": "masculino",
          "teams": [
            { "name": "Barça", "logoUrl": "https://rfef.filesnovanet.es/..." },
            {
              "name": "Catgas Energia",
              "officialName": "Catgas Energía Santa Coloma",
              "aliases": ["FS García", "Industrias Santa Coloma"],
              "logoUrl": "https://futsal.rfef.es/..."
            }
          ],
          "calendar": [
            { "jornada": 1, "phase": "Apertura", "matches": [
              { "home": "CD Xota FS", "away": "Jaen Paraiso Interior F.S.",
                "date": "2026-09-11T19:30:00", "venue": "Pab. Anaitasuna (POL)",
                "actaUrl": "https://resultados.rfef.es/pnfg/NPcd/NFG_CMP_Alineacion?..." }
            ] }
          ]
        }
      ]
    }
  ]
}
```

La categoría `fcf` («Liga Catalana») solo aparece cuando alguna de sus divisiones
tiene equipos (ver «FCF»). Las divisiones con grupos llevan `groups`, cada uno con
sus `teams` y su `calendar`.

`officialName` es **opcional** y solo aparece cuando el club tiene dos nombres: el corto
que publica la federación en su página de competición ("Catgas Energia") y el largo con
patrocinador que sale en el acta y en el calendario ("Catgas Energía Santa Coloma"). La app
enseña `name` y usa los dos para emparejar. Un cliente viejo que no lo conozca lo ignora y se
queda con el corto, que es el que quiere enseñar de todas formas.

`aliases` es **opcional** y lleva los nombres que el club ha tenido antes **en esta
temporada**. Sale de dos sitios: `scrapers/team_renames.py`, que junta los dos nombres de un
equipo que cambia de nombre a mitad de liga dentro del mismo grupo (nunca coinciden en una
jornada y se parecen), y `scrapers/former_names.py`, que compara cada run con el publicado y
recuerda lo aprendido en `data/former-names.json` (dos nombres son del mismo club si
comparten un nombre o si ocupan el mismo hueco del calendario en dos jornadas o más; nunca
por parecido). La app los usa para reconocer al rival guardado con el nombre de antes.
Al cambiar de temporada `former-names.json` empieza de cero.

En el calendario, `phase` solo existe en Liga Prime (Apertura / Clausura), `venue` solo si la
federación ya asignó pabellón, y `actaUrl` solo cuando el acta está publicada. Una fecha sin
hora (la PNFG la escribe `00:00`) se publica con el día a secas.

Opcionales a nivel de categoría o división: `notices` (de `data/notices-manual.json`) y
`rules` (de `data/rules-manual.json`, solo los de la temporada que se genera).
