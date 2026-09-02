# lbcoach-leagues-data

Genera y publica `leagues.json` — la fuente de datos que la app **GoalDash / lbcoach** descarga al crear un equipo para autorrellenar la lista de rivales con sus escudos.

## Cómo funciona

```
┌─────────────────────────────────────┐
│ scrape.py                           │
│   ├─ scrapers/rfef_discovery.py     │  Qué competiciones tiene la temporada
│   ├─ scrapers/rfef_web.py           │  Equipos desde rfef.es (pretemporada)
│   ├─ scrapers/rfef.py               │  Equipos + calendario (PNFG, PDF)
│   ├─ scrapers/fcf.py                │  Lee data/fcf-manual.json
│   └─ scrapers/logo_resolver.py      │  Escudos oficiales → Wikipedia
└─────────────────────────────────────┘
              │
              ▼
       output/leagues.json
              │
              ▼
       Branch `gh-pages`
              │
              ▼
  https://raw.githubusercontent.com/<owner>/lbcoach-leagues-data/gh-pages/leagues.json
              │
              ▼
       App Flutter (lbcoach)
```

## Uso local

```bash
pip install -r requirements.txt
python scrape.py
# Salida: output/leagues.json

python -m unittest discover -s tests -v   # no toca la red
```

Opciones:
- `--season 2026-2027` — cambiar la temporada
- `--no-badges` — saltar la resolución de escudos (más rápido para iterar)

Los tests parsean **HTML real** capturado de la PNFG (`tests/fixtures/`), no
HTML inventado. Se ejecutan también en el workflow, antes del scrape.

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
el sorteo), del PDF oficial legacy, de la **página de competición** de rfef.es, y del
fallback curado.

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
| `calendario` | `NFG_CmpJornada` con `CodTemporada` explícito |
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

### El rate-limit de la PNFG, y por qué costaba horas

La PNFG bloquea **devolviendo 200 con el cuerpo vacío**, no un 429. Eso importa
porque no se distingue de "esta jornada no existe", así que la única respuesta
posible era reintentar: 15+30+60+120 = **225 s por jornada**. Con ~360 jornadas
por run completo (12 grupos × ~30), un run bloqueado se pasaba horas
reintentando —alimentando el bloqueo que causó el reintento— y moría en el corte
de 6 h de Actions **sin llegar a publicar**.

Tres piezas, y cada una cubre lo que las otras no:

| Pieza | Qué hace | Dónde |
|---|---|---|
| `RateLimitBreaker` | deja de pedir | dentro del run |
| `calendar_cache` (`_calendars`) | rellena por jornada | entre runs, sin red |
| `inherit_calendars` | conserva el más completo | entre runs, contra gh-pages |

**El cortacircuitos** abandona un grupo tras 3 jornadas seguidas sin respuesta
(11 min de evidencia; la cuarta no informa de nada nuevo) y el run entero tras 12
fallos. El presupuesto es del run y no por grupo a propósito: doce grupos con dos
fallos describen el mismo servidor enfadado que un grupo con veinticuatro, y el
objetivo es dejar de pegarle. Además **los reintentos se hunden a uno tras el
primer fallo y se recuperan con la primera respuesta buena**, que es lo que evita
que un corte transitorio degrade el resto del run.

**Abandonar no es perder**, y ésa es la condición para que cortar antes sea una
mejora y no un recorte: lo que el breaker deja a medias lo rellena la caché de
jornadas. Si algún día se quitan las otras dos piezas, hay que quitar el breaker
con ellas.

**La caché guarda solo lo que viene fresco de la PNFG**, nunca lo derivado del
PDF. Si entrara el PDF, una jornada con hora acabaría pisada por la misma jornada
sin ella y la caché iría hacia atrás. Al fusionar manda lo fresco —así un
aplazamiento se corrige al volver a bajar esa jornada— con una excepción: si lo
fresco no trae hora en ningún partido y la caché sí, gana la caché.

Esto arregla además un caso que no era de bloqueo total sino **a medias**: el
fallback al PDF es `if not calendar`, un booleano sobre la lista, así que un
grupo que trajera 14 de 15 jornadas se publicaba con 14 y el PDF ni se
consultaba. Pasó el 2026-08-31 con Primera masculina (le faltaba la J12).

