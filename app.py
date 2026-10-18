import os
import re
import sys
import json
import uuid
import signal
import threading
import queue
import subprocess
import shutil
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import requests
from flask import Flask, render_template, request, jsonify, Response
from yt_dlp import YoutubeDL
from yt_dlp.utils import download_range_func
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, APIC, ID3NoHeaderError

# ─── Configuration ──────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# If running inside a "software" subfolder (Portable mode)
if os.path.basename(BASE_DIR).lower() == "software":
    ROOT_DIR = os.path.dirname(BASE_DIR)
else:
    ROOT_DIR = BASE_DIR

CANCIONES_DIR = os.path.join(ROOT_DIR, "canciones")
VIDEOS_DIR = os.path.join(ROOT_DIR, "videos")
SUBTITULOS_DIR = os.path.join(ROOT_DIR, "solo subtitulos")
MINIATURAS_DIR = os.path.join(ROOT_DIR, "miniaturas")

os.makedirs(CANCIONES_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR, exist_ok=True)
os.makedirs(SUBTITULOS_DIR, exist_ok=True)
os.makedirs(MINIATURAS_DIR, exist_ok=True)

# Ensure FFmpeg is in PATH (checks local portable folder, winget links, etc.)
local_ffmpeg = os.path.join(BASE_DIR, "ffmpeg")
winget_links = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links")
if os.path.exists(local_ffmpeg) and local_ffmpeg not in os.environ.get("PATH", ""):
    os.environ["PATH"] = local_ffmpeg + os.pathsep + os.environ.get("PATH", "")
if os.path.exists(winget_links) and winget_links not in os.environ.get("PATH", ""):
    os.environ["PATH"] = winget_links + os.pathsep + os.environ.get("PATH", "")

app = Flask(__name__)

# Common yt-dlp options to avoid 403 errors and enforce single-media extraction
YDL_COMMON = {
    'extractor_args': {'youtube': ['player_client=default,ios']},
    'noplaylist': True,
}
SUPPORTED_BROWSERS = {'chrome', 'edge', 'brave', 'firefox', 'opera', 'vivaldi', 'chromium', 'safari'}
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
}

# Progress tracking for video downloads and system updates
download_tasks = {}  # task_id -> queue.Queue
update_tasks = {}    # task_id -> queue.Queue

# ─── Language Names (Spanish) ───────────────────────────────────────────────────
LANG_NAMES = {
    'af': 'Afrikaans', 'am': 'Amarico', 'ar': 'Arabe', 'az': 'Azerbaiyano',
    'be': 'Bielorruso', 'bg': 'Bulgaro', 'bn': 'Bengali', 'bs': 'Bosnio',
    'ca': 'Catalan', 'ceb': 'Cebuano', 'co': 'Corso', 'cs': 'Checo',
    'cy': 'Gales', 'da': 'Danes', 'de': 'Aleman', 'el': 'Griego',
    'en': 'Ingles', 'eo': 'Esperanto', 'es': 'Espanol', 'et': 'Estonio',
    'eu': 'Vasco', 'fa': 'Persa', 'fi': 'Finlandes', 'fil': 'Filipino',
    'fr': 'Frances', 'fy': 'Frison', 'ga': 'Irlandes', 'gd': 'Gaelico',
    'gl': 'Gallego', 'gu': 'Guyarati', 'ha': 'Hausa', 'haw': 'Hawaiano',
    'he': 'Hebreo', 'hi': 'Hindi', 'hmn': 'Hmong', 'hr': 'Croata',
    'ht': 'Criollo haitiano', 'hu': 'Hungaro', 'hy': 'Armenio',
    'id': 'Indonesio', 'ig': 'Igbo', 'is': 'Islandes', 'it': 'Italiano',
    'iw': 'Hebreo', 'ja': 'Japones', 'jv': 'Javanes', 'jw': 'Javanes',
    'ka': 'Georgiano', 'kk': 'Kazajo', 'km': 'Jemer', 'kn': 'Canares',
    'ko': 'Coreano', 'ku': 'Kurdo', 'ky': 'Kirguis', 'la': 'Latin',
    'lb': 'Luxemburgues', 'lo': 'Lao', 'lt': 'Lituano', 'lv': 'Leton',
    'mg': 'Malgache', 'mi': 'Maori', 'mk': 'Macedonio', 'ml': 'Malayalam',
    'mn': 'Mongol', 'mr': 'Marati', 'ms': 'Malayo', 'mt': 'Maltes',
    'my': 'Birmano', 'ne': 'Nepali', 'nl': 'Neerlandes', 'no': 'Noruego',
    'ny': 'Chichewa', 'or': 'Oriya', 'pa': 'Panyabi', 'pl': 'Polaco',
    'ps': 'Pastun', 'pt': 'Portugues', 'pt-BR': 'Portugues (Brasil)',
    'ro': 'Rumano', 'ru': 'Ruso', 'rw': 'Kinyarwanda', 'sd': 'Sindhi',
    'si': 'Cingales', 'sk': 'Eslovaco', 'sl': 'Esloveno', 'sm': 'Samoano',
    'sn': 'Shona', 'so': 'Somali', 'sq': 'Albanes', 'sr': 'Serbio',
    'st': 'Sesoto', 'su': 'Sundanes', 'sv': 'Sueco', 'sw': 'Suajili',
    'ta': 'Tamil', 'te': 'Telugu', 'tg': 'Tayiko', 'th': 'Tailandes',
    'tk': 'Turcomano', 'tl': 'Tagalo', 'tr': 'Turco', 'tt': 'Tartaro',
    'ug': 'Uigur', 'uk': 'Ucraniano', 'ur': 'Urdu', 'uz': 'Uzbeko',
    'vi': 'Vietnamita', 'xh': 'Xhosa', 'yi': 'Yiddish', 'yo': 'Yoruba',
    'zh': 'Chino', 'zh-CN': 'Chino (Simplificado)',
    'zh-Hans': 'Chino (Simplificado)', 'zh-Hant': 'Chino (Tradicional)',
    'zh-TW': 'Chino (Tradicional)', 'zu': 'Zulu',
    'es-419': 'Espanol (Latinoamerica)', 'en-US': 'Ingles (EE.UU.)',
    'en-GB': 'Ingles (Reino Unido)', 'fr-CA': 'Frances (Canada)',
}


def get_lang_name(code):
    """Get human-readable language name from ISO code."""
    return LANG_NAMES.get(code, code)


# ─── Helpers ────────────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    """Remove characters that are invalid in Windows filenames."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.strip('. ')
    return name[:200]


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    if not text:
        return ''
    return re.sub(r'\x1b\[[0-9;]*m', '', str(text))


