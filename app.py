import os
import re
import glob
import html
import time
import tempfile
import hashlib
import py_compile
import socket
import ipaddress
import importlib
import importlib.util
import sys
import json
import uuid
import signal
import threading
import queue
import subprocess
import shutil
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, quote
import requests
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify, Response
from yt_dlp import YoutubeDL as _RealYoutubeDL
from yt_dlp.utils import download_range_func as _real_download_range_func
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, APIC, ID3NoHeaderError

# La consola de Windows usa cp1252: un caracter raro en un mensaje de yt-dlp no debe tumbar el programa
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

BOOT_ID = uuid.uuid4().hex          # cambia en cada arranque: la web lo usa para saber que el servidor se reinicio
BOOT_TIME = time.time()
APP_ID = 'descargador-universal'
DEFAULT_PORT = 5000
RESTART_EXIT_CODE = 75              # codigo con el que se pide a los .bat que relancen el programa

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
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # las peticiones de la web son JSON pequeños

# ─── Seguridad de la API local ──────────────────────────────────────────────────
# El servidor solo escucha en 127.0.0.1, pero cualquier pagina web abierta en el navegador puede enviarle
# peticiones (CSRF) o resolver un dominio suyo a 127.0.0.1 (DNS rebinding). Se aceptan unicamente peticiones
# cuyo Host sea local y, si traen Origin, que sea esta misma web.
LOCAL_HOSTNAMES = {'localhost', '127.0.0.1', '[::1]', '::1'}
_last_user_activity = time.time()
_PASSIVE_PATHS = ('/api/ping', '/api/system/status', '/api/system/check-app-update', '/api/queue/status')


def _host_only(netloc: str) -> str:
    netloc = (netloc or '').strip().lower()
    if netloc.startswith('['):
        return netloc.split(']')[0] + ']'
    return netloc.rsplit(':', 1)[0] if ':' in netloc else netloc


@app.before_request
def guard_local_requests():
    global _last_user_activity
    if _host_only(request.host) not in LOCAL_HOSTNAMES:
        return jsonify({'success': False, 'error': 'Host no permitido'}), 403
    origin = request.headers.get('Origin')
    if origin and origin != 'null':
        if urlparse(origin).netloc.lower() != request.host.lower():
            return jsonify({'success': False, 'error': 'Origen no permitido'}), 403
    elif origin == 'null' and request.method not in ('GET', 'HEAD', 'OPTIONS'):
        return jsonify({'success': False, 'error': 'Origen no permitido'}), 403
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('Sec-Fetch-Site') == 'cross-site':
        return jsonify({'success': False, 'error': 'Peticion entre sitios bloqueada'}), 403
    if request.path not in _PASSIVE_PATHS:
        _last_user_activity = time.time()


@app.after_request
def add_security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'no-referrer')
    resp.headers.setdefault('Content-Security-Policy', "frame-ancestors 'none'; object-src 'none'; base-uri 'self'")
    if request.path.startswith('/api/') or request.path == '/':
        resp.headers['Cache-Control'] = 'no-store'
    return resp


def _json_error(status, message):
    return jsonify({'success': False, 'error': message}), status


@app.errorhandler(400)
def _err_400(e):
    return _json_error(400, 'Peticion no valida.')


@app.errorhandler(404)
def _err_404(e):
    return _json_error(404, 'No encontrado.')


@app.errorhandler(405)
def _err_405(e):
    return _json_error(405, 'Metodo no permitido.')


@app.errorhandler(413)
def _err_413(e):
    return _json_error(413, 'La peticion es demasiado grande.')


