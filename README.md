# Descargador Universal

Descarga música (MP3 con carátula y etiquetas), vídeo, audio y **transcripciones** de YouTube y otras plataformas
con una interfaz web local. Proyecto gratuito, disfrútalo.

## Cómo se mantiene al día solo

| Qué | Cómo |
| --- | --- |
| **yt-dlp** | Cada 6 horas (y 20 s después de abrir el programa) se comprueba PyPI. Si hay versión nueva se instala (`yt-dlp[default]`, con `yt-dlp-ejs`), se limpia la caché de YouTube y el motor **se recarga en caliente, sin reiniciar**. Si hay descargas en curso, se recarga al terminar. |
| **Autorreparación** | Si una descarga falla con un error típico de cambios en YouTube (firma, 403, formato no disponible…), el programa limpia la caché, busca una yt-dlp más nueva y reintenta una vez. |
| **La app** | Se compara `version.json` de este repositorio. El código nuevo se descarga a una carpeta temporal, se verifica (SHA-256, sintaxis y arranque de prueba) y solo entonces se instala, con copia de seguridad en `_backup/`. Después el programa se reinicia solo cuando está inactivo y la página se recarga sola. |
| **Deno** | Se comprueba que exista y cumpla la versión mínima que exige la yt-dlp instalada. |

La auto-actualización se puede desactivar con `{"auto_update": false}` en `config.json`
(o `POST /api/system/auto-update`). El botón de actualizar de la web hace lo mismo a mano.

## Uso

* **Normal:** `Iniciar Descargador.bat`
* **Portable:** `YT Music Downloader Portable/Iniciar Descargador Portable.bat`

Los `.bat` relanzan el programa cuando este pide reiniciarse (código de salida 75).
Si el puerto 5000 está ocupado por otro programa se usa el siguiente libre; si ya hay una instancia abierta, solo se abre la página.

## Reportes de errores a Discord

La URL del webhook **no** está en el código. Se lee de la variable de entorno `BUG_REPORT_WEBHOOK` o del archivo
local `bug_webhook.txt` (ignorado por git).

## Seguridad

El servidor escucha solo en `127.0.0.1` y valida `Host` y `Origin` (anti CSRF / DNS rebinding), bloquea SSRF en las
descargas de miniaturas, escapa todo el contenido externo en la interfaz y envía cabeceras de seguridad.

## Desarrollo

```bash
python -m unittest discover -s tests -v       # pruebas (sin red)
python make_release.py 2.1.1 "Qué cambia"     # sube la versión y genera los hashes de version.json
```

Después de `make_release.py`, sube `app.py`, `templates/index.html`, `static/style.css` y `version.json` a `main`:
los usuarios recibirán la actualización solos.
