"""Root-only, explicit HTTPS rollout for the inspected Rytivo nginx topology.

Default is a read-only preflight. --apply backs up all targets before changing
them, tests nginx, restarts Django, checks local TLS, then reloads nginx.
No secrets or environment contents are printed. See HTTPS.md.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

SITE = Path('/etc/nginx/sites-available/rytivo.app')
VIDEO = Path('/etc/nginx/snippets/rytivo-video-locations.conf')
ENV = Path('/etc/repbase.env')
HSTS = 'add_header Strict-Transport-Security "max-age=3600" always;'
MARKER = '# Rytivo HTTPS hardening v1'
VALUES = {
    'DJANGO_SECURE_SSL_REDIRECT': 'true',
    'DJANGO_SECURE_HSTS_SECONDS': '3600',
    'DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS': 'false',
    'TRUST_PROXY_SSL_HEADER': 'true',
    'DJANGO_TRUST_PROXY_HTTPS': 'true',
}


def add_location_headers(text):
    # nginx 1.24 inherits add_header only if a location defines none of its own.
    return re.sub(r'(?m)(^[ \t]*add_header[^\n]*\n(?:[ \t]*add_header[^\n]*\n)*)',
                  lambda m: m[0] + '    ' + HSTS + '\n', text)


def site_config(text):
    if MARKER in text:
        if text.count(HSTS) != 3:
            raise ValueError('Modified managed site; review manually.')
        return text
    if 'Strict-Transport-Security' in text or 'proxy_hide_header' in text:
        raise ValueError('Existing header policy requires manual review.')
    if text.count('ssl_certificate_key') != 2 or text.count('add_header ') != 1:
        raise ValueError('Unexpected nginx site structure; review before applying.')
    if (len(re.findall(r'proxy_set_header\s+X-Forwarded-Proto\s+https;', text)) != 4
            or text.count('proxy_pass http://127.0.0.1:8001;') != 4):
        raise ValueError('Missing trusted HTTPS proxy header.')
    result = add_location_headers(text)
    result = re.sub(r'(?m)(^[ \t]*ssl_certificate_key[^\n]*\n)',
                    lambda m: m[0] + '    ' + HSTS + '\n'
                    '    proxy_hide_header Strict-Transport-Security;\n', result)
    return MARKER + '\n' + result


def video_config(text):
    if MARKER in text:
        if text.count(HSTS) != 1:
            raise ValueError('Modified managed video snippet; review manually.')
        return text
    # The repository template may already carry exactly the desired policy.
    if text.count(HSTS) == 1 and text.count('Strict-Transport-Security') == 1:
        text = text.replace('    ' + HSTS + '\n', '')
    if 'Strict-Transport-Security' in text or text.count('add_header ') != 2:
        raise ValueError('Unexpected video header policy; review manually.')
    if (len(re.findall(r'proxy_set_header\s+X-Forwarded-Proto\s+https;', text)) != 2
            or text.count('proxy_pass http://127.0.0.1:8001;') != 2):
        raise ValueError('Unexpected video proxy trust configuration.')
    return MARKER + '\n' + add_location_headers(text)


def environment(text):
    # Preserve all unrelated values, including credentials and moderation gates.
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if not any(
        re.match(r'^\s*(?:export\s+)?' + key + r'\s*=', line) for key in VALUES)]
    return ''.join(kept).rstrip('\n') + '\n' + ''.join(
        f'{key}={value}\n' for key, value in VALUES.items())


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def atomic_write(path, text):
    stat = path.stat()
    fd, name = tempfile.mkstemp(prefix='.rytivo-https-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, stat.st_mode & 0o777)
        os.chown(name, stat.st_uid, stat.st_gid)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def tls_probe(path, expected, *, hsts=False):
    output = run('curl', '--silent', '--show-error', '--max-time', '15',
                 '--resolve', 'rytivo.app:443:127.0.0.1', '-D', '-', '-o', '/dev/null',
                 'https://rytivo.app' + path)
    status = int(output.splitlines()[0].split()[1])
    if status != expected:
        raise ValueError('Unexpected HTTPS status on ' + path)
    headers = [line.strip().lower() for line in output.splitlines()
               if line.lower().startswith('strict-transport-security:')]
    if hsts and headers != ['strict-transport-security: max-age=3600']:
        raise ValueError('Missing or duplicate HSTS on ' + path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run as root; default mode performs preflight only.')
    for path in (SITE, VIDEO, ENV):
        if path.is_symlink() or not path.is_file():
            raise SystemExit('Expected regular target file: ' + str(path))
    if ENV.stat().st_mode & 0o077:
        raise SystemExit('/etc/repbase.env must not be accessible to group/others.')
    settings_source = Path('/home/django/app/config/settings.py').read_text()
    if "'DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS', 'false'" not in settings_source:
        raise SystemExit('Deploy the HTTPS settings change before running this tool.')
    listeners = run('ss', '-ltnH')
    endpoints = [line.split()[3] for line in listeners.splitlines() if ':8001 ' in line + ' ']
    if endpoints != ['127.0.0.1:8001']:
        raise SystemExit('Django must listen exclusively on 127.0.0.1:8001.')
    run('nginx', '-t')
    originals = {path: path.read_text() for path in (SITE, VIDEO, ENV)}
    planned = {SITE: site_config(originals[SITE]), VIDEO: video_config(originals[VIDEO]),
               ENV: environment(originals[ENV])}
    tls_probe('/', 200)
    tls_probe('/api/v1/', 401)
    print('Preflight passed. Targets: nginx site, video snippet, Django environment.')
    if not args.apply:
        print('No changes made. Re-run with --apply after reviewing deploy/HTTPS.md.')
        return
    backup = Path(tempfile.mkdtemp(prefix='rytivo-https-', dir='/root'))
    for index, path in enumerate(originals):
        shutil.copy2(path, backup / str(index))
    print('Private rollback backup:', backup)
    try:
        for path, text in planned.items():
            atomic_write(path, text)
        run('nginx', '-t')
        run('systemctl', 'restart', 'repbase')
        # curl retries startup connection failures; no credentials involved.
        run('curl', '--silent', '--show-error', '--fail', '--retry', '10',
            '--retry-connrefused', '--retry-delay', '1', '--max-time', '10',
            '-H', 'Host: rytivo.app', '-H', 'X-Forwarded-Proto: https',
            'http://127.0.0.1:8001/health/')
        tls_probe('/api/v1/', 401)
        run('systemctl', 'reload', 'nginx')
        for path, expected in [('/', 200), ('/api/v1/', 401), ('/access/reports/', 302),
                               ('/_private_video/nonexistent', 404)]:
            tls_probe(path, expected, hsts=True)
        html = run('curl', '--silent', '--show-error', '--max-time', '15',
                   '--resolve', 'rytivo.app:443:127.0.0.1', 'https://rytivo.app/')
        asset = re.search(r'["\'](/assets/[^"\']+)["\']', html)
        if not asset:
            raise ValueError('No web build asset found to verify HSTS inheritance.')
        tls_probe(asset[1], 200, hsts=True)
        print('HTTPS rollout passed: web, API, moderator login and private-media response.')
    except Exception:
        for path, text in originals.items():
            atomic_write(path, text)
        run('nginx', '-t')
        run('systemctl', 'restart', 'repbase')
        run('systemctl', 'reload', 'nginx')
        raise SystemExit('HTTPS rollout failed; previous configuration restored. Backup: ' + str(backup))


if __name__ == '__main__':
    main()