@app.errorhandler(Exception)
def _err_unhandled(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return _json_error(e.code or 500, e.description)
    print(f"[ERROR] Excepcion no controlada en {request.path}: {e}")
    return _json_error(500, strip_ansi(str(e)) or 'Error interno.')


def is_public_http_url(url: str) -> bool:
    """True si la URL es http(s) y apunta a internet (no a localhost ni a la red local)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return False
        for info in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(info[4][0].split('%')[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def safe_fetch(url: str, timeout: int = 15, max_bytes: int = 15 * 1024 * 1024):
    """GET de una imagen/archivo remoto validando cada redireccion contra destinos internos (SSRF)."""
    for _ in range(4):
        if not is_public_http_url(url):
            raise ValueError('URL no permitida.')
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout, allow_redirects=False, stream=True)
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get('Location'):
            url = requests.compat.urljoin(url, resp.headers['Location'])
            resp.close()
            continue
        data = resp.raw.read(max_bytes + 1, decode_content=True)
        resp.close()
        if len(data) > max_bytes:
            raise ValueError('El archivo es demasiado grande.')
        resp._content = data
        return resp
    raise ValueError('Demasiadas redirecciones.')


@app.route('/api/ping')
def ping():
    """Identifica esta aplicacion (para detectar otra instancia ya abierta y reinicios del servidor)."""
    return jsonify({
        'success': True,
        'app': APP_ID,
        'version': APP_VERSION,
        'boot_id': BOOT_ID,
        'pid': os.getpid(),
    })


# App Version, OTA Remote Updates & Bug Reporting
APP_VERSION = "2.2.0"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/alexis-david-laura-imata/descargador-universal/main"
if os.environ.get("YTDL_OTA_BASE", "").startswith(("http://127.0.0.1", "http://localhost")):
    GITHUB_RAW_BASE = os.environ["YTDL_OTA_BASE"]  # solo para pruebas locales
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

# ─── yt-dlp con recarga en caliente ─────────────────────────────────────────────
# pip puede actualizar yt-dlp en disco, pero el proceso en marcha sigue con la version vieja cargada en memoria
# (y los extractores se importan de forma perezosa, asi que incluso se mezclarian versiones). Para que una
# actualizacion funcione sin reiniciar, todo el codigo usa este envoltorio: cuenta las sesiones activas y,
# cuando no queda ninguna, descarta los modulos yt_dlp y los vuelve a importar.

_ydl_lock = threading.Lock()
_ydl_inflight = 0
_ydl_reload_pending = False
_ydl_loaded_version = getattr(sys.modules.get('yt_dlp.version'), '__version__', '')


class YoutubeDL:
    """Envoltorio de yt_dlp.YoutubeDL (uso: with YoutubeDL(opts) as ydl) compatible con recarga en caliente."""

    def __init__(self, params=None, *args, **kwargs):
        self._call = (params, args, kwargs)
        self._inst = None

    def __enter__(self):
        global _ydl_inflight
        with _ydl_lock:
            _ydl_inflight += 1
            real = _RealYoutubeDL
        try:
            params, args, kwargs = self._call
            self._inst = real(params, *args, **kwargs)
            return self._inst.__enter__()
        except BaseException:
            self._release()
            raise

    def __exit__(self, *exc):
        try:
            return self._inst.__exit__(*exc)
        finally:
            self._release()

    def _release(self):
        global _ydl_inflight
        do_reload = False
        with _ydl_lock:
            _ydl_inflight = max(0, _ydl_inflight - 1)
            do_reload = _ydl_reload_pending and _ydl_inflight == 0
        if do_reload:
            threading.Thread(target=reload_ytdlp, daemon=True).start()


def download_range_func(*args, **kwargs):
    return _real_download_range_func(*args, **kwargs)


def _unwrap_urllib3_percent_re():
    """Quita el parche de yt-dlp sobre urllib3 y devuelve el objeto que habia (para poder restaurarlo)."""
    try:
        import urllib3.util.url as url_mod
    except Exception:
        return None
    current = getattr(url_mod, '_PERCENT_RE', None)
    original = current
    while current is not None and current.__class__.__name__ == 'Urllib3PercentREOverride':
        current = current.re
    if current is not None:
        url_mod._PERCENT_RE = current
    return original


def _restore_urllib3_percent_re(original) -> None:
    if original is not None:
        try:
            import urllib3.util.url as url_mod
            url_mod._PERCENT_RE = original
        except Exception:
            pass


def reload_ytdlp(force: bool = False) -> bool:
    """Recarga yt-dlp desde disco. Devuelve True si ya esta recargado; False si queda pendiente
    porque hay descargas/consultas en curso (se recargara solo cuando terminen)."""
    global _RealYoutubeDL, _real_download_range_func, _ydl_reload_pending, _ydl_loaded_version
    with _ydl_lock:
        if _ydl_inflight > 0 and not force:
            _ydl_reload_pending = True
            return False
        _ydl_reload_pending = False
        stale = [m for m in list(sys.modules)
                 if m in ('yt_dlp', 'yt_dlp_ejs') or m.startswith(('yt_dlp.', 'yt_dlp_ejs.', 'yt_dlp_plugins'))]
        saved = {name: sys.modules.pop(name) for name in stale}
        importlib.invalidate_caches()
        # Al importarse, yt-dlp "parchea" urllib3._PERCENT_RE envolviendolo en otro objeto. Importarlo por segunda vez
        # lo envolveria dos veces y romperia TODAS las peticiones de requests (AttributeError: ... no attribute 'sub').
        patched_re = _unwrap_urllib3_percent_re()
        try:
            import yt_dlp
            from yt_dlp import YoutubeDL as new_cls
            from yt_dlp.utils import download_range_func as new_range
        except Exception:
            # Si la nueva instalacion no se puede importar, conservar el motor anterior en lugar de dejar el programa sin yt-dlp
            for name in [m for m in sys.modules if m == 'yt_dlp' or m.startswith('yt_dlp.')]:
                del sys.modules[name]
            sys.modules.update(saved)
            _restore_urllib3_percent_re(patched_re)
            raise
        _RealYoutubeDL = new_cls
        _real_download_range_func = new_range
        _ydl_loaded_version = getattr(yt_dlp.version, '__version__', '')
    return True


def ytdlp_loaded_version() -> str:
    """Version de yt-dlp que realmente esta cargada en memoria."""
    return _ydl_loaded_version or get_installed_ytdlp_version()


# Progress tracking for video downloads and system updates (thread-safe)
tasks_lock = threading.Lock()
download_tasks = {}  # task_id -> queue.Queue
update_tasks = {}    # task_id -> queue.Queue

# ─── SQLite History Database & ThreadPool Queue Manager ─────────────────────────
DB_PATH = os.environ.get("YTDL_DB_PATH") or os.path.join(ROOT_DIR, "history.db")
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
            with closing(sqlite3.connect(DB_PATH, timeout=20)) as conn:
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
            with closing(sqlite3.connect(DB_PATH, timeout=20)) as conn:
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
            with closing(sqlite3.connect(DB_PATH, timeout=20)) as conn:
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
            with closing(sqlite3.connect(DB_PATH, timeout=20)) as conn:
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
            with closing(sqlite3.connect(DB_PATH, timeout=20)) as conn:
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
            with closing(sqlite3.connect(DB_PATH, timeout=20)) as conn:
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
                    output_format=opts.get('output_format'),
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


NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

CONFIG_PATH = os.path.join(ROOT_DIR, 'config.json')
_config_lock = threading.Lock()
DEFAULT_CONFIG = {'auto_update': True, 'last_ytdlp_version': ''}


def load_config() -> dict:
    """Configuracion local (config.json junto a la app). Nunca falla: ante un archivo roto usa los valores por defecto."""
    cfg = dict(DEFAULT_CONFIG)
    with _config_lock:
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, ValueError):
            pass
    return cfg


def save_config(cfg: dict) -> None:
    with _config_lock:
        try:
            tmp = CONFIG_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            os.replace(tmp, CONFIG_PATH)
        except OSError as e:
            print(f"[AVISO] No se pudo guardar config.json: {e}")


def clear_ytdlp_cache() -> bool:
    """Clear yt-dlp player cache."""
    try:
        with YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
            ydl.cache.remove()
        return True
    except Exception:
        return False


def get_loaded_ytdlp_version() -> str:
    """Version de yt-dlp que esta realmente cargada en memoria."""
    return ytdlp_loaded_version() or 'Desconocida'


def get_installed_ytdlp_version() -> str:
    """Version de yt-dlp instalada en disco en el entorno activo (puede ser mas nueva que la cargada si falta recargar)."""
    try:
        importlib.invalidate_caches()
        from importlib import metadata
        return metadata.version('yt-dlp')
    except Exception:
        return ytdlp_loaded_version() or 'No instalada'


def get_latest_pypi_ytdlp_version() -> str:
    """Query PyPI to get the latest available version of yt-dlp."""
    try:
        r = requests.get('https://pypi.org/pypi/yt-dlp/json', headers=REQUEST_HEADERS, timeout=6)
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


def _find_portable_python():
    """(python.exe, carpeta_de_codigo) de la version portable vecina, o (None, None)."""
    portable_root = os.path.join(BASE_DIR, "YT Music Downloader Portable")
    for sub in ["software", ""]:
        py_path = os.path.join(portable_root, sub, "python", "python.exe") if sub else os.path.join(portable_root, "python", "python.exe")
        if os.path.exists(py_path):
            return py_path, (os.path.join(portable_root, sub) if sub else portable_root)
    return None, None


def get_portable_ytdlp_version() -> str:
    """Get the yt-dlp version installed in the Portable environment or folder."""
    # If running inside the portable environment itself
    if is_portable or "portable" in sys.executable.lower():
        return get_installed_ytdlp_version()

    # Otherwise look into neighbor portable directory
    portable_python, _ = _find_portable_python()
    if portable_python:
        try:
            res = subprocess.run(
                [portable_python, "-c", "from importlib import metadata; print(metadata.version('yt-dlp'))"],
                capture_output=True, text=True, timeout=8, creationflags=NO_WINDOW
            )
            if res.returncode == 0:
                return res.stdout.strip()
        except Exception:
            pass
    return None


def same_version(a: str, b: str) -> bool:
    """Compara versiones ignorando ceros a la izquierda (2026.08.19 == 2026.8.19, que es como las normaliza pip)."""
    return [int(x) for x in re.findall(r'\d+', str(a))] == [int(x) for x in re.findall(r'\d+', str(b))]


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


# ── Informacion remota (PyPI / GitHub) con cache para no bloquear la web ─────────
OTA_DEFAULT_FILES = ['app.py', 'templates/index.html', 'static/style.css', 'version.json']
update_state = {'last_check': None, 'last_result': '', 'restart_pending': False, 'auto_running': False}
_remote_cache = {'ts': 0.0, 'pypi': None, 'ota': None, 'portable': None}
_remote_cache_lock = threading.Lock()


def fetch_remote_manifest(timeout: int = 6):
    """Lee version.json de GitHub (con parametro anti-cache de la CDN). None si no se pudo."""
    if not GITHUB_RAW_BASE or "PEGA_AQUI" in GITHUB_RAW_BASE:
        return None
    try:
        res = requests.get(f"{GITHUB_RAW_BASE.rstrip('/')}/version.json", params={'t': int(time.time())},
                           headers=REQUEST_HEADERS, timeout=timeout)
        if res.status_code == 200:
            data = res.json()
            return {
                'version': str(data.get('version', '')).strip(),
                'changelog': str(data.get('changelog', '')).strip(),
                'files': data.get('files') if isinstance(data.get('files'), dict) else {},
            }
    except Exception:
        pass
    return None


def refresh_remote_info(force: bool = False, max_age: int = 600) -> dict:
    """Consulta PyPI, GitHub y la version portable en paralelo; cachea el resultado `max_age` segundos."""
    with _remote_cache_lock:
        if not force and time.time() - _remote_cache['ts'] < max_age:
            return dict(_remote_cache)
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_pypi = pool.submit(get_latest_pypi_ytdlp_version)
            f_ota = pool.submit(fetch_remote_manifest)
            f_port = pool.submit(get_portable_ytdlp_version)
            _remote_cache['pypi'] = f_pypi.result()
            _remote_cache['ota'] = f_ota.result()
            _remote_cache['portable'] = f_port.result()
        _remote_cache['ts'] = time.time()
        return dict(_remote_cache)


def check_remote_app_update() -> dict:
    """Check GitHub repository for a newer app version."""
    current_ver = get_app_version()
    manifest = refresh_remote_info().get('ota')
    if manifest and manifest.get('version'):
        return {
            "update_available": is_newer_version(manifest['version'], current_ver),
            "current_version": current_ver,
            "latest_version": manifest['version'],
            "changelog": manifest.get('changelog', ''),
        }
    return {"update_available": False, "current_version": current_ver, "latest_version": current_ver, "changelog": ""}


# ── Actualizacion de yt-dlp (pip) ───────────────────────────────────────────────

def _run_pip(python_exe: str, packages: list, log, timeout: int = 900) -> bool:
    cmd = [python_exe, '-m', 'pip', 'install', '-U', '--disable-pip-version-check', '--no-input',
           '--timeout', '30', '--retries', '3'] + packages
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
    watchdog = threading.Timer(timeout, proc.kill)  # pip colgado (sin red) no debe bloquear para siempre
    watchdog.start()
    try:
        for line in proc.stdout:
            clean_line = strip_ansi(line.rstrip())
            if clean_line:
                log(f"  > {clean_line}")
    finally:
        watchdog.cancel()
    proc.wait()
    return proc.returncode == 0


def update_ytdlp_in_env(python_exe: str, log) -> bool:
    """Instala/actualiza yt-dlp con sus extras (yt-dlp-ejs, brotli, certifi... y curl-cffi, que permite "hacerse pasar"
    por un navegador real: TikTok bloquea a los clientes Python normales). Si algun extra falla, instala con menos extras."""
    if _run_pip(python_exe, ['yt-dlp[default,curl-cffi]'], log):
        return True
    log("  [AVISO] Fallo la instalacion con curl-cffi; reintentando sin el...")
    if _run_pip(python_exe, ['yt-dlp[default]'], log):
        return True
    log("  [AVISO] Fallo la instalacion con extras; reintentando solo con yt-dlp...")
    return _run_pip(python_exe, ['yt-dlp'], log)


# ── Suplantacion de navegador (curl_cffi) ───────────────────────────────────────
# TikTok responde con una pagina falsa de "Site Maintenance" a los clientes cuya huella TLS no es la de un navegador
# (pasa con el Python 3.12 / OpenSSL 3.0 de la version portable). yt-dlp lo resuelve imitando a Chrome, pero solo si
# curl_cffi esta instalado. Las copias que se actualizan por OTA solo reciben app.py, asi que la app lo instala sola.
_impersonate_lock = threading.Lock()
_impersonate_tried = False
_IMPERSONATE_HOSTS = ('tiktok.com', 'instagram.com')
_IMPERSONATE_PATTERNS = ('impersonat', 'unexpected response', 'unable to download webpage', 'http error 403',
                         'unable to extract')


def has_impersonation() -> bool:
    return importlib.util.find_spec('curl_cffi') is not None


def needs_impersonation_fix(message: str, url: str = '') -> bool:
    """True si el fallo huele a que el sitio nos tomo por un bot y curl_cffi aun no esta instalado."""
    if has_impersonation():
        return False
    low = (message or '').lower()
    return any(h in (url or '').lower() for h in _IMPERSONATE_HOSTS) or any(p in low for p in _IMPERSONATE_PATTERNS)


def ensure_impersonation(log=print, force_reload: bool = False) -> bool:
    """Instala curl_cffi (como maximo una vez por ejecucion) y recarga yt-dlp para que lo use.
    Devuelve True si quedo instalado y cargado, es decir, si merece reintentar la descarga."""
    global _impersonate_tried
    with _impersonate_lock:
        if _impersonate_tried:
            return False
        _impersonate_tried = True
        try:
            if not has_impersonation():
                log("[*] Instalando curl-cffi (permite descargar de TikTok/Instagram imitando a un navegador)...")
                if not update_ytdlp_in_env(sys.executable, lambda m: log(m)):
                    return False
            reload_ytdlp(force=force_reload)
            return has_impersonation()
        except Exception as e:
            log(f"[AVISO] No se pudo activar curl-cffi: {e}")
            return False


# ── Deno (resuelve los retos JavaScript de YouTube) ─────────────────────────────

def find_deno():
    for d in (os.path.join(BASE_DIR, 'deno'), os.path.join(ROOT_DIR, 'deno')):
        exe = os.path.join(d, 'deno.exe' if os.name == 'nt' else 'deno')
        if os.path.exists(exe):
            return exe
    return shutil.which('deno')


def required_deno_version() -> tuple:
    """Version minima de Deno que declara la yt-dlp instalada (extra 'deno'); (2, 0, 0) si no se puede saber."""
    try:
        from importlib import metadata
        for req in metadata.requires('yt-dlp') or []:
            m = re.match(r'deno\s*>=\s*([\d.]+)', req)
            if m:
                return tuple(int(x) for x in m.group(1).split('.'))
    except Exception:
        pass
    return (2, 0, 0)


def _deno_version(exe: str):
    try:
        res = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
        m = re.search(r'deno\s+(\d+)\.(\d+)\.(\d+)', res.stdout)
        if m:
            return tuple(int(x) for x in m.groups())
    except Exception:
        pass
    return None


def install_deno_windows(deno_dir: str, log) -> bool:
    os.makedirs(deno_dir, exist_ok=True)
    ver_res = subprocess.run(['curl.exe', '--ssl-revoke-best-effort', '-s', 'https://dl.deno.land/release-latest.txt'],
                             capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
    deno_version = ver_res.stdout.strip() if ver_res.returncode == 0 and ver_res.stdout.strip().startswith('v') else 'v2.9.8'
    log(f"  [*] Descargando Deno {deno_version}...")
    zip_path = os.path.join(deno_dir, 'deno.zip')
    dl_url = f'https://dl.deno.land/release/{deno_version}/deno-x86_64-pc-windows-msvc.zip'
    dl_res = subprocess.run(['curl.exe', '--ssl-revoke-best-effort', '-fLo', zip_path, dl_url],
                            capture_output=True, text=True, timeout=180, creationflags=NO_WINDOW)
    if dl_res.returncode == 0 and os.path.exists(zip_path):
        subprocess.run(['tar.exe', 'xf', zip_path, '-C', deno_dir], capture_output=True, timeout=60, creationflags=NO_WINDOW)
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return os.path.exists(os.path.join(deno_dir, 'deno.exe'))
    return False


def ensure_deno(log) -> None:
    """Comprueba que Deno exista y cumpla la version minima que pide yt-dlp; lo instala o actualiza si hace falta."""
    exe = find_deno()
    need = required_deno_version()
    if exe:
        have = _deno_version(exe)
        log(f"  [*] Deno encontrado: {'.'.join(map(str, have)) if have else 'version desconocida'} (minimo requerido {'.'.join(map(str, need))})")
        if have and have >= need:
            return
        log("  [!] Deno desactualizado; ejecutando 'deno upgrade'...")
        try:
            res = subprocess.run([exe, 'upgrade'], capture_output=True, text=True, timeout=240, creationflags=NO_WINDOW)
            log(f"  [{'OK' if res.returncode == 0 else 'AVISO'}] deno upgrade termino con codigo {res.returncode}")
        except Exception as e:
            log(f"  [AVISO] No se pudo actualizar Deno: {e}")
        return
    log("  [!] Deno no encontrado; se necesita para resolver las firmas de YouTube.")
    if os.name == 'nt':
        deno_dir = os.path.join(BASE_DIR, 'deno')
        try:
            if install_deno_windows(deno_dir, log):
                os.environ["PATH"] = deno_dir + os.pathsep + os.environ.get("PATH", "")
                log("  [OK] Deno instalado.")
            else:
                log("  [ERROR] No se pudo instalar Deno automaticamente.")
        except Exception as e:
            log(f"  [AVISO] No se pudo instalar Deno automaticamente: {e}")
    else:
        log("  [INFO] Instala Deno manualmente: curl -fsSL https://deno.land/install.sh | sh")


# ── Actualizacion del codigo de la aplicacion desde GitHub (OTA) ────────────────

def _sha256_variants(data: bytes) -> set:
    return {hashlib.sha256(data).hexdigest(), hashlib.sha256(data.replace(b'\r\n', b'\n')).hexdigest()}


def apply_ota_update(manifest: dict, log) -> bool:
    """Descarga el codigo nuevo a una carpeta temporal, lo verifica (hash, sintaxis y arranque de prueba) y solo
    entonces lo instala de forma atomica con copia de seguridad. Si algo falla no se toca ningun archivo."""
    base = GITHUB_RAW_BASE.rstrip('/')
    wanted = list(manifest.get('files') or {}) or list(OTA_DEFAULT_FILES)
    files = [f for f in OTA_DEFAULT_FILES if f in wanted and f != 'version.json']
    files.append('version.json')
    hashes = manifest.get('files') or {}

    staging = tempfile.mkdtemp(prefix='ota_')
    try:
        # 1) Descargar y verificar integridad
        for rel in files:
            res = requests.get(f"{base}/{rel}", params={'t': int(time.time())}, headers=REQUEST_HEADERS, timeout=30)
            if res.status_code != 200 or not res.content:
                raise RuntimeError(f"No se pudo descargar {rel} (HTTP {res.status_code}).")
            expected = hashes.get(rel)
            if expected and expected.lower() not in _sha256_variants(res.content):
                raise RuntimeError(f"{rel} no coincide con el hash publicado (la CDN de GitHub aun sirve una version anterior). Reintenta en unos minutos.")
            dest = os.path.join(staging, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as f:
                f.write(res.content)
            log(f"    [OK] Descargado y verificado: {rel}")

        # 2) Verificar que el codigo nuevo es valido
        py_compile.compile(os.path.join(staging, 'app.py'), cfile=os.path.join(staging, 'app.pyc'), doraise=True)
        for rel in ('templates/index.html', 'static/style.css'):
            if os.path.getsize(os.path.join(staging, rel)) < 1000:
                raise RuntimeError(f"{rel} descargado parece incompleto.")
        smoke = subprocess.run(
            [sys.executable, '-c', 'import app; print("SMOKE_OK", app.APP_VERSION)'],
            cwd=staging, capture_output=True, text=True, timeout=90, creationflags=NO_WINDOW,
            env={**os.environ, 'YTDL_SMOKE_TEST': '1', 'PYTHONDONTWRITEBYTECODE': '1'},
        )
        if 'SMOKE_OK' not in smoke.stdout:
            raise RuntimeError(f"El codigo nuevo no arranca: {(smoke.stderr or smoke.stdout).strip()[-300:]}")
        log("    [OK] El codigo nuevo supera la prueba de arranque.")

        # 3) Copia de seguridad del codigo actual
        backup_root = os.path.join(BASE_DIR, '_backup')
        backup_dir = os.path.join(backup_root, f"{get_app_version()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        for rel in files:
            src = os.path.join(BASE_DIR, rel)
            if os.path.exists(src):
                os.makedirs(os.path.dirname(os.path.join(backup_dir, rel)), exist_ok=True)
                shutil.copy2(src, os.path.join(backup_dir, rel))

        # 4) Instalar de forma atomica (version.json al final); si falla, restaurar
        done = []
        try:
            for rel in files:
                dest = os.path.join(BASE_DIR, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                tmp = dest + '.ota_new'
                shutil.copyfile(os.path.join(staging, rel), tmp)
                os.replace(tmp, dest)
                done.append(rel)
        except Exception:
            for rel in done:
                bak = os.path.join(backup_dir, rel)
                if os.path.exists(bak):
                    shutil.copyfile(bak, os.path.join(BASE_DIR, rel))
            raise
        if ROOT_DIR != BASE_DIR:
            try:
                shutil.copyfile(os.path.join(staging, 'version.json'), os.path.join(ROOT_DIR, 'version.json'))
            except OSError:
                pass

        # Conservar solo las 3 copias de seguridad mas recientes
        try:
            olds = sorted(os.listdir(backup_root))[:-3]
            for name in olds:
                shutil.rmtree(os.path.join(backup_root, name), ignore_errors=True)
        except OSError:
            pass
        return True
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def sync_code_to_portable(log) -> None:
    """Desde la version normal: copia el codigo actualizado a la carpeta portable vecina."""
    _, portable_dest = _find_portable_python()
    if not portable_dest or is_portable:
        return
    try:
        shutil.copy2(os.path.join(BASE_DIR, "app.py"), os.path.join(portable_dest, "app.py"))
        ver_src = os.path.join(BASE_DIR, "version.json")
        if os.path.exists(ver_src):
            shutil.copy2(ver_src, os.path.join(portable_dest, "version.json"))
            if os.path.dirname(portable_dest) and os.path.basename(portable_dest).lower() == 'software':
                shutil.copy2(ver_src, os.path.join(os.path.dirname(portable_dest), "version.json"))
        for folder in ["templates", "static"]:
            src = os.path.join(BASE_DIR, folder)
            if os.path.exists(src):
                shutil.copytree(src, os.path.join(portable_dest, folder), dirs_exist_ok=True)
        log("  [OK] Codigo sincronizado con la version Portable.")
    except Exception as e:
        log(f"  [AVISO] No se pudo sincronizar con Portable: {e}")


def update_ytdlp_everywhere(log) -> bool:
    """Actualiza yt-dlp en el entorno activo y en el portable vecino; limpia la cache y recarga el motor en caliente.
    Devuelve True si el motor ya esta recargado, False si quedo pendiente a que terminen las descargas en curso."""
    before = get_installed_ytdlp_version()
    log(f"[*] yt-dlp instalado antes: {before}")
    if not update_ytdlp_in_env(sys.executable, log):
        raise RuntimeError("pip no pudo actualizar yt-dlp en el entorno principal (revisa tu conexion a internet).")

    portable_python, _ = _find_portable_python()
    if portable_python and not is_portable:
        log("[*] Actualizando yt-dlp tambien en la version Portable...")
        if not update_ytdlp_in_env(portable_python, lambda m: log(m.replace('  >', '  [Portable]', 1))):
            log("  [AVISO] No se pudo actualizar yt-dlp en la version Portable.")

    log("[*] Limpiando cache de firmas y reproductores de YouTube...")
    log("  [OK] Cache eliminada." if clear_ytdlp_cache() else "  [AVISO] No habia cache que limpiar.")

    ensure_deno(log)

    after = get_installed_ytdlp_version()
    log(f"[*] yt-dlp instalado ahora: {after}")
    reloaded = reload_ytdlp()
    if reloaded:
        log(f"  [OK] Motor recargado en memoria: yt-dlp {get_loaded_ytdlp_version()} (sin reiniciar el programa).")
    else:
        log("  [INFO] Hay descargas en curso: el motor nuevo se cargara solo cuando terminen.")
    cfg = load_config()
    cfg['last_ytdlp_version'] = after
    save_config(cfg)
    with _remote_cache_lock:
        _remote_cache['ts'] = 0.0
    return reloaded


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

        log("\n[1/3] Comprobando actualizaciones de la aplicación en GitHub...")
        restart_required = False
        manifest = fetch_remote_manifest(timeout=10)
        current_app_ver = get_app_version()
        if manifest and manifest['version']:
            log(f"  > Versión instalada: v{current_app_ver} | Versión en GitHub: v{manifest['version']}")
            if is_newer_version(manifest['version'], current_app_ver):
                log(f"  [!] ¡Nueva versión de la app encontrada: v{manifest['version']}!")
                if manifest['changelog']:
                    log(f"  > Notas: {manifest['changelog']}")
                try:
                    if apply_ota_update(manifest, log):
                        log("  [✓] Archivos de la aplicación actualizados. Se reiniciará para aplicarlos.")
                        restart_required = True
                        update_state['restart_pending'] = True
                        sync_code_to_portable(log)
                except Exception as ota_err:
                    log(f"  [AVISO] No se actualizó el código de la app (se conserva la versión actual): {ota_err}")
            else:
                log("  [OK] El código de la aplicación ya está en su última versión.")
        else:
            log("  [AVISO] No se pudo consultar GitHub (¿sin conexión?). Se continúa con yt-dlp.")

        log("\n[2/3] Actualizando yt-dlp, cache y Deno...")
        reloaded = update_ytdlp_everywhere(log)

        log("\n[3/3] Verificación final...")
        new_version = get_loaded_ytdlp_version()
        log("\n=======================================================")
        log(f" [✓] ¡MANTENIMIENTO Y ACTUALIZACIÓN COMPLETADOS!")
        log(f" [✓] yt-dlp en uso: {new_version}")
        log(f" [✓] App: v{APP_VERSION}" + (" → se aplicará al reiniciar" if restart_required else ""))
        log("=======================================================\n")

        q.put({
            'status': 'complete',
            'version': new_version,
            'restart_required': restart_required,
            'engine_reloaded': reloaded,
            'message': (f'Actualizado: yt-dlp {new_version}.' + (' Reiniciando para aplicar la nueva versión de la app...' if restart_required else '')),
        })

    except Exception as e:
        log(f"\n[ERROR CRÍTICO] Falló la actualización: {e}")
        q.put({'status': 'error', 'message': str(e)})


# ── Auto-actualizacion en segundo plano ─────────────────────────────────────────
AUTO_UPDATE_INTERVAL = 6 * 3600
_heal_lock = threading.Lock()
_last_heal = 0.0
_HEAL_PATTERNS = (
    'unable to extract', 'nsig', 'signature', 'http error 403', 'requested format is not available',
    'player response', 'precondition check failed', 'no video formats', 'challenge', 'ejs',
    'unable to download video data', 'unable to download webpage', 'sabr', 'po token',
)


def looks_like_ytdlp_breakage(message: str) -> bool:
    low = (message or '').lower()
    return any(p in low for p in _HEAL_PATTERNS)


def auto_heal(reason: str) -> bool:
    """Autorreparacion ante un fallo tipico de cambios en YouTube: limpia la cache y, si hay una yt-dlp mas nueva,
    la instala y recarga. Como maximo una vez cada 15 minutos. Devuelve True si cambio algo y merece reintentar."""
    global _last_heal
    with _heal_lock:
        if time.time() - _last_heal < 900:
            return False
        _last_heal = time.time()
    print(f"[auto-heal] Fallo detectado ({reason[:120]}). Limpiando cache y buscando una yt-dlp mas nueva...")
    changed = clear_ytdlp_cache()
    try:
        latest = get_latest_pypi_ytdlp_version()
        if latest and is_newer_version(latest, get_installed_ytdlp_version()):
            update_ytdlp_everywhere(lambda m: print(f"[auto-heal] {m}"))
            changed = True
        elif not same_version(get_installed_ytdlp_version(), get_loaded_ytdlp_version()):
            reload_ytdlp()
            changed = True
    except Exception as e:
        print(f"[auto-heal] No se pudo actualizar yt-dlp: {e}")
    return changed


def is_idle(quiet_seconds: int = 45) -> bool:
    """True si no hay descargas ni consultas y el usuario lleva un rato sin interactuar con la web."""
    with queue_mgr.lock:
        busy = bool(queue_mgr.active_tasks or queue_mgr.pending_queue)
    return not busy and _ydl_inflight == 0 and (time.time() - _last_user_activity) >= quiet_seconds


def schedule_restart_when_idle():
    """Reinicia el programa en cuanto este inactivo (para aplicar codigo nuevo). La web se recarga sola."""
    def waiter():
        while not is_idle():
            time.sleep(5)
        request_restart()
    threading.Thread(target=waiter, daemon=True, name='restart-when-idle').start()


def run_auto_update_cycle() -> str:
    """Un ciclo de comprobacion/actualizacion automatica. Devuelve un resumen legible."""
    update_state['auto_running'] = True
    summary = []
    try:
        info = refresh_remote_info(force=True)
        log = lambda m: print(f"[auto-update] {m}")

        latest = info.get('pypi')
        installed = get_installed_ytdlp_version()
        if latest and is_newer_version(latest, installed):
            log(f"yt-dlp {installed} -> {latest}")
            update_ytdlp_everywhere(log)
            summary.append(f'yt-dlp {installed} -> {latest}')
        elif not same_version(installed, get_loaded_ytdlp_version()):
            reload_ytdlp()
            summary.append('motor yt-dlp recargado')
        elif importlib.util.find_spec('yt_dlp_ejs') is None and not load_config().get('ejs_install_tried'):
            # Instalaciones antiguas no traen yt-dlp-ejs (resolutor de retos JS de YouTube): instalarlo una vez
            cfg = load_config()
            cfg['ejs_install_tried'] = True
            save_config(cfg)
            log("Instalando extras de yt-dlp (yt-dlp-ejs, brotli, certifi...)")
            if update_ytdlp_in_env(sys.executable, log):
                reload_ytdlp()
                summary.append('extras de yt-dlp instalados')

        if not has_impersonation() and ensure_impersonation(log):
            summary.append('curl-cffi instalado (TikTok/Instagram)')

        manifest = info.get('ota')
        if manifest and manifest.get('version') and is_newer_version(manifest['version'], get_app_version()):
            log(f"App {get_app_version()} -> {manifest['version']}")
            if apply_ota_update(manifest, log):
                sync_code_to_portable(log)
                update_state['restart_pending'] = True
                summary.append(f"app -> {manifest['version']} (reinicio pendiente)")
                schedule_restart_when_idle()
    except Exception as e:
        summary.append(f'error: {e}')
    finally:
        update_state['auto_running'] = False
        update_state['last_check'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        update_state['last_result'] = '; '.join(summary) or 'Todo al dia'
    return update_state['last_result']


def auto_update_worker():
    time.sleep(20)
    while True:
        try:
            if load_config().get('auto_update', True):
                run_auto_update_cycle()
        except Exception as e:
            print(f"[auto-update] Error inesperado: {e}")
        time.sleep(AUTO_UPDATE_INTERVAL)


# ─── Extraccion / descarga con yt-dlp (cookies, autorreparacion) ────────────────

def ydl_extract(ydl_opts: dict, url: str, download: bool = False):
    """extract_info con dos redes de seguridad: reintenta sin cookies del navegador y, ante un fallo tipico de
    cambios en YouTube, limpia la cache / actualiza yt-dlp (auto_heal) y reintenta una vez."""
    opts = dict(ydl_opts)
    healed = False
    while True:
        try:
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=download)
        except Exception as e:
            if 'cookiesfrombrowser' in opts:
                opts.pop('cookiesfrombrowser', None)
                continue
            if needs_impersonation_fix(str(e), url) and ensure_impersonation(
                    log=lambda m: print(f"[curl-cffi] {m}"), force_reload=True):
                continue
            if not healed and looks_like_ytdlp_breakage(str(e)) and auto_heal(str(e)):
                healed = True
                continue
            raise


def ydl_download(ydl_opts: dict, url: str, notify=None):
    """Igual que ydl_extract pero para descargar. `notify(msg)` recibe avisos de los reintentos."""
    opts = dict(ydl_opts)
    healed = False
    while True:
        try:
            with YoutubeDL(opts) as ydl:
                return ydl.download([url])
        except DownloadCancelled:
            raise
        except Exception as e:
            if 'cookiesfrombrowser' in opts:
                if notify:
                    notify('[Aviso] Reintentando descarga sin cookies del navegador...')
                opts.pop('cookiesfrombrowser', None)
                continue
            if needs_impersonation_fix(str(e), url):
                if notify:
                    notify('[Aviso] El sitio bloqueo la descarga: instalando componente de compatibilidad (curl-cffi)...')
                if ensure_impersonation(log=lambda m: print(f"[curl-cffi] {m}"), force_reload=True):
                    continue
            if not healed and looks_like_ytdlp_breakage(str(e)) and auto_heal(str(e)):
                if notify:
                    notify('[Aviso] YouTube cambio algo: se limpio la cache / actualizo yt-dlp. Reintentando...')
                healed = True
                continue
            raise


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

    info = ydl_extract(ydl_opts, clean_url)

    if not info:
        raise RuntimeError("No se pudo obtener información del enlace proporcionado.")

    if 'entries' in info and info['entries']:
        first_entry = next((e for e in info['entries'] if e), None)
        if first_entry:
            info = first_entry

    # Clean up the title - often contains " - Topic" or other noise
    title = info.get('track') or info.get('title') or 'Unknown'
    artist = info.get('artist') or info.get('uploader') or info.get('channel') or 'Unknown'

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
    ydl_download(ydl_opts, clean_url, notify=(lambda m: q.put({'status': 'log', 'message': m})) if q else None)

    mp3_path = output_path + '.mp3'

    if not os.path.exists(mp3_path):
        # yt-dlp may have added an extra extension - find the file
        for f in os.listdir(CANCIONES_DIR):
            if f.startswith(filename) and f.endswith('.mp3'):
                mp3_path = os.path.join(CANCIONES_DIR, f)
                break

    if not os.path.exists(mp3_path):
        raise RuntimeError('No se pudo encontrar el archivo MP3 descargado.')

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
            resp = safe_fetch(thumbnail_url)
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

    info = ydl_extract(ydl_opts, clean_url)

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


# ── Formatos de salida universales ───────────────────────────────────────────
# Video: 'universal' = H.264 + AAC (+ subtitulos mov_text), lo unico que reproducen
# sin problemas iPhone/iPad/Mac (Fotos, Archivos, QuickTime), Android, TVs y WhatsApp.
VIDEO_OUTPUT_FORMATS = {
    'mp4_universal': {'ext': 'mp4', 'universal': True},
    'mov':           {'ext': 'mov', 'universal': True},
    'mp4':           {'ext': 'mp4', 'universal': False},   # maxima calidad (AV1/VP9/Opus posibles)
    'mkv':           {'ext': 'mkv', 'universal': False},
}
# Audio: clave -> (codec para FFmpegExtractAudio, extension final, calidad o None)
AUDIO_OUTPUT_FORMATS = {
    'mp3':  ('mp3', 'mp3', '192'),
    'm4a':  ('m4a', 'm4a', '192'),
    'wav':  ('wav', 'wav', None),
    'flac': ('flac', 'flac', None),
    'opus': ('opus', 'opus', None),
    'ogg':  ('vorbis', 'ogg', None),
}
DEFAULT_VIDEO_FORMAT = 'mp4_universal'
DEFAULT_AUDIO_FORMAT = 'mp3'
APPLE_VIDEO_CODECS = {'h264', 'hevc'}
APPLE_AUDIO_CODECS = {'aac', 'mp3', 'alac', 'ac3', 'eac3'}
VIDEO_FILE_EXTS = ('.mp4', '.mov', '.mkv', '.webm', '.m4v')


def resolve_output_format(download_type, output_format):
    """Devuelve una clave valida de formato segun el tipo de descarga."""
    if download_type == 'audio_only':
        return output_format if output_format in AUDIO_OUTPUT_FORMATS else DEFAULT_AUDIO_FORMAT
    return output_format if output_format in VIDEO_OUTPUT_FORMATS else DEFAULT_VIDEO_FORMAT


def probe_media(path):
    """ffprobe -> (streams, duracion_en_segundos)."""
    res = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries',
         'stream=index,codec_type,codec_name:stream_disposition=attached_pic:format=duration',
         '-of', 'json', path],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=120, creationflags=NO_WINDOW)
    data = json.loads(res.stdout or '{}')
    try:
        duration = float((data.get('format') or {}).get('duration') or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return data.get('streams') or [], duration


def plan_universal_conversion(streams, current_ext, target_ext):
    """Decide que hay que recodificar para que el archivo sea reproducible en iPhone.
    Devuelve None si ya es compatible, o un dict con lo que hay que convertir."""
    video = [s for s in streams if s.get('codec_type') == 'video'
             and not (s.get('disposition') or {}).get('attached_pic')]
    audio = [s for s in streams if s.get('codec_type') == 'audio']
    subs = [s for s in streams if s.get('codec_type') == 'subtitle']
    plan = {
        'video': any(s.get('codec_name') not in APPLE_VIDEO_CODECS for s in video),
        'audio': any(s.get('codec_name') not in APPLE_AUDIO_CODECS for s in audio),
        'subs': any(s.get('codec_name') != 'mov_text' for s in subs),
        'hevc': any(s.get('codec_name') == 'hevc' for s in video),
    }
    if not (plan['video'] or plan['audio'] or plan['subs']) and current_ext == target_ext:
        return None
    return plan


def make_universal(path, target_ext, notify=None, on_progress=None, should_cancel=None):
    """Convierte (solo lo necesario) a H.264 + AAC + subtitulos mov_text en MP4/MOV.
    Devuelve la ruta final del archivo compatible."""
    streams, duration = probe_media(path)
    current_ext = os.path.splitext(path)[1].lower().lstrip('.')
    plan = plan_universal_conversion(streams, current_ext, target_ext)
    final_path = os.path.splitext(path)[0] + '.' + target_ext
    if plan is None:
        return path

    codecs = ', '.join(sorted({s.get('codec_name', '?') for s in streams
                               if s.get('codec_type') in ('video', 'audio')}))
    if notify:
        if plan['video']:
            notify(f'[Compatibilidad] Codecs {codecs} no se reproducen en iPhone: convirtiendo a H.264/AAC (puede tardar)...')
        else:
            notify(f'[Compatibilidad] Ajustando audio/subtitulos ({codecs}) para iPhone sin recodificar la imagen...')

    tmp_path = os.path.splitext(path)[0] + '.convirtiendo.' + target_ext
    cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-nostats',
           '-progress', 'pipe:1', '-i', path,
           '-map', '0:V?', '-map', '0:a?', '-map', '0:s?', '-map_metadata', '0']
    if plan['video']:
        cmd += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                '-pix_fmt', 'yuv420p', '-profile:v', 'high']
    else:
        cmd += ['-c:v', 'copy'] + (['-tag:v', 'hvc1'] if plan['hevc'] else [])
    cmd += ['-c:a', 'aac', '-b:a', '192k'] if plan['audio'] else ['-c:a', 'copy']
    cmd += ['-c:s', 'mov_text', '-movflags', '+faststart', tmp_path]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', errors='replace',
                            creationflags=NO_WINDOW)
    # stderr en hilo aparte para que no se llene el buffer y bloquee ffmpeg
    err_lines = []
    err_thread = threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True)
    err_thread.start()
    last_pct = -1
    try:
        for line in proc.stdout:
            if should_cancel and should_cancel():
                proc.kill()
                raise DownloadCancelled('Descarga cancelada por el usuario')
            key, _, val = line.strip().partition('=')
            if key == 'out_time_us' and duration > 0 and on_progress:
                try:
                    pct = int(min(99, max(0, int(val) / 1e6 / duration * 100)))
                except ValueError:
                    continue
                if pct != last_pct:
                    last_pct = pct
                    on_progress(pct)
        proc.wait()
        err_thread.join(timeout=5)
    except BaseException:
        proc.kill()
        proc.wait()
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    finally:
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except Exception:
                pass

    if proc.returncode != 0 or not os.path.isfile(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise RuntimeError('ffmpeg no pudo convertir el video: ' + ''.join(err_lines[-5:]).strip())

    os.replace(tmp_path, final_path)
    if os.path.abspath(final_path) != os.path.abspath(path):
        try:
            os.remove(path)
        except OSError:
            pass
    return final_path


def _do_video_download(url, height, download_type, title, task_id,
                       audio_lang=None, embed_subs=False, sub_langs=None,
                       write_auto_sub=False, write_manual_sub=False,
                       start_time=None, end_time=None, editor_mode=False,
                       sponsorblock=False, split_chapters=False,
                       cookies_from_browser=None, output_format=None):
    """Background thread: download video/audio with progress via queue."""
    q = download_tasks[task_id]
    output_format = resolve_output_format(download_type, output_format)
    if download_type == 'audio_only':
        audio_codec, out_ext, audio_quality = AUDIO_OUTPUT_FORMATS[output_format]
        make_compatible = False
    else:
        out_ext = VIDEO_OUTPUT_FORMATS[output_format]['ext']
        # El modo editores tambien exige H.264/AAC real (no solo preferirlo)
        make_compatible = VIDEO_OUTPUT_FORMATS[output_format]['universal'] or (
            editor_mode and out_ext in ('mp4', 'mov'))

    try:
        clean_url = normalize_media_url(url)
        safe_title = sanitize_filename(title) if title else None

        # If no title provided, fetch it
        if not safe_title:
            t_opts = {**YDL_COMMON, 'quiet': True, 'skip_download': True, 'playlist_items': '1'}
            apply_browser_cookies(t_opts, cookies_from_browser)
            info = ydl_extract(t_opts, clean_url)
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

        has_embedded_subs = False
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
        elif make_compatible:
            # Respeta la resolucion elegida y, a igual resolucion, prefiere H.264/AAC
            # para no tener que recodificar (YouTube solo ofrece H.264 hasta 1080p)
            ydl_opts['format_sort'] = ['res', 'vcodec:h264', 'acodec:aac', 'proto:https', 'fps']

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
            extract_pp = {'key': 'FFmpegExtractAudio', 'preferredcodec': audio_codec}
            if audio_quality:
                extract_pp['preferredquality'] = audio_quality
            ydl_opts['postprocessors'].append(extract_pp)
        else:
            # MOV no admite todos los codecs de YouTube (Opus/VP9): se une en MKV y
            # make_universal() lo deja luego en MOV con H.264/AAC
            ydl_opts['merge_output_format'] = 'mkv' if out_ext == 'mov' else out_ext
            if download_type == 'video_only' and not make_compatible:
                # Sin merge, yt-dlp dejaria el contenedor original (a menudo .webm)
                ydl_opts['postprocessors'].append({'key': 'FFmpegVideoRemuxer', 'preferedformat': out_ext})
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
                has_embedded_subs = True

        if is_split:
            ydl_opts['postprocessors'].append({'key': 'FFmpegSplitChapters'})

        notify = lambda m: q.put({'status': 'log', 'message': m})
        try:
            ydl_download(ydl_opts, clean_url, notify=notify)
        except DownloadCancelled:
            raise
        except Exception as dl_err:
            # Un subtitulo defectuoso (p. ej. HTTP 429 en uno traducido) no debe impedir bajar el video
            if has_embedded_subs and 'subtitle' in str(dl_err).lower():
                notify('[Aviso] No se pudieron obtener los subtitulos; descargando el video sin subtitulos incrustados...')
                for key in ('writesubtitles', 'writeautomaticsub', 'subtitleslangs', 'subtitlesformat'):
                    ydl_opts.pop(key, None)
                ydl_opts['postprocessors'] = [pp for pp in ydl_opts['postprocessors']
                                              if pp.get('key') not in ('FFmpegSubtitlesConvertor', 'FFmpegEmbedSubtitle')]
                ydl_download(ydl_opts, clean_url, notify=notify)
            else:
                raise

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
                    t_resp = safe_fetch(t_url, timeout=12)
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
        def _universal(path, label=''):
            """Pasa un archivo a H.264/AAC/mov_text mostrando el avance de la conversion."""
            def on_progress(pct):
                msg = f'Convirtiendo para iPhone / compatibilidad universal{label}... {pct}%'
                if task_id:
                    queue_mgr.update_task_progress(task_id, pct, '', '', '', '')
                q.put({'status': 'processing', 'message': msg})
            try:
                return make_universal(
                    path, out_ext, notify=notify, on_progress=on_progress,
                    should_cancel=lambda: bool(task_id and task_id in queue_mgr.cancel_flags))
            except DownloadCancelled:
                raise
            except Exception as conv_err:
                notify(f'[Aviso] No se pudo convertir a formato compatible: {conv_err}. Se conserva el archivo original.')
                return path

        if is_split:
            final_path = chapter_dir
            final_filename = f"{safe_title} (Capítulos divididos)"
            if make_compatible:
                parts = sorted(f for f in os.listdir(chapter_dir) if f.lower().endswith(VIDEO_FILE_EXTS))
                for i, f in enumerate(parts, 1):
                    _universal(os.path.join(chapter_dir, f), f' ({i}/{len(parts)})')
        else:
            final_path = os.path.join(target_dir, f'{safe_title}.{out_ext}')

            if not os.path.exists(final_path):
                for f in os.listdir(target_dir):
                    if f.startswith(safe_title) and not f.endswith(('.part', '.ytdl', '.srt', '.vtt', '.json3', '.ttml')):
                        final_path = os.path.join(target_dir, f)
                        break
            if not os.path.isfile(final_path):
                raise RuntimeError('La descarga termino pero no se encontro el archivo resultante.')
            if make_compatible:
                final_path = _universal(final_path)
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
        # No dejar archivos a medias (.part / .ytdl) de una descarga cancelada
        try:
            for f in os.listdir(target_dir):
                if f.startswith(safe_title) and f.endswith(('.part', '.ytdl')):
                    os.remove(os.path.join(target_dir, f))
        except Exception:
            pass
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
    try:
        height = max(144, min(4320, int(data.get('height', 1080))))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Calidad no valida.'}), 400
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
    output_format = resolve_output_format(download_type, data.get('output_format'))

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
            'output_format': output_format,
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
        resp = safe_fetch(thumb_url)
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
            'download_url': f'/api/download-file/miniaturas/{quote(filename)}'
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


class SubtitleRateLimited(RuntimeError):
    """YouTube respondio HTTP 429 al pedir el subtitulo."""


def _fetch_track(clean_url, lang, sub_type, cookies_from_browser, attempts=3):
    """Descarga una pista de subtitulos con yt-dlp (soporta HLS y reintenta ante 429) y devuelve (titulo, texto)."""
    rate_limited = False
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

            rate_limited = rate_limited or any('429' in m for m in logger.messages)

        if attempt < attempts - 1:
            time.sleep(3 * (attempt + 1))

    if rate_limited:
        raise SubtitleRateLimited('YouTube limito temporalmente la descarga de este subtitulo (HTTP 429).')
    raise RuntimeError('No se pudo descargar el subtitulo. Reintenta en unos minutos o prueba con otro idioma.')


def _find_original_auto_lang(clean_url, cookies_from_browser):
    """Devuelve el idioma original de los subtitulos automaticos (la pista sin 'tlang=' en su URL)."""
    ydl_opts = {**YDL_COMMON, 'quiet': True, 'no_warnings': True, 'skip_download': True, 'playlist_items': '1'}
    apply_browser_cookies(ydl_opts, cookies_from_browser)
    info = ydl_extract(ydl_opts, clean_url)
    if info.get('entries'):
        info = next((e for e in info['entries'] if e), info)

    auto = info.get('automatic_captions') or {}
    for code, formats in auto.items():
        if any(f.get('url') and 'tlang=' not in f['url'] for f in formats):
            return code
    return None


def _translate_target_code(lang: str) -> str:
    """Convierte un codigo de idioma de YouTube (es-419, zh-Hans...) al que entiende el traductor."""
    special = {'zh-hans': 'zh-CN', 'zh-hant': 'zh-TW', 'zh-cn': 'zh-CN', 'zh-tw': 'zh-TW', 'zh-hk': 'zh-TW'}
    return special.get(lang.lower(), lang.split('-')[0])


def _translate_text(text: str, target_lang: str) -> str:
    """Traduce texto plano linea por linea con el traductor web de Google (sin clave de API)."""
    target = _translate_target_code(target_lang)
    # Unir los renglones cortos de los subtitulos en frases para que la traduccion tenga contexto
    lines, current = [], ''
    for raw in text.split('\n'):
        current = f'{current} {raw.strip()}'.strip()
        if len(current) > 400 or (len(current) >= 40 and re.search(r'[.?!…。]["\')\]]?$', current)):
            lines.append(current)
            current = ''
    if current:
        lines.append(current)
    out = []
    batch, batch_len = [], 0

    def flush():
        if not batch:
            return
        data = [('q', l) for l in batch]
        result = None
        for url, params in (
            ('https://clients5.google.com/translate_a/t', {'client': 'dict-chrome-ex', 'sl': 'auto', 'tl': target}),
            ('https://translate.googleapis.com/translate_a/t', {'client': 'gtx', 'sl': 'auto', 'tl': target, 'dt': 't'}),
        ):
            try:
                resp = requests.post(url, params=params, data=data, headers=REQUEST_HEADERS, timeout=30)
                if resp.status_code == 200:
                    parsed = resp.json()
                    if len(batch) == 1 and parsed and isinstance(parsed[0], str):
                        parsed = [parsed]
                    if len(parsed) == len(batch):
                        result = [p[0] if isinstance(p, list) else p for p in parsed]
                        break
            except (requests.RequestException, ValueError):
                continue
        if result is None:
            raise RuntimeError('No se pudo traducir el texto.')
        out.extend(result)
        batch.clear()

    for line in lines:
        if batch and (len(batch) >= 80 or batch_len + len(line) > 3500):
            flush()
            batch_len = 0
        batch.append(line)
        batch_len += len(line)
    flush()
    return '\n'.join(out)


def _download_transcript(clean_url, lang, sub_type, cookies_from_browser):
    """Devuelve (titulo, texto, idioma_guardado, nota). Si YouTube bloquea (429) un subtitulo
    auto-traducido, descarga el idioma original y lo traduce; si tampoco se puede traducir,
    entrega la transcripcion en el idioma original."""
    try:
        title, text = _fetch_track(clean_url, lang, sub_type, cookies_from_browser,
                                   attempts=1 if sub_type != 'manual' else 3)
        return title, text, lang, ''
    except SubtitleRateLimited:
        if sub_type == 'manual':
            raise RuntimeError('YouTube limito temporalmente la descarga (HTTP 429). Espera uno o dos minutos y reintenta.')

    original = _find_original_auto_lang(clean_url, cookies_from_browser)
    if not original or original == lang:
        raise RuntimeError(
            'YouTube limito temporalmente la descarga de este subtitulo (HTTP 429). '
            'Espera uno o dos minutos y reintenta.'
        )
    try:
        title, text = _fetch_track(clean_url, original, 'auto', cookies_from_browser)
    except SubtitleRateLimited:
        raise RuntimeError(
            'YouTube limito temporalmente la descarga de subtitulos (HTTP 429). '
            'Espera uno o dos minutos y reintenta.'
        )

    original_code = original[:-5] if original.endswith('-orig') else original
    original_name = get_lang_name(original_code)
    try:
        translated = _translate_text(text, lang)
    except Exception:
        return title, text, original_code, (
            f'YouTube bloqueo el subtitulo traducido, asi que se guardo la transcripcion en el idioma '
            f'original ({original_name}).'
        )
    return title, translated, lang, (
        f'YouTube bloqueo el subtitulo traducido: se descargo el original ({original_name}) y se tradujo automaticamente.'
    )


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
            title, plain_text, saved_lang, note = _download_transcript(clean_url, lang, sub_type, cookies_from_browser)
        except (ValueError, RuntimeError) as e:
            return jsonify({'success': False, 'error': str(e)})

        filename = f'{title}_subtitulos_{saved_lang}.txt'
        filepath = os.path.join(SUBTITULOS_DIR, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(plain_text)

        return jsonify({
            'success': True,
            'filename': filename,
            'path': filepath,
            'download_url': f'/api/download-file/subtitulos/{quote(filename)}',
            'preview': plain_text[:3000],
            'total_length': len(plain_text),
            'note': note,
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
        try:
            inside_root = os.path.commonpath([cand, root_abs]) == root_abs
        except ValueError:  # otra unidad (C: vs D:)
            inside_root = False
        raw_path = cand if (inside_root and os.path.isdir(cand)) else VIDEOS_DIR

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
    """Estado de yt-dlp y de la app (con cache de 10 min; ?force=1 la ignora)."""
    info = refresh_remote_info(force=request.args.get('force') == '1')
    loaded = get_loaded_ytdlp_version()
    on_disk = get_installed_ytdlp_version()
    latest = info.get('pypi')
    ytdlp_update = bool(latest and is_newer_version(latest, on_disk))

    app_ver = get_app_version()
    manifest = info.get('ota') or {}
    app_update_available = bool(manifest.get('version') and is_newer_version(manifest['version'], app_ver))
    restart_required = bool(update_state['restart_pending'] or is_newer_version(app_ver, APP_VERSION))
    engine_stale = not same_version(loaded, on_disk)
    update_available = bool(ytdlp_update or app_update_available or engine_stale)

    return jsonify({
        'success': True,
        'app_version': app_ver,
        'code_version': APP_VERSION,
        'installed_version': loaded,
        'disk_version': on_disk,
        'latest_version': latest or on_disk,
        'update_available': update_available,
        'ytdlp_update_available': ytdlp_update,
        'app_update_available': app_update_available,
        'engine_stale': engine_stale,
        'engine_reload_pending': _ydl_reload_pending,
        'restart_required': restart_required,
        'remote_app_version': manifest.get('version') or app_ver,
        'has_portable': True,
        'portable_version': app_ver,
        'portable_ytdlp_version': info.get('portable') or on_disk,
        'auto_update': bool(load_config().get('auto_update', True)),
        'last_auto_check': update_state['last_check'],
        'last_auto_result': update_state['last_result'],
        'boot_id': BOOT_ID,
    })


_update_lock = threading.Lock()
_current_update_task = {'id': None}


@app.route('/api/system/update', methods=['POST'])
def system_update():
    """Trigger system update in background thread (una sola a la vez)."""
    with _update_lock:
        existing = _current_update_task['id']
        with tasks_lock:
            if existing and existing in update_tasks:
                return jsonify({'success': True, 'task_id': existing, 'already_running': True})
            task_id = str(uuid.uuid4())
            update_tasks[task_id] = queue.Queue()
        _current_update_task['id'] = task_id

    def runner():
        try:
            _run_system_update(task_id)
        finally:
            with _update_lock:
                if _current_update_task['id'] == task_id:
                    _current_update_task['id'] = None

    threading.Thread(target=runner, daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id})


@app.route('/api/system/auto-update', methods=['GET', 'POST'])
def system_auto_update():
    """Consulta o cambia la auto-actualizacion automatica en segundo plano."""
    cfg = load_config()
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        cfg['auto_update'] = bool(data.get('enabled', True))
        save_config(cfg)
    return jsonify({'success': True, 'auto_update': bool(cfg.get('auto_update', True)),
                    'last_check': update_state['last_check'], 'last_result': update_state['last_result']})


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


_discord_lock = threading.Lock()
_discord_last_post = 0.0


def _fence(text: str, limit: int) -> str:
    """Texto seguro para un bloque de codigo de Discord (sin cerrar el bloque ni mencionar a nadie)."""
    return str(text).replace('```', "'''").replace('@', '@​')[:limit]


def post_discord(payload: dict) -> tuple:
    """Envia un embed al webhook. Devuelve (enviado, error). Sin menciones, con limite de frecuencia y reintento ante 429."""
    global _discord_last_post
    if not BUG_REPORT_WEBHOOK or BUG_REPORT_WEBHOOK.startswith("PEGA_AQUI"):
        return False, 'El webhook de Discord no esta configurado (falta bug_webhook.txt).'
    with _discord_lock:
        if time.time() - _discord_last_post < 3:
            return False, 'Espera unos segundos antes de enviar otro mensaje.'
        _discord_last_post = time.time()
    payload = {**payload, 'username': 'Descargador Universal', 'allowed_mentions': {'parse': []}}
    try:
        for attempt in range(2):
            resp = requests.post(BUG_REPORT_WEBHOOK, json=payload, headers=REQUEST_HEADERS, timeout=10)
            if resp.status_code in (200, 204):
                return True, None
            if resp.status_code == 429 and attempt == 0:
                try:
                    wait = float(resp.json().get('retry_after', 1))
                except Exception:
                    wait = 1.0
                if wait <= 5:
                    time.sleep(wait)
                    continue
            return False, f"Discord HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, str(e)
    return False, 'No se pudo enviar el mensaje.'


def _append_local_log(text: str) -> str:
    log_file = os.path.join(ROOT_DIR, 'ultimo_reporte_error.log')
    try:
        if os.path.exists(log_file) and os.path.getsize(log_file) > 512 * 1024:
            os.replace(log_file, log_file + '.old')  # evitar que el log crezca sin limite
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(text)
    except Exception as e:
        print(f"[ERROR] No se pudo escribir en ultimo_reporte_error.log: {e}")
    return log_file


@app.route('/api/system/report-bug', methods=['POST'])
def report_bug():
    """Log bug report to local file and dispatch embed to Discord webhook."""
    data = request.get_json(silent=True) or {}
    url = str(data.get('url') or 'N/A').strip()
    error_msg = str(data.get('error') or 'Error no especificado').strip()
    user_note = str(data.get('note') or '').strip()
    diagnostic = data.get('diagnostic') or {}

    timestamp_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    entry = f"\n{'='*60}\nREPORTE DE FALLO: {timestamp_str}\nVersión: {APP_VERSION}\nURL: {url}\nError: {error_msg}\n"
    if user_note:
        entry += f"Nota del usuario: {user_note}\n"
    if diagnostic:
        entry += f"Diagnóstico: {json.dumps(diagnostic, ensure_ascii=False, indent=2)}\n"
    entry += f"{'='*60}\n"
    log_file = _append_local_log(entry)

    fields = [
        {"name": "📦 Versión de la App", "value": f"`{APP_VERSION}`", "inline": True},
        {"name": "🌐 URL Reportada", "value": f"`{_fence(url, 300)}`" if url != 'N/A' else "`N/A`", "inline": False},
        {"name": "⚠️ Mensaje de Error", "value": f"```{_fence(error_msg, 900)}```", "inline": False},
    ]
    if user_note:
        fields.append({"name": "💬 Nota del Usuario", "value": _fence(user_note, 500), "inline": False})
    if diagnostic and isinstance(diagnostic, dict):
        diag_summary = (
            f"• Tipo: {diagnostic.get('type', 'desconocido')}\n"
            f"• Título: {diagnostic.get('title', 'N/A')}\n"
            f"• Sugerencia: {diagnostic.get('suggestion', 'N/A')}"
        )
        fields.append({"name": "🔍 Diagnóstico Asistido", "value": _fence(diag_summary, 800), "inline": False})

    webhook_sent, webhook_error = post_discord({
        "content": "🚨 **Nuevo reporte de error recibido en Descargador Universal**",
        "embeds": [{
            "title": "Bug Report - Descargador Universal",
            "description": "Un usuario ha presionado el botón de reporte de fallos.",
            "color": 15158332,
            "fields": fields,
            "footer": {"text": f"Descargador Universal v{APP_VERSION} • {timestamp_str}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    })

    return jsonify({
        'success': True,
        'message': ('Reporte de fallo enviado correctamente. ¡Gracias por avisar a Alex!' if webhook_sent
                    else 'El reporte se guardó en el archivo local, pero no se pudo enviar a Discord.'),
        'webhook_sent': webhook_sent,
        'webhook_error': webhook_error,
        'local_log': log_file,
    })


@app.route('/api/system/feedback', methods=['POST'])
def send_feedback():
    """Send user suggestion, idea, comment, or question directly to Alex's Discord webhook."""
    data = request.get_json(silent=True) or {}
    msg_type = str(data.get('type') or '💡 Sugerencia').strip()[:60]
    sender = (str(data.get('sender') or 'Usuario').strip() or 'Usuario')[:60]
    message = str(data.get('message') or '').strip()
    url = (str(data.get('url') or 'N/A').strip() or 'N/A')

    if not message:
        return jsonify({'success': False, 'error': 'Por favor escribe un mensaje o sugerencia.'}), 400

    timestamp_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _append_local_log(
        f"\n{'='*60}\nFEEDBACK / MENSAJE DE USUARIO: {timestamp_str}\nTipo: {msg_type}\n"
        f"Remitente: {sender}\nURL: {url}\nMensaje:\n{message}\n{'='*60}\n"
    )

    # Discord color: Error rojo, Sugerencia/Idea verde, resto violeta
    color = 10181046
    if any(k in msg_type.lower() for k in ['error', 'fallo', 'bug']):
        color = 15158332
    elif any(k in msg_type.lower() for k in ['sugerencia', 'idea', 'nueva']):
        color = 3066993

    fields = [
        {"name": "🏷️ Tipo", "value": f"`{_fence(msg_type, 60)}`", "inline": True},
        {"name": "👤 Remitente", "value": f"`{_fence(sender, 60)}`", "inline": True},
        {"name": "📦 Versión", "value": f"`v{APP_VERSION}`", "inline": True},
    ]
    if url != 'N/A':
        fields.append({"name": "🌐 Enlace Relacionado", "value": f"`{_fence(url, 350)}`", "inline": False})
    fields.append({"name": "💬 Mensaje / Sugerencia", "value": _fence(message, 1024), "inline": False})

    webhook_sent, webhook_error = post_discord({
        "content": "📬 **Nuevo mensaje recibido para Alex en Descargador Universal**",
        "embeds": [{
            "title": f"{msg_type} - Descargador Universal",
            "color": color,
            "fields": fields,
            "footer": {"text": f"Descargador Universal v{APP_VERSION} • {timestamp_str}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    })

    if not webhook_sent:
        return jsonify({'success': False, 'error': f'No se pudo enviar a Discord: {webhook_error}',
                        'webhook_sent': False, 'webhook_error': webhook_error}), 502
    return jsonify({
        'success': True,
        'message': '¡Tu mensaje ha sido enviado exitosamente al Discord de Alex!',
        'webhook_sent': True,
        'webhook_error': None,
    })


_server = None            # servidor HTTP en marcha (para poder pararlo limpiamente)
_restart_requested = threading.Event()
CURRENT_PORT = int(os.environ.get('YTDL_PORT', DEFAULT_PORT))


def request_restart():
    """Pide reiniciar el programa: se detiene el servidor y main() relanza el proceso."""
    _restart_requested.set()
    if _server is not None:
        threading.Thread(target=_server.shutdown, daemon=True).start()


@app.route('/api/system/restart', methods=['POST'])
def system_restart():
    """Reinicia el proceso Python (recarga todo el codigo y yt-dlp) sin chocar con el puerto."""
    threading.Timer(0.5, request_restart).start()
    return jsonify({'success': True, 'message': 'Reiniciando motor en segundo plano...', 'boot_id': BOOT_ID})


@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    """Shut down the Flask server gracefully without dropping the HTTP response."""
    def stop_server():
        if _server is not None:
            _server.shutdown()
        else:
            os._exit(0)

    threading.Timer(0.5, lambda: threading.Thread(target=stop_server, daemon=True).start()).start()
    return jsonify({'success': True, 'status': 'shutting_down', 'message': 'Servidor apagándose ordenadamente...'})


# ─── Entry Point ────────────────────────────────────────────────────────────────

def _port_is_free(port: int) -> bool:
    """True si nadie escucha en el puerto (bind exclusivo: en Windows detecta tambien instancias antiguas)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _is_our_app(port: int) -> bool:
    try:
        r = requests.get(f'http://127.0.0.1:{port}/api/ping', timeout=2)
        return r.status_code == 200 and r.json().get('app') == APP_ID
    except Exception:
        return False


def _pick_port(preferred: int, restarting: bool):
    """Devuelve el puerto a usar. Tras un reinicio espera a que el antiguo proceso suelte el suyo."""
    # Tras un reinicio (o si el proceso anterior se esta cerrando) esperar a que suelte el puerto en vez de saltar a otro
    deadline = time.time() + (25 if restarting else 4)
    while time.time() < deadline:
        if _port_is_free(preferred):
            return preferred
        time.sleep(0.4)
    for port in range(preferred, preferred + 10):
        if _port_is_free(port):
            return port
    return None


def _make_server(port: int):
    from werkzeug.serving import ThreadedWSGIServer

    class ExclusiveServer(ThreadedWSGIServer):
        allow_reuse_address = False  # en Windows SO_REUSEADDR permite que dos programas compartan el puerto

        def server_bind(self):
            if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    srv = ExclusiveServer('127.0.0.1', port, app)
    srv.daemon_threads = True
    return srv


def main():
    global _server, CURRENT_PORT
    import webbrowser
    restarting = os.environ.get('YTDL_RESTART') == '1'
    preferred = int(os.environ.get('YTDL_PORT', DEFAULT_PORT))

    if not restarting and not _port_is_free(preferred) and _is_our_app(preferred):
        print(f"\n  [i] Descargador Universal ya esta abierto en http://localhost:{preferred}")
        if os.environ.get("YTDLP_NO_BROWSER") != "1":
            webbrowser.open(f'http://localhost:{preferred}')
        return 0

    port = _pick_port(preferred, restarting)
    if port is None:
        print(f"\n  [ERROR] No hay ningun puerto libre entre {preferred} y {preferred + 9}.")
        return 1
    CURRENT_PORT = port
    os.environ['YTDL_PORT'] = str(port)

    # Si yt-dlp cambio desde la ultima vez (p. ej. se actualizo con el programa cerrado), la cache puede estar obsoleta
    cfg = load_config()
    loaded = get_loaded_ytdlp_version()
    if not same_version(cfg.get('last_ytdlp_version') or '', loaded):
        clear_ytdlp_cache()
        cfg['last_ytdlp_version'] = loaded
        save_config(cfg)

    _server = _make_server(port)
    print(f"\n  [*] YouTube Downloader v{APP_VERSION}  (yt-dlp {loaded})")
    print(f"  [>] Canciones se guardaran en: {CANCIONES_DIR}")
    print(f"  [>] Videos se guardaran en:    {VIDEOS_DIR}")
    print(f"  [>] Abriendo http://localhost:{port} ...\n")
    if port != preferred:
        print(f"  [i] El puerto {preferred} estaba ocupado; se usa el {port}.\n")
    if os.environ.get("YTDLP_NO_BROWSER") != "1" and not restarting:
        threading.Timer(1.5, lambda: webbrowser.open(f'http://localhost:{port}')).start()

    threading.Thread(target=auto_update_worker, daemon=True, name='auto-update').start()

    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            _server.server_close()
        except Exception:
            pass

    if _restart_requested.is_set():
        print("\n  [*] Reiniciando para aplicar los cambios...\n")
        if os.environ.get('YTDL_SUPERVISED') == '1':
            return RESTART_EXIT_CODE  # el .bat relanza el programa
        env = {**os.environ, 'YTDL_RESTART': '1', 'YTDLP_NO_BROWSER': '1', 'YTDL_PORT': str(port)}
        # Sin .bat que lo supervise: lanzar el proceso nuevo y esperar a que termine para que la consola siga siendo la misma
        return subprocess.call([sys.executable] + sys.argv, env=env)
    return 0


if __name__ == '__main__':
    sys.exit(main())
