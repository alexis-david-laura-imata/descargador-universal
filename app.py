import os
import re
import glob
import html
import time
import tempfile
import sys
import json
import uuid
import signal
import threading
import queue
import subprocess
import shutil
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import requests
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify, Response
from yt_dlp import YoutubeDL
from yt_dlp.utils import download_range_func
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, APIC, ID3NoHeaderError

# ─── Configuration ──────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Detección rigurosa de entorno Portable vs Local
is_portable = (
    os.path.basename(BASE_DIR).lower() == "software" or
    os.path.exists(os.path.join(BASE_DIR, "python", "python.exe"))
)

if is_portable:
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

# Whitelist dictionary for secure folder access and path traversal defense
ALLOWED_FOLDERS = {
    'canciones': CANCIONES_DIR,
    'videos': VIDEOS_DIR,
    'subtitulos': SUBTITULOS_DIR,
    'solo subtitulos': SUBTITULOS_DIR,
    'miniaturas': MINIATURAS_DIR,
}

# Ensure FFmpeg and Deno are in PATH (checks local portable folder, winget links, etc.)
local_ffmpeg = os.path.join(BASE_DIR, "ffmpeg")
local_deno = os.path.join(BASE_DIR, "deno")
portable_deno = os.path.join(ROOT_DIR, "YT Music Downloader Portable", "software", "deno")
user_deno = os.path.expandvars(r"%USERPROFILE%\.deno\bin")
winget_links = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links")
for tool_dir in [local_ffmpeg, local_deno, portable_deno, user_deno, winget_links]:
    if os.path.exists(tool_dir) and tool_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = tool_dir + os.pathsep + os.environ.get("PATH", "")

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static")
)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# App Version, OTA Remote Updates & Bug Reporting
APP_VERSION = "2.0.1"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/alexis-david-laura-imata/descargador-universal/main"
def _load_bug_report_webhook() -> str:
    """Webhook de Discord para reportes: variable de entorno o archivo local bug_webhook.txt (no se sube a GitHub)."""
    env_url = os.environ.get("BUG_REPORT_WEBHOOK", "").strip()
    if env_url:
        return env_url
    for base in (BASE_DIR, ROOT_DIR):
        path = os.path.join(base, "bug_webhook.txt")
        try:
            with open(path, "r", encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value
        except OSError:
            pass
    return ""


BUG_REPORT_WEBHOOK = _load_bug_report_webhook()

# Common yt-dlp options with network resilience, anti-throttling and timeout guards
YDL_COMMON = {
    'extractor_args': {'youtube': ['player_client=default,ios']},
    'noplaylist': True,
    'socket_timeout': 20,
    'retries': 10,
    'fragment_retries': 10,
    'retry_sleep_functions': {'http': lambda n: 2, 'fragment': lambda n: 2},
    'http_chunk_size': 10485760,  # 10MB chunks to prevent YouTube CDN throttling and stalls
    'buffersize': 1024 * 64,
    'remote_components': {'ejs:github'},  # Allow yt-dlp to download JS challenge solver scripts from GitHub (required since yt-dlp 2026+)
}
SUPPORTED_BROWSERS = {'chrome', 'edge', 'brave', 'firefox', 'opera', 'vivaldi', 'chromium', 'safari'}
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
}

# Progress tracking for video downloads and system updates (thread-safe)
tasks_lock = threading.Lock()
download_tasks = {}  # task_id -> queue.Queue
update_tasks = {}    # task_id -> queue.Queue

# ─── SQLite History Database & ThreadPool Queue Manager ─────────────────────────
DB_PATH = os.path.join(ROOT_DIR, "history.db")
db_lock = threading.Lock()


def format_bytes(num_bytes) -> str:
    """Convierte bytes numéricos a formato legible por humanos (B, KB, MB, GB)."""
    try:
        n = float(num_bytes)
        if n <= 0:
            return "0 MB"
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if n < 1024.0:
                return f"{n:.1f} {unit}" if unit != 'B' else f"{int(n)} B"
            n /= 1024.0
        return f"{n:.1f} PB"
    except Exception:
        return "0 MB"


def init_db():
    """Inicializa la tabla downloads en history.db con control estricto de concurrencia."""
    try:
        with db_lock:
            with sqlite3.connect(DB_PATH, timeout=20) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS downloads (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT,
                        title TEXT,
                        type TEXT,
                        status TEXT,
                        filepath TEXT,
                        filesize TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloads_timestamp ON downloads(timestamp DESC)")
                # Limpiar cualquier descarga que haya quedado huérfana o interrumpida por cierre forzado/reinicio
                cursor.execute("UPDATE downloads SET status = 'failed' WHERE status IN ('downloading', 'pending')")
                conn.commit()
    except Exception as e:
        print(f"[ERROR] No se pudo inicializar history.db: {e}")


def db_add_download(url: str, title: str, type_: str, status: str = 'pending', filepath: str = '', filesize: str = '') -> int:
    """Inserta un nuevo registro de descarga en history.db y retorna su ID."""
    try:
        with db_lock:
            with sqlite3.connect(DB_PATH, timeout=20) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO downloads (url, title, type, status, filepath, filesize)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (url, title, type_, status, filepath, filesize))
                conn.commit()
                return cursor.lastrowid
    except Exception as e:
        print(f"[ERROR] Error al insertar en history.db: {e}")
        return 0


def db_update_download(download_id: int, status: str, filepath: str = None, filesize: str = None, title: str = None):
    """Actualiza el estado, ruta, peso o título de una descarga existente en history.db."""
    if not download_id:
        return
    try:
        with db_lock:
            with sqlite3.connect(DB_PATH, timeout=20) as conn:
                cursor = conn.cursor()
                updates = ["status = ?"]
                params = [status]
                if filepath is not None:
                    updates.append("filepath = ?")
                    params.append(filepath)
                if filesize is not None:
                    updates.append("filesize = ?")
                    params.append(filesize)
                if title is not None:
                    updates.append("title = ?")
                    params.append(title)
                params.append(download_id)
                query = f"UPDATE downloads SET {', '.join(updates)} WHERE id = ?"
                cursor.execute(query, params)
                conn.commit()
    except Exception as e:
        print(f"[ERROR] Error al actualizar history.db: {e}")