Cuando el breaker recorta algo, el JSON publicado lo dice en `warnings`. Va ahí y
no solo al log porque un run recortado se parece demasiado a uno normal: sin eso,
la única diferencia visible entre "la federación no lo ha publicado" y "nos
bloquearon" es un recuento de jornadas que nadie mira.

> **Lo que esto NO hace: bajar el número de peticiones.** Sigue siendo una por
> jornada. El siguiente paso natural es invertir la cascada —PDF como base (1
> petición por grupo, las 30 jornadas con su día) y PNFG solo para la ventana
> alrededor de hoy (hora y `actaUrl`, que es lo único que cambia)—, que llevaría
> el run de ~360 peticiones a ~84. No está hecho.

### FCF (manual, ~30 min/año)
Edita `data/fcf-manual.json`:
1. Para cada división donde juegues, rellena el array `teams` con los rivales de la temporada actual
2. Si juegas en un grupo territorial concreto, duplica la división con un id específico (ej. `fcf-1cat-fs-fem-grup-2`)
3. Los escudos los puedes dejar como `null` — el resolver de Wikipedia los rellenará si encuentra el club

Push al repo y GitHub Actions corre solo (o trigger manual desde la UI).

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

## GitHub Actions

El workflow `.github/workflows/scrape.yml`:
- **Cron:** diario a las 06:00 UTC en julio-septiembre (pretemporada), lunes el resto del año. Los meses no se solapan a propósito: si los dos cron casaran el mismo día, GitHub lanzaría dos runs que competirían por publicar
- **Dos modos**, porque cuestan cosas muy distintas:

  | Modo | Qué trae | Coste | Cuándo |
  |---|---|---|---|
  | `--teams-only` | equipos (2 jornadas por grupo) | ~1 min | cron de martes a domingo |
  | completo | equipos + las ~30 jornadas | ~3 h | lunes, y `workflow_dispatch` por defecto |

  El run rápido **hereda los calendarios del JSON ya publicado**, y no publica si no puede leerlo o si es de otra temporada — sin eso, un fallo de red de treinta segundos dejaría a la app sin autorrelleno por jornada, y heredar de otra temporada sería reintroducir el bug original por la puerta de atrás.

  El reparto no es arbitrario: el calendario de una temporada apenas cambia (lo que cambia dentro son las actas, y esas ya tienen caché acumulativa), mientras que en agosto lo que cambia a diario es qué divisiones existen. Bajarse las 30 jornadas cada día era pegarle hora y media diaria a la federación desde la misma IP, provocando en parte los bloqueos que luego hacen perder jornadas.
- **Manual:** desde la pestaña Actions → "Scrape leagues data" → "Run workflow"
- **Output:** publica `output/leagues.json` en la branch `gh-pages`

URL pública del JSON tras el primer despliegue:
```
https://raw.githubusercontent.com/<owner>/lbcoach-leagues-data/gh-pages/leagues.json
```

Esa URL es la que usará la app Flutter. Configurarla en `lib/leagues_service.dart` cuando se implemente la Fase B.

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
```

## Estructura del JSON publicado

```json
{
  "version": "2026-2027",
  "lastUpdated": "2026-08-15T06:00:00+00:00",
  "warnings": [
    "rfef: sin publicar en la PNFG para 2026-2027: rfef-primera-fs-masc, rfef-segunda-fs-masc"
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
              "name": "Osasuna Magna",
              "officialName": "C.A. Osasuna Magna",
              "logoUrl": null
            }
          ]
        }
      ]
    },
    { "id": "fcf", "name": "Liga Catalana", "divisions": [...] }
  ]
}
```

`officialName` es **opcional** y solo aparece cuando el club tiene dos nombres: el corto
que publica la federación en su página de competición ("Osasuna Magna") y el largo con
patrocinador que sale en el acta y en el calendario ("C.A. Osasuna Magna"). La app enseña
`name` y usa los dos para emparejar. Un cliente viejo que no lo conozca lo ignora y se
queda con el corto, que es el que quiere enseñar de todas formas.
