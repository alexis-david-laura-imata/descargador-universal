"""Prepara una version nueva: sube APP_VERSION, escribe version.json con los hashes SHA-256 de los archivos
que la actualizacion automatica (OTA) descargara, y comprueba que app.py compila.

Uso:  python make_release.py 2.1.1 "Texto de los cambios"
Despues sube app.py, templates/index.html, static/style.css y version.json a GitHub (rama main).
"""
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
FILES = ['app.py', 'templates/index.html', 'static/style.css']


def sha256_lf(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read().replace(b'\r\n', b'\n')).hexdigest()


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    version, changelog = sys.argv[1], sys.argv[2]
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        print('La version debe tener el formato 1.2.3')
        return 1

    app_path = os.path.join(ROOT, 'app.py')
    with open(app_path, encoding='utf-8', newline='') as f:
        code = f.read()
    code, n = re.subn(r'^APP_VERSION = "[^"]*"', f'APP_VERSION = "{version}"', code, count=1, flags=re.M)
    if n != 1:
        print('No se encontro APP_VERSION en app.py')
        return 1
    with open(app_path, 'w', encoding='utf-8', newline='') as f:
        f.write(code)
    compile(code, app_path, 'exec')  # falla si app.py tiene errores de sintaxis

    manifest = {'version': version, 'changelog': changelog,
                'files': {rel: sha256_lf(os.path.join(ROOT, rel)) for rel in FILES}}
    with open(os.path.join(ROOT, 'version.json'), 'w', encoding='utf-8', newline='') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write('\n')
    print(f'version.json -> {version}')
    for rel, digest in manifest['files'].items():
        print(f'  {digest[:16]}  {rel}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