def normalize_media_url(url: str) -> str:
    """
    Sanitize URLs from YouTube/YouTube Music that contain both video ID and playlist/mix parameters.
    Removes list, index, and start_radio to ensure single video processing.
    Preserves original URL for pure playlists or other platforms (TikTok, Instagram, X, etc.).
    """
    if not url:
        return ''
    url = url.strip()
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if any(h in netloc for h in ['youtube.com', 'youtu.be']):
            query = parse_qs(parsed.query, keep_blank_values=True)
            has_video_id = 'v' in query or ('youtu.be' in netloc and bool(parsed.path.strip('/')))
            if has_video_id and any(p in query for p in ['list', 'index', 'start_radio']):
                query.pop('list', None)
                query.pop('index', None)
                query.pop('start_radio', None)
                new_query = urlencode(query, doseq=True)
                return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))
    except Exception:
        pass
    return url


def apply_browser_cookies(ydl_opts: dict, browser: str) -> None:
    """Safely attach cookiesfrombrowser option if specified."""
    if browser and isinstance(browser, str):
        b = browser.strip().lower()
        if b in SUPPORTED_BROWSERS:
            ydl_opts['cookiesfrombrowser'] = (b,)


def parse_time_to_seconds(time_str) -> float:
    """Convert timestamp strings like '01:15', '00:01:15', '75', '01:15.5' to seconds."""
    if time_str is None:
        return None
    time_str = str(time_str).strip()
    if not time_str:
        return None
    parts = time_str.split(':')
    try:
        if len(parts) == 1:
            return float(parts[0])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except (ValueError, TypeError):
        return None
    return None


def estimate_format_bytes(f: dict, duration: float) -> float:
    """Estimate stream size in bytes from format metadata."""
    if not f:
        return 0
    if f.get('filesize') and f['filesize'] > 0:
        return float(f['filesize'])
    if f.get('filesize_approx') and f['filesize_approx'] > 0:
        return float(f['filesize_approx'])
    bitrate = f.get('tbr') or ((f.get('vbr') or 0) + (f.get('abr') or 0)) or f.get('abr') or 0
    dur = duration or f.get('duration') or 0
    if bitrate > 0 and dur > 0:
        return float((bitrate * 1000 / 8) * dur)
    return 0


def format_filesize_mb(total_bytes: float) -> str:
    """Format byte count into a readable MB/GB string."""
    if not total_bytes or total_bytes <= 0:
        return None
    mb = total_bytes / (1024 * 1024)
    if mb >= 1024:
        gb = mb / 1024
        return f"~{gb:.2f} GB"
    return f"~{mb:.1f} MB"


# ─── YouTube Diagnosis & Self-Update Engine ────────────────────────────────────

def diagnose_error(err_str: str) -> dict:
    """Analyze error message against known YouTube / Google breaking patterns."""
    err_lower = err_str.lower()

    if "cookie" in err_lower:
        return {
            "type": "BROWSER_COOKIE_ERROR",
            "title": "Error al leer cookies del navegador",
            "message": "No se pudieron extraer las cookies de la sesión del navegador seleccionado.",
            "suggestion": "Cierra todas las ventanas de tu navegador para desbloquear el archivo de cookies o selecciona 'Cookies: Ninguna' en la barra superior.",
            "can_auto_fix": False,
            "action": "none"
        }
    if "403" in err_lower or "forbidden" in err_lower:
        return {
            "type": "YOUTUBE_403_FORBIDDEN",
            "title": "Firma o Cifrado de YouTube desactualizado (Error 403 Forbidden)",
            "message": "YouTube ha cambiado los algoritmos de su reproductor web o bloqueó la firma de descarga.",
            "suggestion": "Presiona el botón 'Actualizar yt-dlp' para descargar la última versión de la librería y limpiar la caché.",
            "can_auto_fix": True,
            "action": "update"
        }
    if "sign in" in err_lower or "bot" in err_lower or "confirm you're not a bot" in err_lower:
        return {
            "type": "YOUTUBE_BOT_DETECTION",
            "title": "Verificación anti-bot de YouTube detectada",
            "message": "YouTube detectó múltiples peticiones seguidas o solicitó una prueba de origen (PO Token).",
            "suggestion": "Espera un par de minutos, limpia la caché, selecciona tu navegador en 'Sesión Anti-Bloqueo' o actualiza yt-dlp.",
            "can_auto_fix": True,
            "action": "update"
        }
    if "javascript runtime" in err_lower or "ejs" in err_lower:
        return {
            "type": "MISSING_JS_RUNTIME",
            "title": "Advertencia de motor JavaScript (EJS)",
            "message": "YouTube requiere ejecutar scripts de reto JavaScript para descifrar ciertos formatos.",
            "suggestion": "Actualiza yt-dlp para mantener los métodos de extracción alternativos compatibles.",
            "can_auto_fix": True,
            "action": "update"
        }
    if "429" in err_lower or "too many requests" in err_lower:
        return {
            "type": "YOUTUBE_RATE_LIMIT",
            "title": "Límite de peticiones de YouTube (HTTP 429)",
            "message": "Se han enviado demasiadas consultas seguidas a YouTube desde tu dirección IP.",
            "suggestion": "Espera 2 a 5 minutos antes de intentar descargar nuevamente.",
            "can_auto_fix": False,
            "action": "wait"
        }
    if "unavailable" in err_lower or "private" in err_lower:
        return {
            "type": "VIDEO_UNAVAILABLE",
            "title": "Video privado o no disponible",
            "message": "El contenido no es accesible públicamente o ha sido eliminado de YouTube.",
            "suggestion": "Verifica que el enlace funcione en una pestaña de incógnito de tu navegador.",
            "can_auto_fix": False,
            "action": "none"
        }
    return {
        "type": "GENERIC_ERROR",
        "title": "Error durante la operación",
        "message": err_str,
        "suggestion": "Si el problema persiste, actualiza yt-dlp con el botón de actualización.",
        "can_auto_fix": True,
        "action": "update"
    }


def clear_ytdlp_cache() -> bool:
    """Clear yt-dlp player cache."""
    try:
        with YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
            ydl.cache.remove()
        return True
    except Exception:
        return False


def get_installed_ytdlp_version() -> str:
    """Get the currently installed yt-dlp version in the active Python environment."""
    try:
        import yt_dlp
        return getattr(yt_dlp.version, '__version__', 'Desconocida')
    except Exception:
        return 'No instalada'


def get_latest_pypi_ytdlp_version() -> str:
    """Query PyPI to get the latest available version of yt-dlp."""
    try:
        r = requests.get('https://pypi.org/pypi/yt-dlp/json', headers=REQUEST_HEADERS, timeout=5)
        if r.status_code == 200:
            return r.json().get('info', {}).get('version', '')
    except Exception:
        pass
    return None


