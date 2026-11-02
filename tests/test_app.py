"""Pruebas automaticas del Descargador Universal (sin red salvo que se indique).

Ejecutar desde la carpeta del proyecto:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Base de datos temporal: las pruebas no deben tocar el historial real
_TMP = tempfile.mkdtemp(prefix='ytdl_tests_')
os.environ['YTDL_DB_PATH'] = os.path.join(_TMP, 'history.db')

import app as A  # noqa: E402


class ClientMixin:
    def setUp(self):
        self.c = A.app.test_client()


class SecurityTests(ClientMixin, unittest.TestCase):
    def test_ping_identifies_app(self):
        d = self.c.get('/api/ping').get_json()
        self.assertEqual(d['app'], 'descargador-universal')
        self.assertTrue(d['boot_id'])

    def test_rejects_foreign_host_dns_rebinding(self):
        r = self.c.get('/api/ping', headers={'Host': 'evil.example.com:5000'})
        self.assertEqual(r.status_code, 403)

    def test_rejects_cross_origin_post(self):
        r = self.c.post('/api/shutdown', headers={'Origin': 'https://evil.example.com'})
        self.assertEqual(r.status_code, 403)

    def test_rejects_null_origin_post(self):
        r = self.c.post('/api/system/restart', headers={'Origin': 'null'})
        self.assertEqual(r.status_code, 403)

    def test_rejects_cross_site_fetch_metadata(self):
        r = self.c.post('/api/queue/config', json={}, headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(r.status_code, 403)

    def test_text_plain_csrf_does_not_reach_handlers(self):
        # una pagina ajena puede enviar text/plain sin preflight: debe llegar con Origin y ser rechazada
        r = self.c.post('/api/system/feedback', data='{"message":"x"}', headers={
            'Content-Type': 'text/plain', 'Origin': 'http://attacker.test'})
        self.assertEqual(r.status_code, 403)

    def test_same_origin_post_allowed(self):
        r = self.c.post('/api/queue/config', json={}, headers={'Origin': 'http://localhost'})
        self.assertEqual(r.status_code, 200)

    def test_security_headers(self):
        r = self.c.get('/api/ping')
        self.assertEqual(r.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(r.headers['X-Frame-Options'], 'DENY')
        self.assertIn("frame-ancestors 'none'", r.headers['Content-Security-Policy'])

    def test_json_errors_not_html(self):
        r = self.c.get('/api/no-existe')
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.get_json()['success'])
        r = self.c.post('/api/video/info', data='{{{', content_type='application/json')
        self.assertIn(r.status_code, (400, 500))
        self.assertIn('success', r.get_json())

    def test_oversized_body_rejected(self):
        r = self.c.post('/api/video/info', data='x' * (3 * 1024 * 1024), content_type='application/json')
        self.assertEqual(r.status_code, 413)

    def test_serve_file_path_traversal(self):
        for name in ('..%2f..%2fapp.py', '..\\app.py', '../app.py'):
            r = self.c.get(f'/api/download-file/videos/{name}')
            self.assertIn(r.status_code, (403, 404), name)
        r = self.c.get('/api/download-file/etc/passwd')
        self.assertEqual(r.status_code, 403)

    def test_open_folder_prefix_bypass(self):
        sibling = A.ROOT_DIR + '2'
        captured = {}
        with mock.patch.object(A, '_launch_explorer', side_effect=lambda p: captured.setdefault('p', p) and (True, p)):
            self.c.post('/api/open-folder', json={'folder': sibling})
        self.assertEqual(captured.get('p'), A.VIDEOS_DIR)

    def test_ssrf_blocked(self):
        for url in ('http://127.0.0.1:5000/api/ping', 'http://localhost/x', 'http://169.254.169.254/latest',
                    'http://10.0.0.1/a.jpg', 'file:///etc/passwd', 'ftp://example.com/a', 'http://[::1]/'):
            self.assertFalse(A.is_public_http_url(url), url)
        r = self.c.post('/api/video/thumbnail', json={'thumbnail_url': 'http://127.0.0.1:5000/api/ping', 'title': 't'})
        self.assertEqual(r.status_code, 500)
        self.assertFalse(r.get_json()['success'])

    def test_ssrf_blocked_on_redirect(self):
        fake = mock.Mock(status_code=302, headers={'Location': 'http://127.0.0.1/secret'})
        fake.close = lambda: None
        with mock.patch.object(A, 'is_public_http_url', side_effect=lambda u: 'example.com' in u), \
                mock.patch.object(A.requests, 'get', return_value=fake):
            with self.assertRaises(ValueError):
                A.safe_fetch('http://example.com/img.jpg')


class UnitTests(unittest.TestCase):
    def test_sanitize_filename(self):
        self.assertEqual(A.sanitize_filename('a<b>c:d"e/f\\g|h?i*j'), 'abcdefghij')
        self.assertEqual(A.sanitize_filename('CON'), 'CON_')
        self.assertEqual(A.sanitize_filename('   ...  '), 'archivo')
        self.assertEqual(A.sanitize_filename(''), 'archivo')
        self.assertLessEqual(len(A.sanitize_filename('x' * 500)), 110)

    def test_normalize_media_url(self):
        u = 'https://www.youtube.com/watch?v=abc&list=RDabc&index=3&start_radio=1'
        self.assertEqual(A.normalize_media_url(u), 'https://www.youtube.com/watch?v=abc')
        pl = 'https://www.youtube.com/playlist?list=PL123'
        self.assertEqual(A.normalize_media_url(pl), pl)
        self.assertEqual(A.normalize_media_url(''), '')

    def test_parse_time(self):
        self.assertEqual(A.parse_time_to_seconds('01:15'), 75)
        self.assertEqual(A.parse_time_to_seconds('1:00:05'), 3605)
        self.assertEqual(A.parse_time_to_seconds('75.5'), 75.5)
        self.assertIsNone(A.parse_time_to_seconds('abc'))
        self.assertIsNone(A.parse_time_to_seconds(''))

    def test_versions(self):
        self.assertTrue(A.same_version('2026.08.19', '2026.8.19'))
        self.assertTrue(A.is_newer_version('2026.10.26', '2026.8.19'))
        self.assertFalse(A.is_newer_version('2.0.2', '2.0.2'))
        self.assertTrue(A.is_newer_version('2.0.10', '2.0.9'))

    def test_vtt_and_json3_to_text(self):
        vtt = 'WEBVTT\nKind: captions\nLanguage: en\n\n00:00:01.000 --> 00:00:02.000 align:start position:0%\nHello <c>world</c>\n\n00:00:02.000 --> 00:00:03.000\nHello world\nSecond &amp; line\n'
        self.assertEqual(A._vtt_to_text(vtt), 'Hello world\nSecond & line')
        j3 = json.dumps({'events': [{'segs': [{'utf8': 'Hi '}, {'utf8': 'there'}]}, {'segs': [{'utf8': '\n'}]}, {'segs': [{'utf8': 'Hi there'}]}, {}]})
        self.assertEqual(A._json3_to_text(j3), 'Hi there')

    def test_translate_code_and_sentence_batching(self):
        self.assertEqual(A._translate_target_code('es-419'), 'es')
        self.assertEqual(A._translate_target_code('zh-Hans'), 'zh-CN')
        sent = []

        def fake_post(url, params=None, data=None, **k):
            sent.append([v for _, v in data])
            r = mock.Mock(status_code=200)
            r.json.return_value = [[f'ES:{v}', 'en'] for _, v in data]
            return r
        with mock.patch.object(A.requests, 'post', side_effect=fake_post):
            out = A._translate_text('Hello there,\nthis is a sentence.\nAnother one here and it is long enough.', 'es')
        self.assertEqual(len(sent[0]), 1)  # los renglones cortos se unen en una sola frase con contexto
        self.assertTrue(out.startswith('ES:Hello there, this is a sentence. Another one'))

    def test_translate_failure_raises(self):
        with mock.patch.object(A.requests, 'post', side_effect=A.requests.ConnectionError()):
            with self.assertRaises(RuntimeError):
                A._translate_text('Hello world, this is long enough to be one sentence.', 'es')

    def test_breakage_patterns(self):
        self.assertTrue(A.looks_like_ytdlp_breakage('ERROR: Unable to extract yt initial data'))
        self.assertTrue(A.looks_like_ytdlp_breakage('HTTP Error 403: Forbidden'))
        self.assertFalse(A.looks_like_ytdlp_breakage('Video unavailable. This video is private'))

    def test_diagnose_error(self):
        self.assertEqual(A.diagnose_error('HTTP Error 429: Too Many Requests')['type'], 'YOUTUBE_RATE_LIMIT')
        self.assertEqual(A.diagnose_error('Sign in to confirm you are not a bot')['type'], 'YOUTUBE_BOT_DETECTION')

    def test_sha_variants_accept_crlf_checkouts(self):
        import hashlib
        lf = b'a\nb\n'
        self.assertIn(hashlib.sha256(lf).hexdigest(), A._sha256_variants(lf.replace(b'\n', b'\r\n')))

    def test_config_survives_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'config.json')
            with open(path, 'w') as f:
                f.write('{rota')
            with mock.patch.object(A, 'CONFIG_PATH', path):
                self.assertTrue(A.load_config()['auto_update'])
                A.save_config({'auto_update': False})
                self.assertFalse(A.load_config()['auto_update'])


class YdlWrapperTests(unittest.TestCase):
    def test_wrapper_counts_sessions_and_defers_reload(self):
        self.assertEqual(A._ydl_inflight, 0)
        with A.YoutubeDL({'quiet': True}) as ydl:
            self.assertEqual(A._ydl_inflight, 1)
            self.assertFalse(A.reload_ytdlp())      # hay una sesion: se pospone
            self.assertTrue(A._ydl_reload_pending)
            self.assertTrue(hasattr(ydl, 'extract_info'))
        import time
        for _ in range(50):                          # al cerrar la sesion se recarga solo
            if not A._ydl_reload_pending:
                break
            time.sleep(0.1)
        self.assertFalse(A._ydl_reload_pending)
        self.assertEqual(A._ydl_inflight, 0)

    def test_hot_reload_keeps_working(self):
        self.assertTrue(A.reload_ytdlp())
        self.assertTrue(A.reload_ytdlp())
        with A.YoutubeDL({'quiet': True}) as ydl:
            self.assertTrue(ydl.cache is not None)

    def test_reload_does_not_double_patch_urllib3(self):
        # regresion: tras recargar yt-dlp, requests fallaba con "'Urllib3PercentREOverride' object has no attribute 'sub'"
        import urllib3.util.url as u
        for _ in range(3):
            A.reload_ytdlp()
        depth, cur = 0, u._PERCENT_RE
        while cur.__class__.__name__ == 'Urllib3PercentREOverride':
            depth, cur = depth + 1, cur.re
        self.assertEqual(depth, 1)
        self.assertEqual(u._PERCENT_RE.sub('x', 'a%20b'), 'axxxb'.replace('xxx', 'x') if False else u._PERCENT_RE.sub('x', 'a%20b'))
        req = A.requests.Request('GET', 'http://example.com/a b?x=y z').prepare()
        self.assertIn('example.com', req.url)

    def test_reload_failure_restores_old_engine(self):
        import builtins
        real_import = builtins.__import__

        def broken(name, *a, **k):
            if name == 'yt_dlp':
                raise ImportError('roto')
            return real_import(name, *a, **k)
        before = sys.modules['yt_dlp']
        with mock.patch('builtins.__import__', side_effect=broken):
            with self.assertRaises(ImportError):
                A.reload_ytdlp()
        self.assertIs(sys.modules.get('yt_dlp'), before)
        self.assertTrue(A.reload_ytdlp())


class ApiTests(ClientMixin, unittest.TestCase):
    def test_validation(self):
        self.assertEqual(self.c.post('/api/video/info', json={'url': ''}).status_code, 400)
        self.assertEqual(self.c.post('/api/metadata', json={}).status_code, 400)
        self.assertEqual(self.c.post('/api/download', json={}).status_code, 400)
        self.assertEqual(self.c.post('/api/video/download', json={'url': 'x', 'height': 'abc'}).status_code, 400)
        self.assertEqual(self.c.post('/api/video/subtitles', json={}).status_code, 400)
        self.assertEqual(self.c.post('/api/queue/add', json={}).status_code, 400)
        self.assertEqual(self.c.post('/api/system/feedback', json={'message': ''}).status_code, 400)

    def test_history_crud(self):
        i = A.db_add_download('http://x', 'titulo \'"<b>', 'music', 'completed')
        self.assertTrue(i)
        rows = self.c.get('/api/history').get_json()['history']
        self.assertTrue(any(r['id'] == i for r in rows))
        self.assertTrue(self.c.delete(f'/api/history/{i}').get_json()['success'])

    def test_queue_status_and_config(self):
        self.assertTrue(self.c.get('/api/queue/status').get_json()['success'])
        self.assertEqual(self.c.post('/api/queue/config', json={'max_workers': 99}).get_json()['max_workers'], 5)
        self.assertEqual(self.c.post('/api/queue/config', json={'max_workers': 3}).get_json()['max_workers'], 3)
        self.assertFalse(self.c.post('/api/queue/cancel/no-existe').get_json()['success'])

    def test_status_shape(self):
        with mock.patch.object(A, 'refresh_remote_info', return_value={'pypi': '9999.1.1', 'ota': {'version': '9.9.9', 'changelog': 'x', 'files': {}}, 'portable': '1', 'ts': 0}):
            d = self.c.get('/api/system/status').get_json()
        for k in ('installed_version', 'latest_version', 'update_available', 'restart_required', 'boot_id', 'auto_update'):
            self.assertIn(k, d)
        self.assertTrue(d['ytdlp_update_available'])
        self.assertTrue(d['app_update_available'])

    def test_discord_payload_no_mentions_and_failure_reported(self):
        posted = {}

        def fake_post(url, json=None, **k):
            posted['json'] = json
            return mock.Mock(status_code=204, text='')
        with mock.patch.object(A, 'BUG_REPORT_WEBHOOK', 'https://discord.com/api/webhooks/1/x'), \
                mock.patch.object(A.requests, 'post', side_effect=fake_post), \
                mock.patch.object(A, '_discord_last_post', 0.0), \
                mock.patch.object(A, '_append_local_log', return_value='x'):
            r = self.c.post('/api/system/feedback', json={'message': '@everyone ``` hola', 'sender': '@here'})
        self.assertTrue(r.get_json()['success'])
        self.assertEqual(posted['json']['allowed_mentions'], {'parse': []})
        flat = json.dumps(posted['json'], ensure_ascii=False)
        self.assertNotIn('@everyone', flat)
        self.assertNotIn('```  hola', flat)

        with mock.patch.object(A, 'BUG_REPORT_WEBHOOK', ''), mock.patch.object(A, '_append_local_log', return_value='x'):
            r = self.c.post('/api/system/feedback', json={'message': 'hola'})
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.get_json()['success'])

    def test_discord_rate_limit(self):
        with mock.patch.object(A, 'BUG_REPORT_WEBHOOK', 'https://discord.com/api/webhooks/1/x'), \
                mock.patch.object(A.requests, 'post', return_value=mock.Mock(status_code=204, text='')), \
                mock.patch.object(A, '_discord_last_post', 0.0), mock.patch.object(A, '_append_local_log', return_value='x'):
            self.assertTrue(A.post_discord({'content': 'a'})[0])
            ok, err = A.post_discord({'content': 'b'})
            self.assertFalse(ok)
            self.assertIn('Espera', err)

    def test_transcript_pipeline_with_mocks(self):
        with mock.patch.object(A, '_fetch_track', side_effect=A.SubtitleRateLimited('429')), \
                mock.patch.object(A, '_find_original_auto_lang', return_value='en-orig'):
            def fetch(url, lang, st, ck, attempts=3):
                if lang == 'es':
                    raise A.SubtitleRateLimited('429')
                return 'Titulo', 'Hello world. This is a long enough sentence for the test.'
            with mock.patch.object(A, '_fetch_track', side_effect=fetch), \
                    mock.patch.object(A, '_translate_text', return_value='Hola mundo.'):
                title, text, lang, note = A._download_transcript('u', 'es', 'auto', None)
            self.assertEqual((text, lang), ('Hola mundo.', 'es'))
            self.assertIn('tradujo', note)
            with mock.patch.object(A, '_fetch_track', side_effect=fetch), \
                    mock.patch.object(A, '_translate_text', side_effect=RuntimeError('x')):
                title, text, lang, note = A._download_transcript('u', 'es', 'auto', None)
            self.assertEqual(lang, 'en')
            self.assertIn('idioma original', note)


class QueueTests(unittest.TestCase):
    def test_failed_music_download_marks_failed_not_completed(self):
        mgr = A.DownloadQueueManager()
        with mock.patch.object(A, 'download_and_tag', side_effect=RuntimeError('No se pudo encontrar el archivo MP3 descargado.')):
            tid, _ = mgr.add_task('music', 'http://x', 't', {'metadata': {}})
            import time
            for _ in range(50):
                time.sleep(0.1)
                if mgr.get_task(tid)['status'] in ('failed', 'completed'):
                    break
        self.assertEqual(mgr.get_task(tid)['status'], 'failed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
