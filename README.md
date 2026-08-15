# lbcoach-leagues-data

Genera y publica `leagues.json` — la fuente de datos que la app **GoalDash / lbcoach** descarga al crear un equipo para autorrellenar la lista de rivales con sus escudos.

## Cómo funciona

```
┌─────────────────────────────────────┐
│ scrape.py                           │
│   ├─ scrapers/rfef_discovery.py     │  Qué competiciones tiene la temporada
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
no existe hasta la J1), del **calendario** (existe desde el sorteo — es lo que
funciona en pretemporada), del PDF oficial, y del fallback curado.

**Al cambiar de temporada no hay que hacer nada.** Las divisiones que la
federación aún no haya publicado —las masculinas de LNFS suelen ir semanas por
detrás de las femeninas— salen listadas en `warnings` del JSON y aparecen solas
en el run siguiente.

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

### FCF (manual, ~30 min/año)
Edita `data/fcf-manual.json`:
1. Para cada división donde juegues, rellena el array `teams` con los rivales de la temporada actual
2. Si juegas en un grupo territorial concreto, duplica la división con un id específico (ej. `fcf-1cat-fs-fem-grup-2`)
3. Los escudos los puedes dejar como `null` — el resolver de Wikipedia los rellenará si encuentra el club

Push al repo y GitHub Actions corre solo (o trigger manual desde la UI).

### Novedades de federación (manual)
La app muestra "Novedades de tu liga" (notificación local + bandeja con badge) a partir del campo `notices` del JSON. Se mantienen a mano en `data/notices-manual.json` y `scrape.py` las inyecta en la categoría/división correspondiente:

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
- El fichero incluye entradas `ejemplo`; bórralas cuando confirmes el flujo.

## GitHub Actions

El workflow `.github/workflows/scrape.yml`:
- **Cron:** diario a las 06:00 UTC en julio-septiembre (pretemporada: las divisiones van abriendo de una en una), semanal los lunes el resto del año
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
            { "name": "Barça", "logoUrl": "https://upload.wikimedia.org/..." }
          ]
        }
      ]
    },
    { "id": "fcf", "name": "Liga Catalana", "divisions": [...] }
  ]
}
```
