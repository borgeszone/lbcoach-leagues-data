# Plan B: el scraper desde un PC de casa

El scraper corre en GitHub Actions. Cada run sale por una IP distinta del pool
de GitHub y hace unas 40-50 peticiones a la PNFG: el calendario de cada grupo en
una, como mucho 8 páginas de jornada para las actas (la página cuyo cupo se ha
medido), y todo en tandas de 6 con 15 minutos de silencio entre ellas. Es menos
de lo que la PNFG deja (ver `scrapers/pnfg_pacer.py`).

Este plan es para el día en que eso deje de funcionar: **si la PNFG empieza a
bloquear las IP de centros de datos**, o si el cupo baja tanto que ni las tandas
lo respetan. Hoy (octubre de 2026) no pasa.

## Cómo se sabe que toca

- El run de Actions sale con el aviso **«La PNFG ha cortado»**.
- En `leagues.json`, `warnings` dice «la PNFG cortó a mitad de run».
- El panel enseña grupos con menos jornadas de las que tienen.

**Un aviso suelto no es motivo**: lo que falta lo pone la caché y lo pide el run
del día siguiente, que sale por otra IP. Toca actuar si se repite varios días
seguidos. Antes del plan B, prueba lo barato en `.github/workflows/scrape.yml`:
bajar `PNFG_ACTAS_MAX` a 4 y `PNFG_BURST` a 4.

## Qué es

Un *self-hosted runner*: un programa de GitHub que se instala en un PC y recoge
los trabajos de Actions de este repo. El workflow es el mismo y publica igual en
gh-pages. Lo único que cambia es que las peticiones salen por la IP de casa.

El precio es que **ese PC tiene que estar encendido** a la hora del cron
(06:00 UTC: las 8:00 en verano y las 7:00 en invierno, hora de España) durante
1-2 horas. Si está apagado, el trabajo espera en cola hasta 24 h y luego falla.

## Activarlo (unos 10 minutos)

En el PC de casa tiene que estar instalado **Git for Windows**, que trae el
`bash` que usan los pasos del workflow. Python no hace falta: lo instala el
propio workflow.

1. En GitHub: **Settings → Actions → Runners → New self-hosted runner**, y elige
   Windows x64.
2. Sigue los comandos que te enseña esa página en PowerShell, en una carpeta
   tipo `C:\actions-runner`. En el paso de `config.cmd`, acepta la etiqueta por
   defecto (`self-hosted`) y responde **sí** a ejecutarlo como servicio, para
   que arranque solo con Windows.
3. En **Settings → Secrets and variables → Actions → Variables**, crea la
   variable `SCRAPER_RUNNER` con el valor `self-hosted`.
4. Lanza el workflow a mano (**Actions → Scrape leagues data → Run workflow**)
   y comprueba que corre en tu PC y que termina publicando.

## Volver a GitHub

Borra la variable `SCRAPER_RUNNER`. El runner de casa puede quedarse instalado
sin hacer nada, o quitarse desde la misma pantalla de Runners.

## Seguridad: no lo actives sin leer esto

GitHub desaconseja los runners propios en repositorios **públicos**, y este lo
es (gh-pages tiene que serlo). Un runner ejecuta el código del workflow en tu
PC, así que cualquier workflow que pudiera lanzar alguien de fuera ejecutaría su
código en tu casa.

Hoy no hay forma de hacerlo: los dos workflows solo se lanzan con el cron o a
mano (`schedule` y `workflow_dispatch`), nunca con un `pull_request`. Para que
siga así:

- **No añadas nunca** un disparador `pull_request` o `pull_request_target` a un
  workflow mientras el runner de casa esté activo.
- En **Settings → Actions → General**, deja marcado que los workflows de forks
  necesiten aprobación.
- Instala el runner con un usuario de Windows sin permisos de administrador si
  puedes.