def get_portable_ytdlp_version() -> str:
    """Get the yt-dlp version installed in the Portable folder if it exists."""
    for sub in [("software", "python", "python.exe"), ("python", "python.exe")]:
        portable_python = os.path.join(BASE_DIR, "YT Music Downloader Portable", *sub)
        if os.path.exists(portable_python):
            try:
                res = subprocess.run(
                    [portable_python, "-c", "import yt_dlp; print(yt_dlp.version.__version__)"],
                    capture_output=True, text=True, timeout=5
                )
                if res.returncode == 0:
                    return res.stdout.strip()
            except Exception:
                pass
    return None


def _run_system_update(task_id: str):
    """Background task: update yt-dlp in main & portable environments and sync files."""
    q = update_tasks[task_id]

    def log(msg):
        q.put({'status': 'log', 'message': msg})

    try:
        log("=======================================================")
        log("   INICIANDO ACTUALIZACIÓN Y MANTENIMIENTO DEL MOTOR   ")
        log("=======================================================\n")
        log(f"[*] Entorno actual: Python {sys.version.split()[0]}")
        log("[1/4] Actualizando yt-dlp en el sistema principal vía pip...")

        # 1. Update main python environment
        proc = subprocess.Popen(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            clean_line = strip_ansi(line.rstrip())
            if clean_line:
                log(f"  > {clean_line}")
        proc.wait()

        # 2. Check if portable version exists and update it too
        portable_root = os.path.join(BASE_DIR, "YT Music Downloader Portable")
        portable_python = None
        portable_dest = None
        for sub in ["software", ""]:
            py_path = os.path.join(portable_root, sub, "python", "python.exe") if sub else os.path.join(portable_root, "python", "python.exe")
            if os.path.exists(py_path):
                portable_python = py_path
                portable_dest = os.path.join(portable_root, sub) if sub else portable_root
                break

        if portable_python and portable_dest:
            log("\n[2/4] Detectada versión Portable. Actualizando yt-dlp en entorno Portable...")
            proc2 = subprocess.Popen(
                [portable_python, "-m", "pip", "install", "-U", "yt-dlp"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in proc2.stdout:
                clean_line = strip_ansi(line.rstrip())
                if clean_line:
                    log(f"  [Portable] {clean_line}")
            proc2.wait()

            # Synchronize code files to portable
            log("\n[3/4] Sincronizando archivos (app.py, templates, static) con la carpeta Portable...")
            try:
                shutil.copy2(os.path.join(BASE_DIR, "app.py"), os.path.join(portable_dest, "app.py"))
                for folder in ["templates", "static"]:
                    src = os.path.join(BASE_DIR, folder)
                    dst = os.path.join(portable_dest, folder)
                    if os.path.exists(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                log("  [OK] Código sincronizado exitosamente en la versión Portable.")
            except Exception as e:
                log(f"  [AVISO] No se pudo sincronizar archivos a Portable: {e}")
        else:
            log("\n[2/4] Versión Portable no detectada.")
            log("[3/4] Omitiendo sincronización.")

        # 3. Clear cache
        log("\n[4/4] Limpiando la memoria caché de firmas de YouTube...")
        if clear_ytdlp_cache():
            log("  [OK] Caché de firmas y reproductores eliminada.")
        else:
            log("  [AVISO] No fue necesario vaciar caché.")

        new_version = get_installed_ytdlp_version()
        log("\n=======================================================")
        log(f" [✓] ¡ACTUALIZACIÓN COMPLETADA CON ÉXITO!")
        log(f" [✓] Versión instalada ahora: {new_version}")
        log("=======================================================\n")

        q.put({
            'status': 'complete',
            'version': new_version,
            'message': f'yt-dlp actualizado a la versión {new_version}.'
        })

    except Exception as e:
        log(f"\n[ERROR CRÍTICO] Falló la actualización: {e}")
        q.put({'status': 'error', 'message': str(e)})


# ─── Music Functions ────────────────────────────────────────────────────────────

def extract_metadata(url: str, cookies_from_browser: str = None) -> dict:
    """Use yt-dlp to extract metadata from a media URL."""
    clean_url = normalize_media_url(url)
    ydl_opts = {
        **YDL_COMMON,
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'playlist_items': '1',
    }
    apply_browser_cookies(ydl_opts, cookies_from_browser)

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
    except Exception as e:
        if 'cookiesfrombrowser' in ydl_opts:
            ydl_opts.pop('cookiesfrombrowser', None)
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(clean_url, download=False)
        else:
            raise

    if not info:
        raise RuntimeError("No se pudo obtener información del enlace proporcionado.")

    if 'entries' in info and info['entries']:
        first_entry = next((e for e in info['entries'] if e), None)
        if first_entry:
            info = first_entry

    # Clean up the title - often contains " - Topic" or other noise
    title = info.get('track') or info.get('title', 'Unknown')
    artist = info.get('artist') or info.get('uploader') or info.get('channel', 'Unknown')

    # Remove " - Topic" suffix from artist names (YouTube Music auto-channels)
    if artist.endswith(' - Topic'):
        artist = artist[:-8]

    album = info.get('album') or ''
    year = ''
    upload_date = info.get('release_date') or info.get('upload_date') or ''
    if upload_date and len(upload_date) >= 4:
        year = upload_date[:4]

    genre = info.get('genre') or ''
    duration = info.get('duration') or 0

    # Pick the best thumbnail
    thumbnail_url = info.get('thumbnail') or ''
    thumbnails = info.get('thumbnails') or []
    if thumbnails:
        best = max(thumbnails, key=lambda t: t.get('width', 0) * t.get('height', 0))
        thumbnail_url = best.get('url', thumbnail_url)

    # Estimate audio filesize for 160 kbps MP3
    audio_bytes = int((160 * 1000 / 8) * duration) if duration else 0
    filesize_mb = format_filesize_mb(audio_bytes) if audio_bytes > 0 else '~4.5 MB'

    return {
        'title': title,
        'artist': artist,
        'album': album,
        'year': year,
        'genre': genre,
        'duration': duration,
        'thumbnail_url': thumbnail_url,
        'filesize_mb': filesize_mb,
    }


def download_and_tag(url: str, metadata: dict, sponsorblock: bool = False, cookies_from_browser: str = None) -> dict:
    """
    Download audio, convert to 160 kbps MP3, and embed ID3 tags
    including cover art. Supports SponsorBlock for cleaning offtopic audio.
    """
    clean_url = normalize_media_url(url)
    artist = metadata.get('artist', 'Unknown')
    title = metadata.get('title', 'Unknown')
    safe_artist = sanitize_filename(artist)
    safe_title = sanitize_filename(title)
    filename = f"{safe_artist} - {safe_title}"
    output_path = os.path.join(CANCIONES_DIR, filename)

    pps = []
    if sponsorblock:
        pps.append({'key': 'SponsorBlock', 'categories': ['music_offtopic']})
        pps.append({'key': 'ModifyChapters', 'remove_sponsor_segments': ['music_offtopic']})
    pps.append({
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '160',
    })

    ydl_opts = {
        **YDL_COMMON,
        'format': 'bestaudio/best',
        'outtmpl': output_path + '.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'postprocessors': pps,
    }
    apply_browser_cookies(ydl_opts, cookies_from_browser)

    # Download
    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([clean_url])
    except Exception as e:
        if 'cookiesfrombrowser' in ydl_opts:
            ydl_opts.pop('cookiesfrombrowser', None)
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([clean_url])
        else:
            raise

    mp3_path = output_path + '.mp3'

    if not os.path.exists(mp3_path):
        # yt-dlp may have added an extra extension - find the file
        for f in os.listdir(CANCIONES_DIR):
            if f.startswith(filename) and f.endswith('.mp3'):
                mp3_path = os.path.join(CANCIONES_DIR, f)
                break

    if not os.path.exists(mp3_path):
        return {'success': False, 'error': 'No se pudo encontrar el archivo MP3 descargado.'}

    # ── Embed ID3 tags ──────────────────────────────────────────────────────────
    try:
        audio = MP3(mp3_path, ID3=ID3)
    except ID3NoHeaderError:
        audio = MP3(mp3_path)
        audio.add_tags()

    tags = audio.tags
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artist))

    album = metadata.get('album', '')
    if album:
        tags.add(TALB(encoding=3, text=album))

    year = metadata.get('year', '')
    if year:
        tags.add(TDRC(encoding=3, text=year))

    genre = metadata.get('genre', '')
    if genre:
        tags.add(TCON(encoding=3, text=genre))

    # ── Download and embed cover art ────────────────────────────────────────────
    thumbnail_url = metadata.get('thumbnail_url', '')
    if thumbnail_url:
        try:
            resp = requests.get(thumbnail_url, headers=REQUEST_HEADERS, timeout=15)
            if resp.status_code == 200:
                content_type = resp.headers.get('Content-Type', 'image/jpeg')
                tags.add(APIC(
                    encoding=3,
                    mime=content_type,
                    type=3,  # Cover (front)
                    desc='Cover',
                    data=resp.content,
                ))
        except Exception:
            pass  # Non-critical - skip cover if download fails

    audio.save()

    final_filename = os.path.basename(mp3_path)
    return {
        'success': True,
        'filename': final_filename,
        'path': mp3_path,
    }


# ─── Video Functions ────────────────────────────────────────────────────────────

def extract_video_info(url: str, cookies_from_browser: str = None) -> dict:
    """Extract comprehensive video info including available formats, subtitles, estimated sizes, and chapters."""
    clean_url = normalize_media_url(url)
    ydl_opts = {
        **YDL_COMMON,
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'playlist_items': '1',
    }
    apply_browser_cookies(ydl_opts, cookies_from_browser)

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
    except Exception as e:
        if 'cookiesfrombrowser' in ydl_opts:
            ydl_opts.pop('cookiesfrombrowser', None)
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(clean_url, download=False)
        else:
            raise

    if not info:
        raise RuntimeError("No se pudo obtener información del video.")

    if 'entries' in info and info['entries']:
        first_entry = next((e for e in info['entries'] if e), None)
        if first_entry:
            info = first_entry

    duration = info.get('duration') or 0

    # ── Available video qualities with estimated filesize ───────────────────────
    formats = info.get('formats', [])
    audio_formats = [f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') != 'none']
    best_audio = max(audio_formats, key=lambda f: f.get('abr') or f.get('tbr') or f.get('filesize') or 0) if audio_formats else None
    best_audio_bytes = estimate_format_bytes(best_audio, duration)
    if best_audio_bytes == 0 and duration and duration > 0:
        best_audio_bytes = int((128 * 1000 / 8) * float(duration))
    audio_filesize_mb = format_filesize_mb(best_audio_bytes) if best_audio_bytes > 0 else '~4.5 MB'

    heights = set()
    for f in formats:
        h = f.get('height')
        if h and f.get('vcodec', 'none') != 'none':
            heights.add(h)

    quality_labels = {
        4320: '4320p (8K)', 2160: '2160p (4K)', 1440: '1440p (2K)',
        1080: '1080p (Full HD)', 720: '720p (HD)', 480: '480p',
        360: '360p', 240: '240p', 144: '144p',
    }

    qualities = []
    for h in sorted(heights, reverse=True):
        label = quality_labels.get(h, f'{h}p')
        h_formats = [f for f in formats if f.get('height') == h and f.get('vcodec', 'none') != 'none']
        best_v = max(h_formats, key=lambda f: f.get('tbr') or f.get('vbr') or f.get('filesize') or 0) if h_formats else None
        v_bytes = estimate_format_bytes(best_v, duration)
        if best_v and best_v.get('acodec', 'none') == 'none':
            total_est = v_bytes + best_audio_bytes
        else:
            total_est = v_bytes

        filesize_mb = format_filesize_mb(total_est)
        video_only_mb = format_filesize_mb(v_bytes)
        qualities.append({
            'label': label,
            'height': h,
            'filesize_mb': filesize_mb,
            'filesize_bytes': total_est,
            'video_filesize_mb': video_only_mb,
            'video_filesize_bytes': v_bytes,
        })

    # ── Chapters ────────────────────────────────────────────────────────────────
    raw_chapters = info.get('chapters') or []
    has_chapters = bool(raw_chapters)
    chapters_count = len(raw_chapters)
    chapters = [
        {
            'title': c.get('title') or f'Capítulo {i+1}',
            'start_time': c.get('start_time', 0),
            'end_time': c.get('end_time', 0),
        }
        for i, c in enumerate(raw_chapters)
    ]

    # ── Subtitles ───────────────────────────────────────────────────────────────
    manual_subs = info.get('subtitles', {})
    auto_subs = info.get('automatic_captions', {})

    manual_list = sorted(
        [{'code': k, 'name': get_lang_name(k), 'type': 'manual', 'badge': 'Creador'} for k in manual_subs.keys()],
        key=lambda x: x['name']
    )
    auto_list = sorted(
        [{'code': k, 'name': get_lang_name(k), 'type': 'auto', 'badge': 'Auto'} for k in auto_subs.keys()],
        key=lambda x: x['name']
    )

    # ── Audio tracks (dubs / multiple audio languages) ──────────────────────────
    audio_tracks_map = {}
    for f in audio_formats:
        lang = f.get('language')
        if not lang:
            continue
        note = str(f.get('format_note') or '')
        is_original = 'original' in note.lower() or 'default' in note.lower()
        abr = f.get('abr') or 0

        if lang not in audio_tracks_map or (is_original and not audio_tracks_map[lang]['is_original']) or abr > audio_tracks_map[lang].get('abr', 0):
            lang_name = get_lang_name(lang)
            if is_original:
                label = f"{lang_name} (Original)"
            elif 'dubbed' in note.lower() or 'dobl' in note.lower():
                label = f"{lang_name} (Doblaje)"
            else:
                label = lang_name

            audio_tracks_map[lang] = {
                'code': lang,
                'name': lang_name,
                'label': label,
                'note': note,
                'is_original': is_original,
                'abr': abr
            }

    audio_tracks = list(audio_tracks_map.values())
    audio_tracks.sort(key=lambda x: (not x['is_original'], x['name']))
    has_multi_audio = len(audio_tracks) > 1

    # ── Best thumbnail ──────────────────────────────────────────────────────────
    thumbnail_url = info.get('thumbnail', '')
    thumbnails = info.get('thumbnails') or []
    if thumbnails:
        best = max(thumbnails, key=lambda t: t.get('width', 0) * t.get('height', 0))
        thumbnail_url = best.get('url', thumbnail_url)

    # ── Format upload date ──────────────────────────────────────────────────────
    upload_date = info.get('upload_date', '')
    if upload_date and len(upload_date) == 8:
        upload_date = f'{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}'

    return {
        'title': info.get('title', 'Unknown'),
        'channel': info.get('channel') or info.get('uploader', 'Unknown'),
        'description': info.get('description', ''),
        'duration': duration,
        'view_count': info.get('view_count', 0),
        'upload_date': upload_date,
        'thumbnail_url': thumbnail_url,
        'qualities': qualities,
        'audio_filesize_mb': audio_filesize_mb,
        'audio_filesize_bytes': best_audio_bytes,
        'has_chapters': has_chapters,
        'chapters_count': chapters_count,
        'chapters': chapters,
        'has_multi_audio': has_multi_audio,
        'audio_tracks': audio_tracks,
        'subtitles': {
            'manual': manual_list,
            'auto': auto_list,
            'total_count': len(manual_list) + len(auto_list),
        },
    }


def _do_video_download(url, height, download_type, title, task_id,
                       audio_lang=None, embed_subs=False, sub_langs=None,
                       write_auto_sub=False, write_manual_sub=False,
                       start_time=None, end_time=None, editor_mode=False,
                       sponsorblock=False, split_chapters=False,
                       cookies_from_browser=None):
    """Background thread: download video/audio with progress via queue."""
    q = download_tasks[task_id]

    try:
        clean_url = normalize_media_url(url)
        safe_title = sanitize_filename(title) if title else None

        # If no title provided, fetch it
        if not safe_title:
            t_opts = {**YDL_COMMON, 'quiet': True, 'skip_download': True, 'playlist_items': '1'}
            apply_browser_cookies(t_opts, cookies_from_browser)
            with YoutubeDL(t_opts) as ydl:
                info = ydl.extract_info(clean_url, download=False)
                if 'entries' in info and info['entries']:
                    info = info['entries'][0]
            safe_title = sanitize_filename(info.get('title', 'video'))

        target_dir = CANCIONES_DIR if download_type == 'audio_only' else VIDEOS_DIR

        # ── Parse clip range if provided ────────────────────────────────────────
        start_sec = parse_time_to_seconds(start_time)
        end_sec = parse_time_to_seconds(end_time)
        is_clip = (start_sec is not None or end_sec is not None)

        if is_clip:
            safe_title = f"{safe_title}_clip"

        # ── Chapter split setup ─────────────────────────────────────────────────
        is_split = bool(split_chapters and not is_clip)
        chapter_dir = None
        if is_split:
            chapter_dir = os.path.join(target_dir, safe_title)
            os.makedirs(chapter_dir, exist_ok=True)
            output_template = {
                'default': os.path.join(chapter_dir, f'{safe_title}.%(ext)s'),
                'chapter': os.path.join(chapter_dir, '%(section_number)02d - %(section_title)s.%(ext)s'),
            }
        else:
            output_template = os.path.join(target_dir, f'{safe_title}.%(ext)s')

        # ── Build format string ─────────────────────────────────────────────────
        if download_type == 'audio_only':
            if audio_lang:
                format_str = f'bestaudio[language={audio_lang}]/bestaudio[language^={audio_lang[:2]}]/bestaudio/best'
            else:
                format_str = 'bestaudio/best'
        elif download_type == 'video_only':
            format_str = f'bestvideo[height<={height}]'
        else:  # video_audio
            if audio_lang:
                format_str = (
                    f'bestvideo[height<={height}]+bestaudio[language={audio_lang}]/'
                    f'bestvideo[height<={height}]+bestaudio[language^={audio_lang[:2]}]/'
                    f'bestvideo[height<={height}]+bestaudio/'
                    f'best[height<={height}]'
                )
            else:
                format_str = (
                    f'bestvideo[height<={height}]+bestaudio/'
                    f'best[height<={height}]'
                )

        # ── Progress hook ───────────────────────────────────────────────────────
        def progress_hook(d):
            if d['status'] == 'downloading':
                pct_raw = strip_ansi(d.get('_percent_str', '0%')).strip()
                try:
                    pct = float(pct_raw.replace('%', ''))
                except ValueError:
                    pct = 0
                q.put({
                    'status': 'downloading',
                    'percent': pct,
                    'speed': strip_ansi(d.get('_speed_str', '')).strip(),
                    'eta': strip_ansi(d.get('_eta_str', '')).strip(),
                    'downloaded': strip_ansi(d.get('_downloaded_bytes_str', '')).strip(),
                    'total': strip_ansi(
                        d.get('_total_bytes_str', '') or
                        d.get('_total_bytes_estimate_str', '')
                    ).strip(),
                })
            elif d['status'] == 'finished':
                q.put({
                    'status': 'processing',
                    'message': 'Procesando y combinando archivos...',
                })

        # ── Custom logger to capture console output ─────────────────────────────
        class YDLLogger:
            def debug(self, msg):
                q.put({'status': 'log', 'message': strip_ansi(str(msg))})
            def info(self, msg):
                q.put({'status': 'log', 'message': strip_ansi(str(msg))})
            def warning(self, msg):
                q.put({'status': 'log', 'message': f'WARN: {strip_ansi(str(msg))}'})
            def error(self, msg):
                q.put({'status': 'log', 'message': f'ERROR: {strip_ansi(str(msg))}'})

        ydl_opts = {
            **YDL_COMMON,
            'format': format_str,
            'outtmpl': output_template,
            'progress_hooks': [progress_hook],
            'logger': YDLLogger(),
            'postprocessors': [],
        }
        apply_browser_cookies(ydl_opts, cookies_from_browser)

        # ── Editor Compatible Mode (Force H.264 / AAC) ──────────────────────────
        if editor_mode and download_type != 'audio_only':
            ydl_opts['format_sort'] = ['vcodec:h264', 'res', 'acodec:m4a']

        # ── Clip Trimmer ────────────────────────────────────────────────────────
        if is_clip:
            s = start_sec if start_sec is not None else 0
            e = end_sec if end_sec is not None else None
            ydl_opts['download_ranges'] = download_range_func(None, [(s, e)])
            ydl_opts['force_keyframes_at_cuts'] = True

        # ── SponsorBlock Integration ───────────────────────────────────────────
        if sponsorblock:
            ydl_opts['postprocessors'].extend([
                {'key': 'SponsorBlock', 'categories': ['sponsor', 'intro', 'outro', 'selfpromo']},
                {'key': 'ModifyChapters', 'remove_sponsor_segments': ['sponsor', 'intro', 'outro', 'selfpromo']},
            ])

        if download_type == 'audio_only':
            ydl_opts['postprocessors'].append({
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            })
        else:
            ydl_opts['merge_output_format'] = 'mp4'
            if embed_subs and sub_langs:
                ydl_opts['writesubtitles'] = bool(write_manual_sub)
                ydl_opts['writeautomaticsub'] = bool(write_auto_sub)
                ydl_opts['subtitleslangs'] = sub_langs
                ydl_opts['subtitlesformat'] = 'best'
                ydl_opts['postprocessors'].append({
                    'key': 'FFmpegEmbedSubtitle',
                    'already_have_subtitle': False,
                })
                # Prevent 1 bad subtitle stream from failing whole download
                ydl_opts['ignoreerrors'] = True

        if is_split:
            ydl_opts['postprocessors'].append({'key': 'FFmpegSplitChapters'})

        try:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([clean_url])
        except Exception as dl_err:
            if 'cookiesfrombrowser' in ydl_opts:
                q.put({'status': 'log', 'message': '[Aviso] Reintentando descarga sin cookies del navegador...'})
                ydl_opts.pop('cookiesfrombrowser', None)
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.download([clean_url])
            else:
                raise dl_err

        # ── Automatically save full-size thumbnail in MINIATURAS_DIR ───────
        if download_type != 'audio_only':
            try:
                m_opts = {**YDL_COMMON, 'quiet': True, 'skip_download': True, 'playlist_items': '1'}
                apply_browser_cookies(m_opts, cookies_from_browser)
                with YoutubeDL(m_opts) as ydl:
                    vinfo = ydl.extract_info(clean_url, download=False)
                    if 'entries' in vinfo and vinfo['entries']:
                        vinfo = vinfo['entries'][0]
                t_url = vinfo.get('thumbnail')
                t_list = vinfo.get('thumbnails') or []
                if t_list:
                    best_t = max(t_list, key=lambda t: t.get('width', 0) * t.get('height', 0))
                    t_url = best_t.get('url', t_url)
                if t_url:
                    t_resp = requests.get(t_url, headers=REQUEST_HEADERS, timeout=12)
                    if t_resp.status_code == 200:
                        ct = t_resp.headers.get('Content-Type', 'image/jpeg')
                        t_ext = 'png' if 'png' in ct else ('webp' if 'webp' in ct else 'jpg')
                        t_filename = f'{safe_title}_miniatura.{t_ext}'
                        t_filepath = os.path.join(MINIATURAS_DIR, t_filename)
                        with open(t_filepath, 'wb') as tf:
                            tf.write(t_resp.content)
                        q.put({'status': 'log', 'message': f'[Miniatura] Guardada automáticamente en miniaturas: {t_filename}'})
            except Exception as te:
                q.put({'status': 'log', 'message': f'[Aviso] No se pudo guardar miniatura automática: {te}'})

        # ── Find output file or chapter directory ───────────────────────────────
        if is_split:
            final_path = chapter_dir
            final_filename = f"{safe_title} (Capítulos divididos)"
        else:
            expected_ext = '.mp3' if download_type == 'audio_only' else '.mp4'
            final_path = os.path.join(target_dir, f'{safe_title}{expected_ext}')

            if not os.path.exists(final_path):
                for f in os.listdir(target_dir):
                    if f.startswith(safe_title) and not f.endswith('.part'):
                        final_path = os.path.join(target_dir, f)
                        break
            final_filename = os.path.basename(final_path)

        q.put({
            'status': 'complete',
            'filename': final_filename,
            'path': final_path,
            'is_dir': is_split,
        })

    except Exception as e:
        clean_err = strip_ansi(str(e))
        diag = diagnose_error(clean_err)
        q.put({
            'status': 'error',
            'message': clean_err,
            'diagnostic': diag,
        })


# ─── Routes ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ── Music Routes ────────────────────────────────────────────────────────────────

@app.route('/api/metadata', methods=['POST'])
def get_metadata():
    """Extract metadata from a media URL."""
    data = request.get_json(force=True)
    url = data.get('url', '').strip()
    cookies_from_browser = data.get('cookies_from_browser')

    if not url:
        return jsonify({'success': False, 'error': 'No se proporciono un enlace.'}), 400

    try:
        meta = extract_metadata(url, cookies_from_browser=cookies_from_browser)
        return jsonify({'success': True, **meta})
    except Exception as e:
        clean_err = strip_ansi(str(e))
        diag = diagnose_error(clean_err)
        return jsonify({'success': False, 'error': clean_err, 'diagnostic': diag}), 500


@app.route('/api/download', methods=['POST'])
def download():
    """Download the song with the (potentially edited) metadata and SponsorBlock."""
    data = request.get_json(force=True)
    url = data.get('url', '').strip()
    sponsorblock = bool(data.get('sponsorblock', False))
    cookies_from_browser = data.get('cookies_from_browser')

    if not url:
        return jsonify({'success': False, 'error': 'No se proporciono un enlace.'}), 400

    metadata = {
        'title': data.get('title', 'Unknown'),
        'artist': data.get('artist', 'Unknown'),
        'album': data.get('album', ''),
        'year': data.get('year', ''),
        'genre': data.get('genre', ''),
        'thumbnail_url': data.get('thumbnail_url', ''),
    }

    try:
        result = download_and_tag(url, metadata, sponsorblock=sponsorblock, cookies_from_browser=cookies_from_browser)
        return jsonify(result)
    except Exception as e:
        clean_err = strip_ansi(str(e))
        diag = diagnose_error(clean_err)
        return jsonify({'success': False, 'error': clean_err, 'diagnostic': diag}), 500


# ── Video Routes ────────────────────────────────────────────────────────────────

@app.route('/api/video/info', methods=['POST'])
def video_info():
    """Extract comprehensive video info."""
    data = request.get_json(force=True)
    url = data.get('url', '').strip()
    cookies_from_browser = data.get('cookies_from_browser')
    if not url:
        return jsonify({'success': False, 'error': 'No se proporciono un enlace.'}), 400
    try:
        info = extract_video_info(url, cookies_from_browser=cookies_from_browser)
        return jsonify({'success': True, **info})
    except Exception as e:
        clean_err = strip_ansi(str(e))
        diag = diagnose_error(clean_err)
        return jsonify({'success': False, 'error': clean_err, 'diagnostic': diag}), 500


@app.route('/api/video/download', methods=['POST'])
def video_download():
    """Start a video download in a background thread. Returns task_id for SSE progress."""
    data = request.get_json(force=True)
    url = data.get('url', '').strip()
    height = int(data.get('height', 1080))
    download_type = data.get('type', 'video_audio')
    title = data.get('title', '')
    audio_lang = data.get('audio_lang')
    embed_subs = bool(data.get('embed_subs', False))
    sub_langs = data.get('sub_langs', [])
    write_auto_sub = bool(data.get('write_auto_sub', False))
    write_manual_sub = bool(data.get('write_manual_sub', False))

    # New options
    start_time = data.get('start_time')
    end_time = data.get('end_time')
    editor_mode = bool(data.get('editor_mode', False))
    sponsorblock = bool(data.get('sponsorblock', False))
    split_chapters = bool(data.get('split_chapters', False))
    cookies_from_browser = data.get('cookies_from_browser')

    if not url:
        return jsonify({'success': False, 'error': 'No se proporciono un enlace.'}), 400

    task_id = str(uuid.uuid4())
    download_tasks[task_id] = queue.Queue()

    thread = threading.Thread(
        target=_do_video_download,
        args=(url, height, download_type, title, task_id),
        kwargs={
            'audio_lang': audio_lang,
            'embed_subs': embed_subs,
            'sub_langs': sub_langs,
            'write_auto_sub': write_auto_sub,
            'write_manual_sub': write_manual_sub,
            'start_time': start_time,
            'end_time': end_time,
            'editor_mode': editor_mode,
            'sponsorblock': sponsorblock,
            'split_chapters': split_chapters,
            'cookies_from_browser': cookies_from_browser,
        },
        daemon=True,
    )
    thread.start()

    return jsonify({'success': True, 'task_id': task_id})


@app.route('/api/video/progress/<task_id>')
def video_progress(task_id):
    """SSE endpoint: stream download progress events to the browser."""
    def event_stream():
        q = download_tasks.get(task_id)
        if not q:
            yield f'data: {json.dumps({"status": "error", "message": "Tarea no encontrada"})}\n\n'
            return
        while True:
            try:
                data = q.get(timeout=30)
                yield f'data: {json.dumps(data, ensure_ascii=False)}\n\n'
                if data.get('status') in ('complete', 'error'):
                    download_tasks.pop(task_id, None)
                    break
            except queue.Empty:
                # Keep-alive heartbeat
                yield f'data: {json.dumps({"status": "heartbeat"})}\n\n'

    return Response(
        event_stream(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/video/thumbnail', methods=['POST'])
def video_thumbnail():
    """Download the full-size thumbnail and save it to the videos folder."""
    data = request.get_json(force=True)
    thumb_url = data.get('thumbnail_url', '').strip()
    title = sanitize_filename(data.get('title', 'thumbnail'))

    if not thumb_url:
        return jsonify({'success': False, 'error': 'No se proporciono URL de miniatura.'}), 400

    try:
        resp = requests.get(thumb_url, headers=REQUEST_HEADERS, timeout=15)
        if resp.status_code != 200:
            return jsonify({'success': False, 'error': 'No se pudo descargar la miniatura.'}), 500

        ct = resp.headers.get('Content-Type', 'image/jpeg')
        ext = 'png' if 'png' in ct else ('webp' if 'webp' in ct else 'jpg')
        filename = f'{title}_miniatura.{ext}'
        filepath = os.path.join(MINIATURAS_DIR, filename)

        with open(filepath, 'wb') as f:
            f.write(resp.content)

        return jsonify({
            'success': True,
            'filename': filename,
            'path': filepath,
            'download_url': f'/api/download-file/miniaturas/{filename}'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/video/subtitles', methods=['POST'])
def video_subtitles():
    """Download subtitles/transcript, convert to plain text and save to solo subtitulos folder."""
    data = request.get_json(force=True)
    url = data.get('url', '').strip()
    lang = data.get('lang', 'en')
    sub_type = data.get('sub_type', 'auto')  # 'manual' or 'auto'
    cookies_from_browser = data.get('cookies_from_browser')

    if not url:
        return jsonify({'success': False, 'error': 'No se proporciono un enlace.'}), 400

    try:
        clean_url = normalize_media_url(url)
        ydl_opts = {
            **YDL_COMMON,
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'playlist_items': '1',
        }
        apply_browser_cookies(ydl_opts, cookies_from_browser)

        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(clean_url, download=False)
        except Exception as e:
            if 'cookiesfrombrowser' in ydl_opts:
                ydl_opts.pop('cookiesfrombrowser', None)
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(clean_url, download=False)
            else:
                raise

        title = sanitize_filename(info.get('title', 'video'))

        if sub_type == 'manual':
            subs = info.get('subtitles', {})
        else:
            subs = info.get('automatic_captions', {})

        if lang not in subs:
            return jsonify({
                'success': False,
                'error': f'No se encontraron subtitulos en "{get_lang_name(lang)}".',
            })

        # Find best subtitle format URL (prefer json3 for clean parsing)
        sub_url = None
        sub_ext = None
        for fmt in subs[lang]:
            if fmt.get('ext') == 'json3':
                sub_url = fmt.get('url')
                sub_ext = 'json3'
                break
        if not sub_url:
            for fmt in subs[lang]:
                if fmt.get('ext') == 'vtt':
                    sub_url = fmt.get('url')
                    sub_ext = 'vtt'
                    break
        if not sub_url and subs[lang]:
            sub_url = subs[lang][0].get('url')
            sub_ext = subs[lang][0].get('ext', 'unknown')

        if not sub_url:
            return jsonify({
                'success': False,
                'error': 'No se pudo obtener la URL del subtitulo.',
            })

        resp = requests.get(sub_url, headers=REQUEST_HEADERS, timeout=30)
        if resp.status_code != 200:
            return jsonify({
                'success': False,
                'error': 'No se pudo descargar el subtitulo.',
            })

        content = resp.text

        # ── Parse to plain text ─────────────────────────────────────────────────
        if sub_ext == 'json3':
            try:
                sub_data = json.loads(content)
                events = sub_data.get('events', [])
                lines = []
                for event in events:
                    segs = event.get('segs', [])
                    text = ''.join(s.get('utf8', '') for s in segs).strip()
                    if text and text != '\n':
                        lines.append(text)
                plain_text = '\n'.join(lines)
            except (json.JSONDecodeError, KeyError):
                plain_text = content
        else:
            # Strip VTT / SRT formatting
            plain_text = re.sub(
                r'\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}',
                '', content,
            )
            plain_text = re.sub(r'WEBVTT.*?\n\n', '', plain_text, flags=re.DOTALL)
            plain_text = re.sub(r'<[^>]+>', '', plain_text)
            plain_text = re.sub(r'\n{3,}', '\n\n', plain_text).strip()

        lang_name = get_lang_name(lang)
        filename = f'{title}_subtitulos_{lang}.txt'
        filepath = os.path.join(SUBTITULOS_DIR, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(plain_text)

        return jsonify({
            'success': True,
            'filename': filename,
            'path': filepath,
            'download_url': f'/api/download-file/subtitulos/{filename}',
            'preview': plain_text[:3000],
            'total_length': len(plain_text),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── System & File Routes ────────────────────────────────────────────────────────

def _launch_explorer(folder_path):
    """Reliably open a directory in Windows Explorer across different Windows environments."""
    folder_path = os.path.normpath(os.path.abspath(folder_path))
    os.makedirs(folder_path, exist_ok=True)
    
    explorer_exe = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'explorer.exe')
    if not os.path.exists(explorer_exe):
        explorer_exe = 'explorer.exe'
    
    # Method 1: Direct invocation of explorer.exe with the normalized path
    try:
        subprocess.Popen([explorer_exe, folder_path])
        return True, folder_path
    except Exception as e1:
        pass
        
    # Method 2: Force new window switch /n,
    try:
        subprocess.Popen([explorer_exe, '/n,', folder_path])
        return True, folder_path
    except Exception as e2:
        pass
        
    # Method 3: Shell invocation
    try:
        subprocess.Popen(f'explorer.exe "{folder_path}"', shell=True)
        return True, folder_path
    except Exception as e3:
        pass

    # Method 4: os.startfile fallback
    try:
        os.startfile(folder_path)
        return True, folder_path
    except Exception as e4:
        raise RuntimeError(f"No se pudo abrir el explorador de Windows: {e4}")


@app.route('/api/open-folder', methods=['POST'])
def open_folder():
    """Open canciones, videos, solo subtitulos or miniaturas folder in Windows Explorer."""
    data = request.get_json(force=True) if request.data else {}
    target = data.get('folder', 'videos')
    folder_map = {
        'canciones': CANCIONES_DIR,
        'videos': VIDEOS_DIR,
        'subtitulos': SUBTITULOS_DIR,
        'miniaturas': MINIATURAS_DIR,
    }
    raw_path = folder_map.get(target, VIDEOS_DIR)
    try:
        ok, resolved_path = _launch_explorer(raw_path)
        return jsonify({'success': True, 'path': resolved_path})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/download-file/<folder>/<path:filename>')
def serve_file(folder, filename):
    """Download or view a generated file directly from the browser."""
    from flask import send_from_directory
    folder_map = {
        'canciones': CANCIONES_DIR,
        'videos': VIDEOS_DIR,
        'subtitulos': SUBTITULOS_DIR,
        'miniaturas': MINIATURAS_DIR,
    }
    target_dir = folder_map.get(folder, VIDEOS_DIR)
    return send_from_directory(target_dir, filename, as_attachment=True)


# ── System Maintenance & Update Routes ───────────────────────────────────────────

@app.route('/api/system/status')
def system_status():
    """Get installed and remote yt-dlp versions and health status."""
    installed = get_installed_ytdlp_version()
    latest = get_latest_pypi_ytdlp_version()
    portable_ver = get_portable_ytdlp_version()

    update_available = False
    if latest and installed and installed != 'Desconocida':
        # Normalize versions (e.g., 2026.08.19 vs 2026.8.19)
        def norm(v):
            return [int(x) for x in re.findall(r'\d+', str(v))]
        try:
            update_available = norm(latest) > norm(installed)
        except Exception:
            update_available = (installed != latest)

    return jsonify({
        'success': True,
        'installed_version': installed,
        'latest_version': latest or installed,
        'update_available': update_available,
        'has_portable': portable_ver is not None,
        'portable_version': portable_ver,
    })


@app.route('/api/system/update', methods=['POST'])
def system_update():
    """Trigger system update in background thread."""
    task_id = str(uuid.uuid4())
    update_tasks[task_id] = queue.Queue()

    thread = threading.Thread(
        target=_run_system_update,
        args=(task_id,),
        daemon=True,
    )
    thread.start()

    return jsonify({'success': True, 'task_id': task_id})


@app.route('/api/system/update-progress/<task_id>')
def system_update_progress(task_id):
    """SSE endpoint to stream update logs."""
    def event_stream():
        q = update_tasks.get(task_id)
        if not q:
            yield f'data: {json.dumps({"status": "error", "message": "Tarea de actualizacion no encontrada"})}\n\n'
            return
        while True:
            try:
                data = q.get(timeout=30)
                yield f'data: {json.dumps(data, ensure_ascii=False)}\n\n'
                if data.get('status') in ('complete', 'error'):
                    update_tasks.pop(task_id, None)
                    break
            except queue.Empty:
                yield f'data: {json.dumps({"status": "heartbeat"})}\n\n'

    return Response(
        event_stream(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/system/clear-cache', methods=['POST'])
def system_clear_cache():
    """Clear yt-dlp cache directly."""
    ok = clear_ytdlp_cache()
    if ok:
        return jsonify({'success': True, 'message': 'Caché de YouTube eliminada correctamente.'})
    return jsonify({'success': False, 'error': 'No se pudo vaciar la caché.'}), 500


@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    """Shut down the Flask server gracefully."""
    def stop_server():
        import time
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=stop_server).start()
    return jsonify({'success': True, 'message': 'Servidor apagado.'})


# ─── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import webbrowser
    print(f"\n  [*] YouTube Downloader")
    print(f"  [>] Canciones se guardaran en: {CANCIONES_DIR}")
    print(f"  [>] Videos se guardaran en:    {VIDEOS_DIR}")
    print(f"  [>] Abriendo http://localhost:5000 ...\n")
    # Open browser after a short delay
    threading.Timer(1.5, lambda: webbrowser.open('http://localhost:5000')).start()
    app.run(debug=False, port=5000)
