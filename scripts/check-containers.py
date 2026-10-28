"""Exercise the two images in a disposable Docker network, including HTTPS and sessions.

Run with: uv run --directory server python ../scripts/check-containers.py
The Docker daemon must run on this host. Runtime credentials are generated for this test.
"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import tempfile

from argon2 import PasswordHasher


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--relay-image', default='airkeep-relay:local')
    parser.add_argument('--web-image', default='airkeep-web:local')
    args = parser.parse_args()
    token, password = secrets.token_hex(32), secrets.token_urlsafe(24)
    origin = 'https://web:8443'
    project = 'airkeep-test-' + secrets.token_hex(5)
    with tempfile.TemporaryDirectory(prefix='airkeep-containers-') as directory:
        root = Path(directory)
        certificates = root / 'tls'
        certificates.mkdir(mode=0o755)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=web', '-addext', 'subjectAltName=DNS:web',
                        '-keyout', str(certificates/'key.pem'), '-out', str(certificates/'fullchain.pem')],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # This one-day test certificate has no access to a real deployment.
        (certificates/'key.pem').chmod(0o644)
        services = {
            'airkeep': {
                'image': args.relay_image, 'read_only': True, 'cap_drop': ['ALL'],
                'security_opt': ['no-new-privileges:true'],
                'environment': {'AIRKEEP_API_TOKEN': token, 'AIRKEEP_MQTT_ENABLED': 'false',
                                'AIRKEEP_WEB_PASSWORD_HASH': PasswordHasher().hash(password),
                                'AIRKEEP_WEB_ORIGIN': origin},
                'tmpfs': ['/tmp', '/data:uid=10001,gid=10001,mode=0700'],
                'volumes': [str(certificates/'fullchain.pem') + ':/test-ca.pem:ro'],
            },
            'web': {
                'image': args.web_image, 'read_only': True, 'cap_drop': ['ALL'],
                'security_opt': ['no-new-privileges:true'], 'tmpfs': ['/tmp:mode=1777'],
                'environment': {'AIRKEEP_TLS_ENABLED': 'true', 'AIRKEEP_HOST': 'web'},
                'volumes': [str(certificates) + ':/run/tls:ro'],
                'depends_on': {'airkeep': {'condition': 'service_healthy'}},
            },
        }
        compose_file = root / 'compose.json'
        # Compose interpolates dollars even in a generated YAML/JSON mapping.
        compose_file.write_text(json.dumps({'services': services}).replace('$', '$$'))
        compose = ['docker', 'compose', '-p', project, '-f', str(compose_file)]
        probe = r'''
import httpx, os, re, ssl
assert os.getuid() != 0
token, password, origin = VALUES
context = ssl.create_default_context(cafile='/test-ca.pem')
with httpx.Client(base_url='https://web:8443', verify=context) as client:
    page = client.get('/')
    assert page.status_code == 200 and '<title>Airkeep</title>' in page.text
    assert page.headers['cache-control'] == 'no-cache'
    assert page.headers['x-frame-options'] == 'DENY'
    assert "frame-ancestors 'none'" in page.headers['content-security-policy']
    assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page.text)
    assert assets
    for asset in assets:
        response = client.get(asset)
        assert response.status_code == 200 and 'immutable' in response.headers['cache-control']
    assert client.get('/brand/mark.svg').status_code == 200
    missing = client.get('/assets/missing.js')
    assert missing.status_code == 404 and 'immutable' not in missing.headers['cache-control']
    assert client.get('/.env').status_code == 404
    assert client.get('/health').status_code == 200
    assert client.get('/v1/snapshot').status_code == 401
    snapshot = client.get('/v1/snapshot', headers={'Authorization': 'Bearer '+token})
    assert snapshot.status_code == 200 and len(snapshot.content) < 16384
    assert snapshot.headers['cache-control'] == 'no-store'
    assert client.get('/v2/history?hours=169', headers={'Authorization': 'Bearer '+token}).status_code == 422
    assert client.post('/auth/login', json={'password':password}, headers={'Origin':'https://other.example.com'}).status_code == 403
    login = client.post('/auth/login', json={'password':password}, headers={'Origin':origin})
    assert login.status_code == 200, login.text
    cookie = login.headers['set-cookie'].lower()
    assert 'secure' in cookie and 'httponly' in cookie and 'samesite=strict' in cookie
    assert client.get('/auth/session').json()['authenticated']
    assert client.get('/v2/admin/overview').status_code == 200
    assert client.post('/v2/admin/devices').status_code == 405
    assert client.post('/v2/alerts', json={'name':'Container smoke'}).status_code == 403
    headers = {'Origin':origin, 'X-Airkeep-CSRF':login.json()['csrf']}
    created = client.post('/v2/alerts', json={'name':'Container smoke'}, headers=headers)
    assert created.status_code in (200,201), created.text
    rule = created.json()
    path = '/v2/alerts/'+rule['id']
    saved = client.put(path, json={**rule,'name':'Renamed'}, headers={**headers,'If-Match':str(rule['revision'])})
    assert saved.status_code == 200, saved.text
    assert client.put(path, json=rule, headers={**headers,'If-Match':str(rule['revision'])}).status_code == 412
    assert client.post('/auth/logout', headers=headers).status_code == 200
    assert not client.get('/auth/session').json()['authenticated']
with httpx.Client(base_url='http://airkeep:8787') as backend:
    for path in ['/', '/index.html', '/assets/main.js', '/brand/mark.svg']:
        assert backend.get(path).status_code == 404
print('HTTPS, static assets, native API, secure sessions, CSRF and revision conflicts passed.')
'''.replace('VALUES', repr((token, password, origin)))
        try:
            subprocess.run(compose + ['up', '-d', '--wait', '--wait-timeout', '90'], check=True)
            subprocess.run(compose + ['exec', '-T', 'airkeep', 'python', '-'], input=probe, text=True, check=True)
            uid = subprocess.check_output(compose + ['exec', '-T', 'web', 'id', '-u'], text=True).strip()
            assert uid != '0'
            subprocess.run(compose + ['stop', 'airkeep'], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(compose + ['exec', '-T', 'web', 'sh', '-c',
                                     'wget -q -O /dev/null http://127.0.0.1:8080/ && '
                                     'wget -q -O /dev/null http://127.0.0.1:8080/brand/mark.svg'], check=True)
            print('Frontend remains available while the backend is stopped. Both images run without root.')
            services['web']['environment'] = {}
            services['web'].pop('volumes')
            compose_file.write_text(json.dumps({'services': services}).replace('$', '$$'))
            subprocess.run(compose + ['up', '-d', '--no-deps', '--force-recreate', '--wait',
                                     '--wait-timeout', '60', 'web'], check=True)
            subprocess.run(compose + ['exec', '-T', 'web', 'sh', '-c',
                                     'test ! -s /tmp/airkeep-tls.conf && '
                                     'wget -q -O /dev/null http://127.0.0.1:8080/'], check=True)
            print('HTTP-only frontend starts and serves assets without certificate mounts.')
        except Exception:
            subprocess.run(compose + ['logs', '--tail', '60'])
            raise
        finally:
            subprocess.run(compose + ['down', '--volumes', '--remove-orphans'], check=True)


if __name__ == '__main__':
    main()
