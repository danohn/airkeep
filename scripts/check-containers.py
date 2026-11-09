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
    password = secrets.token_urlsafe(24)
    origin = 'https://airkeep-web:8443'
    project = 'airkeep-test-' + secrets.token_hex(5)
    with tempfile.TemporaryDirectory(prefix='airkeep-containers-') as directory:
        root = Path(directory)
        certificates = root / 'tls'
        certificates.mkdir(mode=0o755)
        certificates.chmod(0o755)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=airkeep-web', '-addext', 'subjectAltName=DNS:airkeep-web',
                        '-keyout', str(certificates/'key.pem'), '-out', str(certificates/'fullchain.pem')],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # This one-day test certificate has no access to a real deployment.
        (certificates/'key.pem').chmod(0o644)
        (certificates/'fullchain.pem').chmod(0o644)
        services = {
            'airkeep-relay': {
                'image': args.relay_image, 'read_only': True, 'cap_drop': ['ALL'],
                'security_opt': ['no-new-privileges:true'],
                'environment': {'AIRKEEP_MQTT_ENABLED': 'false',
                                'AIRKEEP_WEB_PASSWORD_HASH': PasswordHasher().hash(password),
                                'AIRKEEP_JWT_SIGNING_KEY': secrets.token_hex(32),
                                'AIRKEEP_WEB_ORIGIN': origin},
                'tmpfs': ['/tmp', '/data:uid=10001,gid=10001,mode=0700'],
                'volumes': [str(certificates/'fullchain.pem') + ':/test-ca.pem:ro'],
                'networks': {'default': {'aliases': ['airkeep']}},
            },
            'airkeep-web': {
                'image': args.web_image, 'read_only': True, 'cap_drop': ['ALL'],
                'security_opt': ['no-new-privileges:true'], 'tmpfs': ['/tmp:mode=1777'],
                'environment': {'AIRKEEP_TLS_ENABLED': 'true', 'AIRKEEP_HOST': 'airkeep-web'},
                'volumes': [str(certificates) + ':/run/tls:ro'],
                'depends_on': {'airkeep-relay': {'condition': 'service_healthy'}},
            },
        }
        compose_file = root / 'compose.json'
        # Compose interpolates dollars even in a generated YAML/JSON mapping.
        compose_file.write_text(json.dumps({'services': services}).replace('$', '$$'))
        compose = ['docker', 'compose', '-p', project, '-f', str(compose_file)]
        probe = r'''
import base64, hashlib, httpx, os, re, ssl, time
from urllib.parse import parse_qs, urlsplit
assert os.getuid() != 0
password, origin = VALUES
context = ssl.create_default_context(cafile='/test-ca.pem')
with httpx.Client(base_url=origin, verify=context) as client:
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
    retired = {'Authorization': 'Bearer ' + 'retired-shared-token-' + 'x'*32}
    assert client.get('/v1/snapshot', headers=retired).status_code == 401
    assert client.get('/v2/alerts', headers=retired).status_code == 401
    client.headers['Origin'] = origin
    verifier = 'v'*43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    authorization = dict(client_id='dashboard',redirect_uri=origin+'/auth/callback',
                         response_type='code',state='s'*43,code_challenge=challenge,code_challenge_method='S256')
    assert client.get('/connect').status_code == 200
    assert client.get('/auth/callback').status_code == 200
    assert client.post('/auth/authorize',json=authorization,headers={'Origin':'https://other.example.com'}).status_code == 403
    transaction = client.post('/auth/authorize',json=authorization).json()['transaction']
    login = client.post('/auth/credentials',json={'transaction':transaction,'username':'owner','password':password})
    assert login.status_code == 200,login.text
    assert 'set-cookie' not in login.headers and not client.cookies
    callback = parse_qs(urlsplit(login.json()['redirect']).query)
    assert callback['state'] == [authorization['state']]
    session = client.post('/auth/token',data=dict(client_id='dashboard',grant_type='authorization_code',
                          redirect_uri=authorization['redirect_uri'],code_verifier=verifier,code=callback['code'][0]))
    assert session.status_code == 200 and session.json() == {'authenticated': True}
    assert 'httponly' in session.headers['set-cookie'].lower()
    assert client.get('/auth/session').json()['authenticated']
    assert client.get('/device').status_code == 200
    pair = client.post('/auth/device/code', data={'client_id':'esp32','installation_id':'a'*32}).json()
    decision = client.post('/auth/device/decision',
                           json={'user_code':pair['user_code'],'approve':True})
    assert decision.status_code == 204, decision.text
    time.sleep(pair['interval'])
    display = client.post('/auth/token', data={'client_id':'esp32','device_code':pair['device_code'],
                         'grant_type':'urn:ietf:params:oauth:grant-type:device_code'})
    assert display.status_code == 200 and 'set-cookie' not in display.headers, display.text
    display_headers = {'Authorization': 'Bearer ' + display.json()['access_token']}
    snapshot = client.get('/v1/snapshot', headers=display_headers)
    assert snapshot.status_code == 200 and len(snapshot.content) < 16384
    assert snapshot.headers['cache-control'] == 'no-store'
    assert 'alertRule' not in snapshot.json()
    assert client.get('/v2/alerts', headers=display_headers).status_code == 403
    assert client.get('/auth/me').status_code == 200
    assert client.get('/v2/history?hours=169').status_code == 422
    assert client.get('/v2/admin/overview').status_code == 200
    assert client.post('/v2/admin/devices').status_code == 405
    assert client.post('/v2/alerts',json={'name':'Container smoke'},
                       headers={'Origin':'https://other.example.com'}).status_code == 403
    created = client.post('/v2/alerts', json={'name':'Container smoke'})
    assert created.status_code in (200,201), created.text
    rule = created.json()
    path = '/v2/alerts/'+rule['id']
    saved = client.put(path, json={**rule,'name':'Renamed'}, headers={'If-Match':str(rule['revision'])})
    assert saved.status_code == 200, saved.text
    assert client.put(path, json=rule, headers={'If-Match':str(rule['revision'])}).status_code == 412
    assert client.post('/auth/revoke').status_code == 204
    assert client.get('/auth/me').status_code == 401
    assert not client.cookies
with httpx.Client(base_url='http://airkeep-relay:8787') as backend:
    for path in ['/', '/index.html', '/assets/main.js', '/brand/mark.svg']:
        assert backend.get(path).status_code == 404
print('HTTPS, static assets, scoped display OAuth, HttpOnly browser sessions, revocation and rule conflicts passed.')
'''.replace('VALUES', repr((password, origin)))
        try:
            subprocess.run(compose + ['up', '-d', '--wait', '--wait-timeout', '90'], check=True)
            subprocess.run(compose + ['exec', '-T', 'airkeep-relay', 'python', 'manage_accounts.py', 'bootstrap', 'owner', '--import-owner-password'], check=True)
            subprocess.run(compose + ['exec', '-T', 'airkeep-relay', 'python', '-'], input=probe, text=True, check=True)
            uid = subprocess.check_output(compose + ['exec', '-T', 'airkeep-web', 'id', '-u'], text=True).strip()
            assert uid != '0'
            subprocess.run(compose + ['stop', 'airkeep-relay'], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(compose + ['exec', '-T', 'airkeep-web', 'sh', '-c',
                                     'wget -q -O /dev/null http://127.0.0.1:8080/ && '
                                     'wget -q -O /dev/null http://127.0.0.1:8080/brand/mark.svg'], check=True)
            print('Frontend remains available while the backend is stopped. Both images run without root.')
            services['airkeep-web']['environment'] = {}
            services['airkeep-web'].pop('volumes')
            compose_file.write_text(json.dumps({'services': services}).replace('$', '$$'))
            subprocess.run(compose + ['up', '-d', '--no-deps', '--force-recreate', '--wait',
                                     '--wait-timeout', '60', 'airkeep-web'], check=True)
            subprocess.run(compose + ['exec', '-T', 'airkeep-web', 'sh', '-c',
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