def db_get_history(limit: int = 100) -> list:
    """Obtiene el historial de descargas ordenado de forma descendente por fecha."""
    try:
        with db_lock:
            with sqlite3.connect(DB_PATH, timeout=20) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, url, title, type, status, filepath, filesize, timestamp
                    FROM downloads
                    ORDER BY id DESC
                    LIMIT ?
                """, (limit,))
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
    except Exception as e:
        print(f"[ERROR] Error al consultar history.db: {e}")
        return []


def db_delete_download(download_id: int) -> bool:
    """Elimina un registro individual de history.db."""
    try:
        with db_lock:
            with sqlite3.connect(DB_PATH, timeout=20) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
                conn.commit()
                return cursor.rowcount > 0
    except Exception as e:
        print(f"[ERROR] Error al eliminar de history.db: {e}")
        return False


def db_clear_history() -> bool:
    """Vacía por completo el historial de descargas en history.db."""
    try:
        with db_lock:
            with sqlite3.connect(DB_PATH, timeout=20) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM downloads")
                conn.commit()
                return True
    except Exception as e:
        print(f"[ERROR] Error al vaciar history.db: {e}")
        return False


class DownloadCancelled(Exception):
    """Excepción lanzada cuando el usuario cancela una descarga desde el gestor de colas."""
    pass


class DownloadQueueManager:
    """
    Gestor de colas con concurrencia controlada (ThreadPoolExecutor) e integración con SQLite.
    Permite procesar múltiples descargas sin saturar la red, CPU ni provocar baneos de IP.
    """
    def __init__(self, default_workers=3):
        self.max_workers = max(1, min(5, int(default_workers)))
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="DLWorker")
        self.tasks = {}           # task_id -> dict con estado en memoria
        self.pending_queue = []   # [task_id, ...] orden de espera
        self.active_tasks = set() # {task_id, ...} tareas actualmente en descarga
        self.cancel_flags = set() # {task_id, ...} tareas marcadas para abortar

    def set_max_workers(self, count):
        with self.lock:
            self.max_workers = max(1, min(5, int(count)))
            self._dispatch_locked()

    def add_task(self, item_type: str, url: str, title: str, options: dict) -> tuple:
        with self.lock:
            task_id = str(uuid.uuid4())
            display_title = title or 'Extrayendo información...'
            db_id = db_add_download(
                url=url,
                title=display_title,
                type_=item_type,
                status='pending'
            )
            item = {
                'task_id': task_id,
                'db_id': db_id,
                'type': item_type,
                'url': url,
                'title': display_title,
                'options': options or {},
                'status': 'pending',
                'progress': 0.0,
                'speed': '',
                'eta': '',
                'downloaded': '',
                'total': '',
                'filepath': '',
                'filesize': '',
                'error': '',
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
            self.tasks[task_id] = item
            self.pending_queue.append(task_id)

            with tasks_lock:
                if task_id not in download_tasks:
                    download_tasks[task_id] = queue.Queue()

            self._dispatch_locked()
            position = (self.pending_queue.index(task_id) + 1) if task_id in self.pending_queue else 0
            return task_id, position

    def update_task_progress(self, task_id: str, pct: float, speed: str, eta: str, downloaded: str, total: str):
        with self.lock:
            if task_id in self.tasks:
                it = self.tasks[task_id]
                it['progress'] = pct
                it['speed'] = speed
                it['eta'] = eta
                it['downloaded'] = downloaded
                it['total'] = total

    def update_task_completion(self, task_id: str, filepath: str, filesize: str, title: str = None):
        with self.lock:
            if task_id in self.tasks:
                it = self.tasks[task_id]
                it['status'] = 'completed'
                it['progress'] = 100.0
                it['filepath'] = filepath
                it['filesize'] = filesize
                if title:
                    it['title'] = title
                db_update_download(it['db_id'], status='completed', filepath=filepath, filesize=filesize, title=title)

    def get_task(self, task_id: str) -> dict:
        with self.lock:
            it = self.tasks.get(task_id)
            return dict(it) if it else None

    def cancel_task(self, task_id: str) -> tuple:
        with self.lock:
            if task_id not in self.tasks:
                return False, "Tarea no encontrada en la cola"

            it = self.tasks[task_id]
            self.cancel_flags.add(task_id)

            if task_id in self.pending_queue:
                self.pending_queue.remove(task_id)
                it['status'] = 'cancelled'
                db_update_download(it['db_id'], status='cancelled')
                with tasks_lock:
                    q = download_tasks.get(task_id)
                    if q:
                        q.put({'status': 'error', 'message': 'Descarga cancelada por el usuario'})
                return True, "Descarga en espera retirada de la cola"

            if task_id in self.active_tasks:
                return True, "Cancelando descarga activa en curso..."

            return False, "La descarga ya finalizó o no puede cancelarse"

    def _dispatch_locked(self):
        while len(self.active_tasks) < self.max_workers and self.pending_queue:
            next_task_id = self.pending_queue.pop(0)
            if next_task_id in self.cancel_flags:
                self.tasks[next_task_id]['status'] = 'cancelled'
                db_update_download(self.tasks[next_task_id]['db_id'], status='cancelled')
                continue
            self.active_tasks.add(next_task_id)
            self.tasks[next_task_id]['status'] = 'downloading'
            db_update_download(self.tasks[next_task_id]['db_id'], status='downloading')
            self.executor.submit(self._worker_run, next_task_id)

    def _worker_run(self, task_id: str):
        item = self.tasks.get(task_id)
        if not item:
            return

        if task_id in self.cancel_flags:
            with self.lock:
                item['status'] = 'cancelled'
                self.active_tasks.discard(task_id)
                db_update_download(item['db_id'], status='cancelled')
                self._dispatch_locked()
            return

        try:
            if item['type'] == 'music':
                opts = item['options']
                res = download_and_tag(
                    url=item['url'],
                    metadata=opts.get('metadata', {}),
                    sponsorblock=opts.get('sponsorblock', False),
                    cookies_from_browser=opts.get('cookies_from_browser'),
                    task_id=task_id
                )
                final_path = res.get('path', '')
                fsize = format_bytes(os.path.getsize(final_path)) if (final_path and os.path.exists(final_path)) else ''
                final_title = res.get('filename', item['title'])
                self.update_task_completion(task_id, final_path, fsize, final_title)
            else:
                opts = item['options']
                _do_video_download(
                    url=item['url'],
                    height=opts.get('height', 1080),
                    download_type=opts.get('download_type', 'video_audio'),
                    title=opts.get('title', ''),
                    task_id=task_id,
                    audio_lang=opts.get('audio_lang'),
                    embed_subs=opts.get('embed_subs', False),
                    sub_langs=opts.get('sub_langs', []),
                    write_auto_sub=opts.get('write_auto_sub', False),
                    write_manual_sub=opts.get('write_manual_sub', False),
                    start_time=opts.get('start_time'),
                    end_time=opts.get('end_time'),
                    editor_mode=opts.get('editor_mode', False),
                    sponsorblock=opts.get('sponsorblock', False),
                    split_chapters=opts.get('split_chapters', False),
                    cookies_from_browser=opts.get('cookies_from_browser'),
                )
        except DownloadCancelled:
            with self.lock:
                item['status'] = 'cancelled'
                db_update_download(item['db_id'], status='cancelled')
                with tasks_lock:
                    q = download_tasks.get(task_id)
                    if q:
                        q.put({'status': 'error', 'message': 'Descarga cancelada por el usuario'})
        except Exception as e:
            clean_err = strip_ansi(str(e))
            with self.lock:
                item['status'] = 'failed'
                item['error'] = clean_err
                db_update_download(item['db_id'], status='failed')
                with tasks_lock:
                    q = download_tasks.get(task_id)
                    if q:
                        diag = diagnose_error(clean_err)
                        q.put({'status': 'error', 'message': clean_err, 'diagnostic': diag})
        finally:
            with self.lock:
                self.active_tasks.discard(task_id)
                self.cancel_flags.discard(task_id)
                self._dispatch_locked()

    def get_status(self) -> dict:
        with self.lock:
            all_items = list(self.tasks.values())
            recent = all_items[-50:]
            recent.reverse()
            return {
                'success': True,
                'max_workers': self.max_workers,
                'active_count': len(self.active_tasks),
                'pending_count': len(self.pending_queue),
                'queue': [
                    {
                        'task_id': it['task_id'],
                        'title': it['title'],
                        'type': it['type'],
                        'url': it['url'],
                        'status': it['status'],
                        'progress': it.get('progress', 0.0),
                        'speed': it.get('speed', ''),
                        'eta': it.get('eta', ''),
                        'downloaded': it.get('downloaded', ''),
                        'total': it.get('total', ''),
                        'filesize': it.get('filesize', ''),
                        'filepath': it.get('filepath', ''),
                        'error': it.get('error', ''),
                        'created_at': it.get('created_at', ''),
                    }
                    for it in recent
                ]
            }

queue_mgr = DownloadQueueManager(default_workers=3)
init_db()

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

WINDOWS_RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
    'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'
}


def sanitize_filename(name: str) -> str:
    """
    Strict sanitization of Windows filenames:
    - Removes characters forbidden in Windows (<>:"/\\|?*)
    - Strips ASCII control characters (0-31)
    - Strips trailing dots and whitespace
    - Prevents Windows reserved device names (CON, PRN, AUX, NUL, COMx, LPTx)
    - Truncates to max 110 characters safely without breaking words if possible
    """
    if not name:
        return 'archivo'
    # 1. Remove illegal characters
    name = re.sub(r'[<>:"/\\|?*]', '', str(name))
    # 2. Remove ASCII control chars (0-31)
    name = re.sub(r'[\x00-\x1f]', '', name)
    # 3. Strip trailing dots and spaces
    name = name.strip('. ')
    # 4. Handle empty after stripping
    if not name:
        return 'archivo'
    # 5. Prevent Windows reserved filenames
    base_stem = name.split('.')[0].upper()
    if base_stem in WINDOWS_RESERVED_NAMES or name.upper() in WINDOWS_RESERVED_NAMES:
        name = f"{name}_"
    # 6. Truncate safely to max 110 chars
    if len(name) > 110:
        truncated = name[:110]
        last_space = truncated.rfind(' ')
        if last_space > 80:
            name = truncated[:last_space].rstrip('. ')
        else:
            name = truncated.rstrip('. ')
    return name or 'archivo'


def convert_image_to_jpeg(img_bytes: bytes) -> tuple:
    """
    Ensure cover image buffer is JPEG format for 100% ID3v2 APIC compliance in Windows/iTunes/Car players.
    Converts WebP, AVIF or other formats using FFmpeg in-memory.
    """
    if not img_bytes:
        return None, None
    # Check JPEG magic bytes (\xff\xd8\xff)
    if img_bytes.startswith(b'\xff\xd8\xff'):
        return img_bytes, 'image/jpeg'
    # Attempt in-memory FFmpeg conversion to clean MJPEG
    try:
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        res = subprocess.run(
            ['ffmpeg', '-y', '-i', 'pipe:0', '-f', 'image2', '-vcodec', 'mjpeg', '-q:v', '2', 'pipe:1'],
            input=img_bytes,
            capture_output=True,
            timeout=12,
            creationflags=creation_flags
        )
        if res.returncode == 0 and res.stdout:
            return res.stdout, 'image/jpeg'
    except Exception:
        pass
    # If already PNG and ffmpeg conversion is unavailable, fallback to image/png
    if img_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        return img_bytes, 'image/png'
    return img_bytes, 'image/jpeg'


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


def get_app_version() -> str:
    """Get the current application version from version.json or APP_VERSION."""
    for base in [BASE_DIR, ROOT_DIR]:
        ver_path = os.path.join(base, "version.json")
        if os.path.exists(ver_path):
            try:
                with open(ver_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    ver = data.get("version")
                    if ver:
                        return str(ver).strip()
            except Exception:
                pass
    return APP_VERSION


def get_portable_ytdlp_version() -> str:
    """Get the yt-dlp version installed in the Portable environment or folder."""
    # If running inside the portable environment itself
    if os.path.basename(BASE_DIR).lower() == "software" or "portable" in sys.executable.lower() or os.path.exists(os.path.join(BASE_DIR, "python", "python.exe")):
        return get_installed_ytdlp_version()

    # Otherwise look into neighbor portable directory
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


def is_newer_version(remote: str, current: str) -> bool:
    """Compare two semantic version strings like '2.0.1' and '2.0.0'."""
    try:
        r_parts = [int(p) for p in re.findall(r'\d+', str(remote))]
        c_parts = [int(p) for p in re.findall(r'\d+', str(current))]
        max_len = max(len(r_parts), len(c_parts))
        r_parts.extend([0] * (max_len - len(r_parts)))
        c_parts.extend([0] * (max_len - len(c_parts)))
        return r_parts > c_parts
    except Exception:
        return str(remote).strip() != str(current).strip()


def check_remote_app_update() -> dict:
    """Check GitHub repository for a newer app version."""
    current_ver = get_app_version()
    if not GITHUB_RAW_BASE or "PEGA_AQUI" in GITHUB_RAW_BASE:
        return {"update_available": False, "current_version": current_ver, "latest_version": current_ver, "changelog": ""}
    try:
        ver_url = f"{GITHUB_RAW_BASE.rstrip('/')}/version.json"
        res = requests.get(ver_url, headers=REQUEST_HEADERS, timeout=5)
        if res.status_code == 200:
            data = res.json()
            remote_ver = str(data.get("version", "")).strip()
            changelog = str(data.get("changelog", "")).strip()
            update_available = bool(remote_ver and is_newer_version(remote_ver, current_ver))
            return {
                "update_available": update_available,
                "current_version": current_ver,
                "latest_version": remote_ver or current_ver,
                "changelog": changelog
            }
    except Exception:
        pass
    return {"update_available": False, "current_version": current_ver, "latest_version": current_ver, "changelog": ""}


def _run_system_update(task_id: str):
    """Background task: update yt-dlp in main & portable environments, check OTA GitHub app update, and sync files."""
    q = update_tasks[task_id]

    def log(msg):
        q.put({'status': 'log', 'message': msg})

    try:
        log("=======================================================")
        log("   INICIANDO ACTUALIZACIÓN Y MANTENIMIENTO DEL MOTOR   ")
        log("=======================================================\n")
        log(f"[*] Entorno actual: Python {sys.version.split()[0]} | App: v{APP_VERSION}")

        # 1. Check and download OTA App Updates from GitHub if available
        log("[1/6] Comprobando actualizaciones remotas de la aplicación en GitHub...")
        ota_updated = False
        current_app_ver = get_app_version()
        if GITHUB_RAW_BASE and "PEGA_AQUI" not in GITHUB_RAW_BASE:
            try:
                ver_url = f"{GITHUB_RAW_BASE.rstrip('/')}/version.json"
                r = requests.get(ver_url, headers=REQUEST_HEADERS, timeout=6)
                if r.status_code == 200:
                    r_data = r.json()
                    remote_ver = str(r_data.get("version", "")).strip()
                    remote_changelog = str(r_data.get("changelog", "")).strip()
                    log(f"  > Versión instalada: v{current_app_ver} | Versión en GitHub: v{remote_ver}")
                    if remote_ver and is_newer_version(remote_ver, current_app_ver):
                        log(f"  [!] ¡Nueva versión de la app encontrada: v{remote_ver}!")
                        if remote_changelog:
                            log(f"  > Notas: {remote_changelog}")
                        log("  [*] Descargando código actualizado desde GitHub...")

                        files_to_sync = [
                            ("app.py", "app.py"),
                            ("templates/index.html", "templates/index.html"),
                            ("static/style.css", "static/style.css"),
                            ("version.json", "version.json"),
                        ]
                        for rel_path, dest_rel in files_to_sync:
                            file_url = f"{GITHUB_RAW_BASE.rstrip('/')}/{rel_path}"
                            f_res = requests.get(file_url, headers=REQUEST_HEADERS, timeout=10)
                            if f_res.status_code == 200:
                                dest_path = os.path.join(BASE_DIR, dest_rel)
                                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                                with open(dest_path, "wb") as f_out:
                                    f_out.write(f_res.content)
                                if dest_rel == "version.json" and ROOT_DIR != BASE_DIR:
                                    try:
                                        with open(os.path.join(ROOT_DIR, "version.json"), "wb") as f_root_ver:
                                            f_root_ver.write(f_res.content)
                                    except Exception:
                                        pass
                                log(f"    [OK] Descargado: {dest_rel}")
                                ota_updated = True
                            else:
                                log(f"    [AVISO] No se pudo descargar {rel_path} (HTTP {f_res.status_code})")
                        if ota_updated:
                            log("  [✓] Archivos de la aplicación actualizados exitosamente vía OTA.")
                    else:
                        log("  [OK] El código de la aplicación ya se encuentra en su última versión.")
                else:
                    log(f"  [AVISO] No se pudo leer version.json en GitHub (HTTP {r.status_code})")
            except Exception as ota_err:
                log(f"  [AVISO] Consulta OTA omitida: {ota_err}")
        else:
            log("  [INFO] Repositorio de GitHub no configurado para OTA.")

        # 2. Update yt-dlp via pip in main environment
        log("\n[2/6] Actualizando librería yt-dlp vía pip en entorno activo...")
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
        if proc.returncode != 0:
            raise RuntimeError(f"pip install falló con código {proc.returncode} al actualizar yt-dlp en el entorno principal.")

        # 3. Check if portable version exists and update it too
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
            log("\n[3/6] Detectada carpeta Portable. Actualizando yt-dlp en entorno Portable...")
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
            if proc2.returncode != 0:
                log(f"  [AVISO] pip retornó código de salida {proc2.returncode} en el entorno portable.")

            # Synchronize code files to portable
            log("\n[4/6] Sincronizando archivos (app.py, templates, static, version.json) con Portable...")
            try:
                shutil.copy2(os.path.join(BASE_DIR, "app.py"), os.path.join(portable_dest, "app.py"))
                ver_src = os.path.join(BASE_DIR, "version.json")
                if os.path.exists(ver_src):
                    shutil.copy2(ver_src, os.path.join(portable_dest, "version.json"))
                for folder in ["templates", "static"]:
                    src = os.path.join(BASE_DIR, folder)
                    dst = os.path.join(portable_dest, folder)
                    if os.path.exists(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                log("  [OK] Código sincronizado exitosamente en la versión Portable.")
            except Exception as e:
                log(f"  [AVISO] No se pudo sincronizar archivos a Portable: {e}")
        else:
            log("\n[3/6] Versión Portable no detectada como carpeta vecina.")
            log("[4/6] Omitiendo sincronización secundaria.")

        # 4. Clear cache
        log("\n[5/6] Limpiando memoria caché de firmas de YouTube...")
        if clear_ytdlp_cache():
            log("  [OK] Caché de firmas y reproductores eliminada.")
        else:
            log("  [AVISO] No fue necesario vaciar caché.")

        # 5. Ensure Deno JS runtime is present (required by yt-dlp 2026+ for YouTube signature solving)
        log("\n[6/6] Verificando runtime de JavaScript (Deno) para resolución de firmas YouTube...")
        deno_dir = os.path.join(BASE_DIR, "deno")
        deno_exe = os.path.join(deno_dir, "deno.exe") if os.name == 'nt' else os.path.join(deno_dir, "deno")
        deno_installed = False
        if os.path.exists(deno_exe):
            log(f"  [OK] Deno ya está instalado en: {deno_dir}")
            # Check for Deno updates
            try:
                deno_ver_res = subprocess.run([deno_exe, '--version'], capture_output=True, text=True, timeout=5)
                if deno_ver_res.returncode == 0:
                    log(f"  [*] Versión actual: {deno_ver_res.stdout.splitlines()[0].strip()}")
            except Exception:
                pass
            deno_installed = True
        else:
            log("  [!] Deno no encontrado. Descargando para soporte de firmas YouTube...")
            try:
                os.makedirs(deno_dir, exist_ok=True)
                if os.name == 'nt':
                    # Get latest version
                    ver_res = subprocess.run(['curl.exe', '--ssl-revoke-best-effort', '-s', 'https://dl.deno.land/release-latest.txt'],
                                             capture_output=True, text=True, timeout=10)
                    deno_version = ver_res.stdout.strip() if ver_res.returncode == 0 else 'v2.9.8'
                    log(f"  [*] Descargando Deno {deno_version}...")
                    zip_path = os.path.join(deno_dir, 'deno.zip')
                    dl_url = f'https://dl.deno.land/release/{deno_version}/deno-x86_64-pc-windows-msvc.zip'
                    dl_res = subprocess.run(['curl.exe', '--ssl-revoke-best-effort', '-Lo', zip_path, dl_url],
                                            capture_output=True, text=True, timeout=120)
                    if dl_res.returncode == 0 and os.path.exists(zip_path):
                        subprocess.run(['tar.exe', 'xf', zip_path, '-C', deno_dir], capture_output=True, timeout=30)
                        try:
                            os.remove(zip_path)
                        except Exception:
                            pass
                        if os.path.exists(deno_exe):
                            log(f"  [OK] Deno {deno_version} instalado exitosamente.")
                            deno_installed = True
                        else:
                            log("  [ERROR] No se encontró deno.exe después de la extracción.")
                    else:
                        log(f"  [ERROR] Falló la descarga de Deno (código {dl_res.returncode}).")
                else:
                    log("  [INFO] Instala Deno manualmente: curl -fsSL https://deno.land/install.sh | sh")
            except Exception as deno_err:
                log(f"  [AVISO] No se pudo instalar Deno automáticamente: {deno_err}")

        if deno_installed and deno_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = deno_dir + os.pathsep + os.environ.get("PATH", "")

        new_version = get_installed_ytdlp_version()
        log("\n=======================================================")
        log(f" [✓] ¡MANTENIMIENTO Y ACTUALIZACIÓN COMPLETADOS!")
        log(f" [✓] yt-dlp versión instalada: {new_version}")
        log(f" [✓] App versión: v{APP_VERSION}")
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

    # Estimate audio filesize for 320 kbps MP3
    audio_bytes = int((320 * 1000 / 8) * duration) if duration else 0
    filesize_mb = format_filesize_mb(audio_bytes) if audio_bytes > 0 else '~7.5 MB'

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


def download_and_tag(url: str, metadata: dict, sponsorblock: bool = False, cookies_from_browser: str = None, task_id: str = None) -> dict:
    """
    Download audio, convert to 320 kbps MP3, and embed ID3 tags
    including cover art (converted to clean JPEG in-memory). Supports SponsorBlock.
    """
    clean_url = normalize_media_url(url)
    artist = metadata.get('artist', 'Unknown')
    title = metadata.get('title', 'Unknown')
    safe_artist = sanitize_filename(artist)
    safe_title = sanitize_filename(title)
    filename = f"{safe_artist} - {safe_title}"
    output_path = os.path.join(CANCIONES_DIR, filename)

    q = download_tasks.get(task_id) if task_id else None

    pps = []
    if sponsorblock:
        pps.append({'key': 'SponsorBlock', 'categories': ['music_offtopic']})
        pps.append({'key': 'ModifyChapters', 'remove_sponsor_segments': ['music_offtopic']})
    pps.append({
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '320',
    })

    def music_progress_hook(d):
        if task_id and task_id in queue_mgr.cancel_flags:
            raise DownloadCancelled("Descarga cancelada por el usuario")
        if d['status'] == 'downloading':
            pct_raw = strip_ansi(d.get('_percent_str', '0%')).strip()
            try:
                pct = float(pct_raw.replace('%', ''))
            except ValueError:
                pct = 0
            speed = strip_ansi(d.get('_speed_str', '')).strip()
            eta = strip_ansi(d.get('_eta_str', '')).strip()
            downloaded = strip_ansi(d.get('_downloaded_bytes_str', '')).strip()
            total = strip_ansi(
                d.get('_total_bytes_str', '') or
                d.get('_total_bytes_estimate_str', '')
            ).strip()
            if task_id:
                queue_mgr.update_task_progress(task_id, pct, speed, eta, downloaded, total)
            if q:
                q.put({
                    'status': 'downloading',
                    'percent': pct,
                    'speed': speed,
                    'eta': eta,
                    'downloaded': downloaded,
                    'total': total,
                })
        elif d['status'] == 'finished' and q:
            q.put({
                'status': 'processing',
                'message': 'Convirtiendo audio a MP3 (320 kbps)...',
            })

    ydl_opts = {
        **YDL_COMMON,
        'format': 'bestaudio[protocol^=http]/bestaudio/best',
        'format_sort': ['proto:https', 'abr', 'ext:m4a'],
        'outtmpl': output_path + '.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'postprocessors': pps,
        'progress_hooks': [music_progress_hook] if q else [],
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
        err_msg = 'No se pudo encontrar el archivo MP3 descargado.'
        if q:
            q.put({'status': 'error', 'message': err_msg})
        return {'success': False, 'error': err_msg}

    if q:
        q.put({
            'status': 'processing',
            'message': 'Incrustando portada JPEG y metadatos ID3...',
        })

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

    # ── Download, convert to JPEG in-memory, and embed cover art ──────────────
    thumbnail_url = metadata.get('thumbnail_url', '')
    if thumbnail_url:
        try:
            resp = requests.get(thumbnail_url, headers=REQUEST_HEADERS, timeout=15)
            if resp.status_code == 200 and resp.content:
                jpeg_bytes, mime_type = convert_image_to_jpeg(resp.content)
                tags.add(APIC(
                    encoding=3,
                    mime=mime_type or 'image/jpeg',
                    type=3,  # Cover (front)
                    desc='Cover',
                    data=jpeg_bytes or resp.content,
                ))
        except Exception:
            pass  # Non-critical - skip cover if download fails

    audio.save()

    final_filename = os.path.basename(mp3_path)
    fsize = format_bytes(os.path.getsize(mp3_path)) if os.path.exists(mp3_path) else ''
    if task_id:
        queue_mgr.update_task_completion(task_id, mp3_path, fsize, final_filename)

    res = {
        'success': True,
        'filename': final_filename,
        'path': mp3_path,
        'filesize': fsize,
    }
    if q:
        q.put({
            'status': 'complete',
            'filename': final_filename,
            'path': mp3_path,
            'filesize': fsize,
        })
    return res


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
    audio_formats_https = [f for f in audio_formats if (f.get('protocol') or '').startswith('http')]
    best_audio = max(audio_formats_https or audio_formats, key=lambda f: f.get('abr') or f.get('tbr') or f.get('filesize') or 0) if audio_formats else None
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
        h_formats_https = [f for f in h_formats if (f.get('protocol') or '').startswith('http')]
        best_v = max(h_formats_https or h_formats, key=lambda f: f.get('tbr') or f.get('vbr') or f.get('filesize') or 0) if h_formats else None
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
    manual_subs = {k: v for k, v in (info.get('subtitles') or {}).items() if k != 'live_chat'}
    auto_subs = {k: v for k, v in (info.get('automatic_captions') or {}).items() if k != 'live_chat'}

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

        # ── Build format string with HTTPS priority to avoid broken m3u8 streams ──
        if download_type == 'audio_only':
            if audio_lang:
                format_str = (
                    f'bestaudio[language={audio_lang}][protocol^=http]/'
                    f'bestaudio[language^={audio_lang[:2]}][protocol^=http]/'
                    f'bestaudio[language={audio_lang}]/'
                    f'bestaudio[language^={audio_lang[:2]}]/'
                    f'bestaudio[protocol^=http]/'
                    f'bestaudio/best'
                )
            else:
                format_str = 'bestaudio[protocol^=http]/bestaudio/best'
        elif download_type == 'video_only':
            format_str = f'bestvideo[height<={height}][protocol^=http]/bestvideo[height<={height}]'
        else:  # video_audio
            if audio_lang:
                format_str = (
                    f'bestvideo[height<={height}][protocol^=http]+bestaudio[language={audio_lang}][protocol^=http]/'
                    f'bestvideo[height<={height}]+bestaudio[language={audio_lang}]/'
                    f'bestvideo[height<={height}][protocol^=http]+bestaudio[protocol^=http]/'
                    f'bestvideo[height<={height}]+bestaudio/'
                    f'best[height<={height}]'
                )
            else:
                format_str = (
                    f'bestvideo[height<={height}][protocol^=http]+bestaudio[protocol^=http]/'
                    f'bestvideo[height<={height}]+bestaudio/'
                    f'best[height<={height}]'
                )

        # ── Progress hook ───────────────────────────────────────────────────────
        def progress_hook(d):
            if task_id and task_id in queue_mgr.cancel_flags:
                raise DownloadCancelled("Descarga cancelada por el usuario")
            if d['status'] == 'downloading':
                pct_raw = strip_ansi(d.get('_percent_str', '0%')).strip()
                try:
                    pct = float(pct_raw.replace('%', ''))
                except ValueError:
                    pct = 0
                speed = strip_ansi(d.get('_speed_str', '')).strip()
                eta = strip_ansi(d.get('_eta_str', '')).strip()
                downloaded = strip_ansi(d.get('_downloaded_bytes_str', '')).strip()
                total = strip_ansi(
                    d.get('_total_bytes_str', '') or
                    d.get('_total_bytes_estimate_str', '')
                ).strip()
                if task_id:
                    queue_mgr.update_task_progress(task_id, pct, speed, eta, downloaded, total)
                q.put({
                    'status': 'downloading',
                    'percent': pct,
                    'speed': speed,
                    'eta': eta,
                    'downloaded': downloaded,
                    'total': total,
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
            'format_sort': ['proto:https', 'res', 'fps'],
            'outtmpl': output_template,
            'progress_hooks': [progress_hook],
            'logger': YDLLogger(),
            'postprocessors': [],
        }
        apply_browser_cookies(ydl_opts, cookies_from_browser)

        # ── Editor Compatible Mode (Force H.264 / AAC) ──────────────────────────
        if editor_mode and download_type != 'audio_only':
            ydl_opts['format_sort'] = ['proto:https', 'vcodec:h264', 'res', 'acodec:m4a']

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
                # Standardize all subtitles to clean SRT format before MP4 muxing to prevent mov_text codec errors
                ydl_opts['postprocessors'].append({
                    'key': 'FFmpegSubtitlesConvertor',
                    'format': 'srt',
                })
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

        # ── Clean up leftover raw subtitle files once embedded into MP4 ─────────
        if embed_subs and download_type != 'audio_only':
            for ext in ('.srt', '.vtt', '.srv3', '.srv2', '.srv1', '.ttml', '.json3'):
                try:
                    for f in os.listdir(target_dir):
                        if f.startswith(safe_title) and f.endswith(ext):
                            os.remove(os.path.join(target_dir, f))
                except Exception:
                    pass

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

        fsize = format_bytes(os.path.getsize(final_path)) if (os.path.exists(final_path) and not is_split) else ''
        if task_id:
            queue_mgr.update_task_completion(task_id, final_path, fsize, final_filename)

        q.put({
            'status': 'complete',
            'filename': final_filename,
            'path': final_path,
            'filesize': fsize,
            'is_dir': is_split,
        })

    except DownloadCancelled:
        raise
    except Exception as e:
        clean_err = strip_ansi(str(e))
        if task_id:
            with queue_mgr.lock:
                if task_id in queue_mgr.tasks:
                    queue_mgr.tasks[task_id]['status'] = 'failed'
                    queue_mgr.tasks[task_id]['error'] = clean_err
                    db_update_download(queue_mgr.tasks[task_id]['db_id'], status='failed')
        diag = diagnose_error(clean_err)
        q.put({
            'status': 'error',
            'message': clean_err,
            'diagnostic': diag,
        })
        raise


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


def _do_music_download(url: str, metadata: dict, sponsorblock: bool, cookies_from_browser: str, task_id: str):
    q = download_tasks.get(task_id)
    try:
        download_and_tag(url, metadata, sponsorblock=sponsorblock, cookies_from_browser=cookies_from_browser, task_id=task_id)
    except Exception as e:
        clean_err = strip_ansi(str(e))
        diag = diagnose_error(clean_err)
        if q:
            q.put({
                'status': 'error',
                'message': clean_err,
                'diagnostic': diag,
            })


@app.route('/api/download', methods=['POST'])
def download():
    """Download the song with metadata and SponsorBlock. Spawns background task for SSE progress tracking."""
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

    # Support sync fallback if requested
    if data.get('sync'):
        try:
            result = download_and_tag(url, metadata, sponsorblock=sponsorblock, cookies_from_browser=cookies_from_browser)
            return jsonify(result)
        except Exception as e:
            clean_err = strip_ansi(str(e))
            diag = diagnose_error(clean_err)
            return jsonify({'success': False, 'error': clean_err, 'diagnostic': diag}), 500

    task_id, position = queue_mgr.add_task(
        item_type='music',
        url=url,
        title=f"{metadata.get('artist', '')} - {metadata.get('title', '')}".strip(" -") or metadata.get('title', 'Canción'),
        options={
            'metadata': metadata,
            'sponsorblock': sponsorblock,
            'cookies_from_browser': cookies_from_browser,
        }
    )

    return jsonify({'success': True, 'task_id': task_id, 'position': position})


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
    """Start a video download via centralized queue manager with controlled concurrency."""
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

    task_id, position = queue_mgr.add_task(
        item_type=download_type,
        url=url,
        title=title or 'Vídeo',
        options={
            'height': height,
            'download_type': download_type,
            'title': title,
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
        }
    )

    return jsonify({'success': True, 'task_id': task_id, 'position': position})


@app.route('/api/video/progress/<task_id>')
@app.route('/api/music/progress/<task_id>')
def video_progress(task_id):
    """SSE endpoint: stream download progress events to the browser with guaranteed garbage collection."""
    def event_stream():
        with tasks_lock:
            q = download_tasks.get(task_id)
        if not q:
            # Check if task is already recorded in queue_mgr
            item = queue_mgr.get_task(task_id) if queue_mgr else None
            if item:
                if item.get('status') == 'completed':
                    yield f'data: {json.dumps({"status": "complete", "filename": item.get("title"), "path": item.get("filepath"), "filesize": item.get("filesize")}, ensure_ascii=False)}\n\n'
                    return
                elif item.get('status') == 'failed':
                    yield f'data: {json.dumps({"status": "error", "message": item.get("error", "Error en la descarga")}, ensure_ascii=False)}\n\n'
                    return
                elif item.get('status') == 'cancelled':
                    yield f'data: {json.dumps({"status": "error", "message": "Descarga cancelada por el usuario"}, ensure_ascii=False)}\n\n'
                    return
            yield f'data: {json.dumps({"status": "error", "message": "Tarea no encontrada"})}\n\n'
            return

        # Notificar al cliente si la tarea está en cola de espera
        if queue_mgr and task_id in queue_mgr.pending_queue:
            pos = queue_mgr.pending_queue.index(task_id) + 1
            yield f'data: {json.dumps({"status": "pending", "message": f"En cola de espera (turno #{pos}). Aguardando trabajador...", "position": pos})}\n\n'

        try:
            while True:
                try:
                    data = q.get(timeout=30)
                    yield f'data: {json.dumps(data, ensure_ascii=False)}\n\n'
                    if data.get('status') in ('complete', 'error'):
                        break
                except queue.Empty:
                    # Keep-alive heartbeat
                    yield f'data: {json.dumps({"status": "heartbeat"})}\n\n'
        finally:
            # Only pop from download_tasks if the task has actually finished!
            # If the user simply reconnected or refreshed mid-download, leave q intact.
            item = queue_mgr.get_task(task_id) if queue_mgr else None
            if not item or item.get('status') in ('completed', 'failed', 'cancelled'):
                with tasks_lock:
                    download_tasks.pop(task_id, None)

    return Response(
        event_stream(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ── Queue & History Routes ─────────────────────────────────────────────────────

@app.route('/api/history', methods=['GET'])
def get_history_route():
    """Retrieve last 100 downloads from SQLite history."""
    try:
        history = db_get_history(limit=100)
        return jsonify({'success': True, 'history': history})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/history/<int:download_id>', methods=['DELETE'])
def delete_history_route(download_id):
    """Delete a single record from SQLite history."""
    try:
        ok = db_delete_download(download_id)
        return jsonify({'success': ok, 'message': 'Registro eliminado correctamente'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/history/clear', methods=['DELETE'])
def clear_history_route():
    """Clear all records from SQLite history."""
    try:
        ok = db_clear_history()
        return jsonify({'success': ok, 'message': 'Historial vaciado correctamente'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/queue/add', methods=['POST'])
def queue_add():
    """Enqueue a new download (music or video) with controlled concurrency."""
    data = request.get_json(force=True) or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'success': False, 'error': 'No se proporcionó un enlace válido.'}), 400

    item_type = data.get('type', 'video_audio')
    title = data.get('title', '')
    options = data.get('options')
    if not options:
        options = {k: v for k, v in data.items() if k not in ('url', 'type', 'title')}

    task_id, position = queue_mgr.add_task(item_type, url, title, options)
    return jsonify({
        'success': True,
        'task_id': task_id,
        'position': position,
        'status': 'pending' if position > 0 else 'downloading'
    })


@app.route('/api/queue/status', methods=['GET'])
def queue_status():
    """Get active, pending, and recent queue status."""
    return jsonify(queue_mgr.get_status())


@app.route('/api/queue/cancel/<task_id>', methods=['POST'])
def queue_cancel(task_id):
    """Cancel a pending or active download task."""
    ok, msg = queue_mgr.cancel_task(task_id)
    return jsonify({'success': ok, 'message': msg})


@app.route('/api/queue/config', methods=['POST'])
def queue_config():
    """Update max concurrent downloads (1 to 5)."""
    data = request.get_json(silent=True) or {}
    workers = data.get('max_workers')
    if workers is not None:
        queue_mgr.set_max_workers(workers)
    return jsonify({'success': True, 'max_workers': queue_mgr.max_workers})


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


def _json3_to_text(content: str) -> str:
    """Convierte un subtitulo json3 de YouTube en texto plano, una frase por linea."""
    sub_data = json.loads(content)
    lines = []
    for event in sub_data.get('events', []):
        text = ''.join(s.get('utf8', '') for s in event.get('segs') or [])
        for line in text.split('\n'):
            line = line.strip()
            if line and (not lines or lines[-1] != line):
                lines.append(line)
    return '\n'.join(lines)


def _vtt_to_text(content: str) -> str:
    """Convierte WebVTT en texto plano; quita cabecera, tiempos, etiquetas y repeticiones de los auto-subtitulos."""
    lines = []
    in_header = True
    for raw in content.replace('﻿', '').splitlines():
        line = raw.strip()
        if in_header:
            if not line:
                in_header = False
            continue
        if not line or '-->' in line or line.startswith(('NOTE', 'STYLE', 'REGION')):
            continue
        line = html.unescape(re.sub(r'<[^>]+>', '', line)).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return '\n'.join(lines)


class _SubtitleLogger:
    """Captura los avisos de yt-dlp (p. ej. HTTP 429) para poder explicarlos al usuario."""

    def __init__(self):
        self.messages = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.messages.append(str(msg))

    def error(self, msg):
        self.messages.append(str(msg))


def _download_transcript(clean_url, lang, sub_type, cookies_from_browser):
    """Descarga el subtitulo con yt-dlp (soporta HLS y reintenta ante 429) y lo devuelve como (titulo, texto)."""
    last_logger = None
    rate_limited = False
    attempts = 3
    use_cookies = bool(cookies_from_browser)

    for attempt in range(attempts):
        with tempfile.TemporaryDirectory(prefix='ytsubs_') as tmp_dir:
            logger = _SubtitleLogger()
            ydl_opts = {
                **YDL_COMMON,
                'quiet': True,
                'no_warnings': True,
                'logger': logger,
                'skip_download': True,
                'playlist_items': '1',
                'writesubtitles': sub_type == 'manual',
                'writeautomaticsub': sub_type != 'manual',
                'subtitleslangs': [re.escape(lang)],
                'subtitlesformat': 'json3/vtt/best',
                'outtmpl': os.path.join(tmp_dir, 'sub.%(ext)s'),
            }
            if use_cookies:
                apply_browser_cookies(ydl_opts, cookies_from_browser)

            try:
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(clean_url, download=True)
            except Exception as e:
                if use_cookies:
                    use_cookies = False  # las cookies del navegador fallaron: reintentar sin ellas
                    continue
                if '429' not in str(e):
                    raise
                rate_limited = True
                logger.messages.append(str(e))
                if attempt < attempts - 1:
                    time.sleep(3 * (attempt + 1))
                continue

            if info.get('entries'):
                info = next((e for e in info['entries'] if e), info)
            title = sanitize_filename(info.get('title', 'video'))
            available = info.get('subtitles' if sub_type == 'manual' else 'automatic_captions') or {}
            if lang not in available:
                raise ValueError(f'No se encontraron subtitulos en "{get_lang_name(lang)}".')

            files = [f for f in sorted(glob.glob(os.path.join(tmp_dir, 'sub.*')))
                     if f.lower().endswith(('.json3', '.vtt'))]
            for path in files:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                try:
                    text = _json3_to_text(content) if path.lower().endswith('.json3') else _vtt_to_text(content)
                except (json.JSONDecodeError, AttributeError):
                    continue
                if text.strip():
                    return title, text

            last_logger = logger

        if attempt < attempts - 1:
            time.sleep(3 * (attempt + 1))

    if rate_limited or (last_logger and any('429' in m for m in last_logger.messages)):
        raise RuntimeError(
            'YouTube limito temporalmente la descarga de este subtitulo (HTTP 429). '
            'Espera uno o dos minutos y reintenta, o elige el idioma original del video '
            '(los subtitulos traducidos automaticamente son los mas afectados).'
        )
    raise RuntimeError('No se pudo descargar el subtitulo. Reintenta en unos minutos o prueba con otro idioma.')


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
        try:
            title, plain_text = _download_transcript(clean_url, lang, sub_type, cookies_from_browser)
        except (ValueError, RuntimeError) as e:
            return jsonify({'success': False, 'error': str(e)})

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
    
    # Method 1 (Primary for Windows): Direct invocation of explorer.exe with folder path
    try:
        subprocess.Popen(['explorer.exe', folder_path])
        return True, folder_path
    except Exception:
        pass

    # Method 2: PowerShell Start-Process (launches in interactive user session)
    try:
        ps_cmd = f'Start-Process explorer.exe -ArgumentList @("{folder_path}")'
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True,
            timeout=5
        )
        if res.returncode == 0:
            return True, folder_path
    except Exception:
        pass

    # Method 3: Shell start command
    try:
        subprocess.Popen(f'start "" "{folder_path}"', shell=True)
        return True, folder_path
    except Exception:
        pass

    # Method 4: Native ShellExecute via os.startfile
    if hasattr(os, 'startfile'):
        try:
            os.startfile(folder_path)
            return True, folder_path
        except Exception:
            pass

    # Method 5: Direct Win32 ShellExecuteW via ctypes
    try:
        import ctypes
        res = ctypes.windll.shell32.ShellExecuteW(None, "open", folder_path, None, None, 1)
        if res > 32:
            return True, folder_path
    except Exception as e:
        raise RuntimeError(f"No se pudo abrir el explorador de Windows: {e}")

    return True, folder_path


@app.route('/api/open-folder', methods=['POST'])
def open_folder():
    """Open canciones, videos, solo subtitulos or miniaturas folder in Windows Explorer securely."""
    data = request.get_json(force=True) if request.data else {}
    target = str(data.get('folder', 'videos')).strip().lower()
    
    if target in ALLOWED_FOLDERS:
        raw_path = ALLOWED_FOLDERS[target]
    else:
        # Sanitize against path traversal attacks
        cand = os.path.normpath(os.path.abspath(target))
        root_abs = os.path.normpath(os.path.abspath(ROOT_DIR))
        if cand.startswith(root_abs) and os.path.isdir(cand):
            raw_path = cand
        else:
            raw_path = VIDEOS_DIR

    try:
        ok, resolved_path = _launch_explorer(raw_path)
        return jsonify({'success': True, 'path': resolved_path})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/download-file/<folder>/<path:filename>')
def serve_file(folder, filename):
    """Download or view a generated file directly from the browser with strict Path Traversal defense."""
    from flask import send_from_directory
    folder_key = str(folder).strip().lower()
    if folder_key not in ALLOWED_FOLDERS:
        return jsonify({'success': False, 'error': 'Directorio no permitido'}), 403

    target_dir = os.path.normpath(os.path.abspath(ALLOWED_FOLDERS[folder_key]))
    safe_name = os.path.basename(filename)
    full_path = os.path.normpath(os.path.abspath(os.path.join(target_dir, safe_name)))

    # Prevent Path Traversal by verifying full_path is strictly within target_dir
    if not full_path.startswith(target_dir + os.sep) and full_path != target_dir:
        return jsonify({'success': False, 'error': 'Acceso denegado'}), 403

    if not os.path.isfile(full_path):
        return jsonify({'success': False, 'error': 'Archivo no encontrado'}), 404

    return send_from_directory(target_dir, safe_name, as_attachment=True)


# ── System Maintenance & Update Routes ───────────────────────────────────────────

@app.route('/api/system/status')
def system_status():
    """Get installed and remote yt-dlp versions and health status."""
    installed = get_installed_ytdlp_version()
    latest = get_latest_pypi_ytdlp_version()
    portable_ytdlp_ver = get_portable_ytdlp_version()
    app_ver = get_app_version()

    ytdlp_update = False
    if latest and installed and installed != 'Desconocida':
        # Normalize versions (e.g., 2026.08.19 vs 2026.8.19)
        def norm(v):
            return [int(x) for x in re.findall(r'\d+', str(v))]
        try:
            ytdlp_update = norm(latest) > norm(installed)
        except Exception:
            ytdlp_update = (installed != latest)

    app_update = check_remote_app_update()
    app_update_available = bool(app_update.get("update_available", False))
    update_available = bool(ytdlp_update or app_update_available)

    return jsonify({
        'success': True,
        'app_version': app_ver,
        'installed_version': installed,
        'latest_version': latest or installed,
        'update_available': update_available,
        'ytdlp_update_available': ytdlp_update,
        'app_update_available': app_update_available,
        'remote_app_version': app_update.get("latest_version", app_ver),
        'has_portable': True,
        'portable_version': app_ver,
        'portable_ytdlp_version': portable_ytdlp_ver or installed,
    })


@app.route('/api/system/update', methods=['POST'])
def system_update():
    """Trigger system update in background thread."""
    task_id = str(uuid.uuid4())
    with tasks_lock:
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
    """SSE endpoint to stream update logs with guaranteed garbage collection."""
    def event_stream():
        with tasks_lock:
            q = update_tasks.get(task_id)
        if not q:
            yield f'data: {json.dumps({"status": "error", "message": "Tarea de actualizacion no encontrada"})}\n\n'
            return
        try:
            while True:
                try:
                    data = q.get(timeout=30)
                    yield f'data: {json.dumps(data, ensure_ascii=False)}\n\n'
                    if data.get('status') in ('complete', 'error'):
                        break
                except queue.Empty:
                    yield f'data: {json.dumps({"status": "heartbeat"})}\n\n'
        finally:
            with tasks_lock:
                update_tasks.pop(task_id, None)

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


@app.route('/api/system/check-app-update', methods=['GET'])
def check_app_update_route():
    """Check if a new version of the app is available on GitHub."""
    try:
        info = check_remote_app_update()
        return jsonify({'success': True, **info})
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'update_available': False,
            'current_version': APP_VERSION,
            'latest_version': APP_VERSION,
            'changelog': '',
        })


@app.route('/api/system/report-bug', methods=['POST'])
def report_bug():
    """Log bug report to local file and dispatch embed to Discord webhook."""
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or 'N/A').strip()
    error_msg = (data.get('error') or 'Error no especificado').strip()
    user_note = (data.get('note') or '').strip()
    diagnostic = data.get('diagnostic') or {}

    timestamp_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Guardar en archivo local
    log_file = os.path.join(ROOT_DIR, 'ultimo_reporte_error.log')
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"REPORTE DE FALLO: {timestamp_str}\n")
            f.write(f"Versión: {APP_VERSION}\n")
            f.write(f"URL: {url}\n")
            f.write(f"Error: {error_msg}\n")
            if user_note:
                f.write(f"Nota del usuario: {user_note}\n")
            if diagnostic:
                f.write(f"Diagnóstico: {json.dumps(diagnostic, ensure_ascii=False, indent=2)}\n")
            f.write(f"{'='*60}\n")
    except Exception as e:
        print(f"[ERROR] No se pudo escribir en ultimo_reporte_error.log: {e}")

    # Enviar al Webhook de Discord
    webhook_sent = False
    webhook_error = None
    if BUG_REPORT_WEBHOOK and not BUG_REPORT_WEBHOOK.startswith("PEGA_AQUI"):
        try:
            fields = [
                {"name": "📦 Versión de la App", "value": f"`{APP_VERSION}`", "inline": True},
                {"name": "🌐 URL Reportada", "value": f"`{url[:300]}`" if url != 'N/A' else "`N/A`", "inline": False},
                {"name": "⚠️ Mensaje de Error", "value": f"```{error_msg[:900]}```", "inline": False},
            ]
            if user_note:
                fields.append({"name": "💬 Nota del Usuario", "value": user_note[:500], "inline": False})
            if diagnostic and isinstance(diagnostic, dict):
                diag_summary = (
                    f"• Tipo: {diagnostic.get('type', 'desconocido')}\n"
                    f"• Título: {diagnostic.get('title', 'N/A')}\n"
                    f"• Sugerencia: {diagnostic.get('suggestion', 'N/A')}"
                )
                fields.append({"name": "🔍 Diagnóstico Asistido", "value": diag_summary[:800], "inline": False})

            discord_payload = {
                "content": "🚨 **Nuevo reporte de error recibido en Descargador Universal**",
                "embeds": [
                    {
                        "title": "Bug Report - Descargador Universal",
                        "description": "Un usuario ha presionado el botón de reporte de fallos.",
                        "color": 15158332,  # Rojo carmesí
                        "fields": fields,
                        "footer": {
                            "text": f"Descargador Universal v{APP_VERSION} • {timestamp_str}"
                        },
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                ]
            }

            resp = requests.post(
                BUG_REPORT_WEBHOOK,
                json=discord_payload,
                headers=REQUEST_HEADERS,
                timeout=8
            )
            webhook_sent = (resp.status_code in (200, 204))
            if not webhook_sent:
                webhook_error = f"Discord HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            webhook_error = str(e)

    return jsonify({
        'success': True,
        'message': 'Reporte de fallo registrado correctamente. ¡Gracias por avisar a Alex!',
        'webhook_sent': webhook_sent,
        'webhook_error': webhook_error,
        'local_log': log_file
    })


@app.route('/api/system/feedback', methods=['POST'])
def send_feedback():
    """Send user suggestion, idea, comment, or question directly to Alex's Discord webhook."""
    data = request.get_json(silent=True) or {}
    msg_type = (data.get('type') or '💡 Sugerencia').strip()
    sender = (data.get('sender') or 'Usuario').strip() or 'Usuario'
    message = (data.get('message') or '').strip()
    url = (data.get('url') or 'N/A').strip() or 'N/A'

    if not message:
        return jsonify({'success': False, 'error': 'Por favor escribe un mensaje o sugerencia.'}), 400

    timestamp_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Guardar en archivo local
    log_file = os.path.join(ROOT_DIR, 'ultimo_reporte_error.log')
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"FEEDBACK / MENSAJE DE USUARIO: {timestamp_str}\n")
            f.write(f"Tipo: {msg_type}\n")
            f.write(f"Remitente: {sender}\n")
            f.write(f"URL: {url}\n")
            f.write(f"Mensaje:\n{message}\n")
            f.write(f"{'='*60}\n")
    except Exception as e:
        print(f"[ERROR] No se pudo escribir en log: {e}")

    # Discord color:
    # Error: 15158332 (Rojo)
    # Sugerencia / Idea: 3066993 (Cyan/Verde)
    # Mensaje: 10181046 (Violeta)
    color = 10181046
    if any(k in msg_type.lower() for k in ['error', 'fallo', 'bug']):
        color = 15158332
    elif any(k in msg_type.lower() for k in ['sugerencia', 'idea', 'nueva']):
        color = 3066993

    webhook_sent = False
    webhook_error = None
    if BUG_REPORT_WEBHOOK and not BUG_REPORT_WEBHOOK.startswith("PEGA_AQUI"):
        try:
            fields = [
                {"name": "🏷️ Tipo", "value": f"`{msg_type}`", "inline": True},
                {"name": "👤 Remitente", "value": f"`{sender}`", "inline": True},
                {"name": "📦 Versión", "value": f"`v{APP_VERSION}`", "inline": True},
            ]
            if url and url != 'N/A':
                fields.append({"name": "🌐 Enlace Relacionado", "value": f"`{url[:350]}`", "inline": False})
            fields.append({"name": "💬 Mensaje / Sugerencia", "value": message[:1024], "inline": False})

            discord_payload = {
                "content": f"📬 **Nuevo mensaje recibido para Alex en Descargador Universal**",
                "embeds": [
                    {
                        "title": f"{msg_type} - Descargador Universal",
                        "color": color,
                        "fields": fields,
                        "footer": {
                            "text": f"Descargador Universal v{APP_VERSION} • {timestamp_str}"
                        },
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                ]
            }

            resp = requests.post(
                BUG_REPORT_WEBHOOK,
                json=discord_payload,
                headers=REQUEST_HEADERS,
                timeout=8
            )
            webhook_sent = (resp.status_code in (200, 204))
            if not webhook_sent:
                webhook_error = f"Discord HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            webhook_error = str(e)

    return jsonify({
        'success': True,
        'message': '¡Tu mensaje ha sido enviado exitosamente al Discord de Alex!',
        'webhook_sent': webhook_sent,
        'webhook_error': webhook_error,
    })


@app.route('/api/system/restart', methods=['POST'])
def system_restart():
    """Restart the Python process to reload sys.modules (e.g. freshly upgraded yt-dlp) without port collisions."""
    def do_restart():
        import time
        time.sleep(0.6)
        env = os.environ.copy()
        env["YTDLP_NO_BROWSER"] = "1"
        try:
            cmd = [
                sys.executable, "-c",
                "import time, subprocess, sys; time.sleep(1.5); subprocess.Popen(sys.argv[1:], close_fds=True)",
                sys.executable
            ] + sys.argv
            subprocess.Popen(cmd, env=env, close_fds=True)
        except Exception as e:
            print(f"[ERROR] No se pudo reiniciar el proceso: {e}")
        os._exit(0)

    threading.Thread(target=do_restart, daemon=True).start()
    return jsonify({'success': True, 'message': 'Reiniciando motor en segundo plano...'})


@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    """Shut down the Flask server gracefully without dropping the HTTP response."""
    def stop_server():
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            os._exit(0)

    threading.Timer(0.5, stop_server).start()
    return jsonify({'success': True, 'status': 'shutting_down', 'message': 'Servidor apagándose ordenadamente...'})


# ─── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import webbrowser
    print(f"\n  [*] YouTube Downloader")
    print(f"  [>] Canciones se guardaran en: {CANCIONES_DIR}")
    print(f"  [>] Videos se guardaran en:    {VIDEOS_DIR}")
    print(f"  [>] Abriendo http://localhost:5000 ...\n")
    # Open browser after a short delay only if not restarted via update
    if os.environ.get("YTDLP_NO_BROWSER") != "1":
        threading.Timer(1.5, lambda: webbrowser.open('http://localhost:5000')).start()
    app.run(debug=False, port=5000)
