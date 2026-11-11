import http.client
import json
from pathlib import Path
import ssl
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from agent.cloud import Cloud, Uploader, https_origin
from agent.setup import PrinterSettings, Setup, test_printer as check_printer
from agent.spool import Spool
from collector import Collector
from domain import Reading, parse_readings


def sample(at=None, ams='0'):
    return Reading(amsID=ams, humidity=24, temperature=31.6, rawIndex=3,
                   observedAt=time.time() if at is None else at)


@pytest.fixture
def spool(tmp_path):
    value = Spool(tmp_path / 'agent.sqlite3')
    yield value
    value.close()


def test_persistent_sampling_ordering_pruning_and_identity(tmp_path):
    path = tmp_path / 'outbox.sqlite3'
    first = Spool(path)
    now = time.time()
    first.save_reading(sample(now - 86401))
    first.save_reading(sample(now - 50, '1'))
    first.save_reading(sample(now - 40))
    first.save_reading(sample(now - 11))  # Same AMS, less than 30 seconds.
    first.save_reading(sample(now - 10))
    batch = first.batch(now)
    assert len(batch) == 3
    assert [x['observedAt'] for x in batch] == [now - 10, now - 50, now - 40]
    identities = [x['eventID'] for x in batch]
    first.close()
    restored = Spool(path)
    assert {x['eventID'] for x in restored.batch(now)} == set(identities)
    assert [x['observedAt'] for x in restored.batch(now)] == [now - 50, now - 40, now - 10]
    assert all(x['replay'] for x in restored.batch(now))
    restored.save_reading(sample(now + 10))
    assert len(restored.batch(now)) == 3
    restored.acknowledge(restored.batch(now)[:1])
    assert len(restored.batch(now)) == 2
    assert restored.batch(now + 86401) == []
    restored.close()


def test_batch_limit_and_account_replacement(spool):
    now = time.time()
    for index in range(110):
        spool.save_reading(sample(now - 5000 + index * 30))
    assert len(spool.batch(now)) == 100
    spool.configure({'token': 'new-account'})
    assert spool.configuration() == {'token': 'new-account'}
    assert spool.batch(now) == []
    spool.save_reading(sample(now))
    assert len(spool.batch(now)) == 1


def test_fresh_samples_after_success_and_replay_after_failure(spool):
    requests = []
    responses = iter([204, 503, 204, 204])
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(next(responses))
    cloud = Cloud('https://dashboard.example', httpx.Client(transport=httpx.MockTransport(handle)))
    uploader = Uploader(cloud, spool, 'opaque-token')
    uploader.step(False, 100)
    assert requests[0] == {'readings': [], 'printerConnected': False}
    spool.save_reading(sample())
    uploader.step(True, 101)
    assert requests[-1]['readings'][0]['replay'] is False
    event_id = requests[-1]['readings'][0]['eventID']
    assert uploader.status == 'retrying'
    uploader.step(True, 101.1)
    assert len(requests) == 2
    uploader.step(True, uploader.next_attempt)
    assert requests[-1]['readings'][0]['eventID'] == event_id
    assert requests[-1]['readings'][0]['replay'] is True
    assert requests[-1]['printerConnected'] is True
    assert spool.batch() == []
    uploader.step(True, uploader.heartbeat_at - 1)
    assert len(requests) == 3
    uploader.step(True, uploader.heartbeat_at)
    assert len(requests) == 4
    assert requests[-1] == {'readings': [], 'printerConnected': True}
    cloud.close()


@pytest.mark.parametrize('status', [301, 302, 307, 401, 403, 429, 500])
def test_upload_errors_preserve_outbox_and_never_follow_redirects(spool, status):
    spool.save_reading(sample())
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={'Location': 'http://insecure.test', 'Retry-After': '60'})
    cloud = Cloud('https://dashboard.example', httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True))
    uploader = Uploader(cloud, spool, 'opaque-token')
    uploader.step(False, 100)
    assert len(requests) == 1
    assert requests[0].headers['authorization'] == 'Bearer opaque-token'
    assert spool.batch()[0]['replay'] is True
    assert uploader.revoked == (status in (401, 403))
    if status == 429:
        assert uploader.next_attempt >= 160
    cloud.close()


def test_network_failure_and_restart_replay(spool):
    spool.save_reading(sample())
    def handle(request):
        raise httpx.ReadTimeout('timed out')
    cloud = Cloud('https://dashboard.example', httpx.Client(transport=httpx.MockTransport(handle)))
    uploader = Uploader(cloud, spool, 'secret')
    uploader.step(False, 1)
    assert uploader.next_attempt > 1
    assert len(spool.batch()) == 1
    assert spool.batch()[0]['replay'] is True
    cloud.close()


@pytest.mark.parametrize('origin', ['http://example.com', 'https://u:p@example.com', 'https://example.com/path',
                                   'https://example.com?', 'https://example.com#', 'https://example.com:bad', 'https://example.com:0'])
def test_https_only(origin):
    with pytest.raises(ValueError):
        https_origin(origin)


def test_enrollment_contract_and_opaque_token():
    calls = []
    def handle(request):
        calls.append((request.url.path, parse_qs(request.content.decode())))
        if request.url.path == '/auth/device/code':
            return httpx.Response(200, json={'device_code': 'private-device-code', 'user_code': 'ABC-123',
                'verification_uri': 'https://dashboard.example/pair', 'expires_in': 600, 'interval': 5})
        return httpx.Response(200, json={'access_token': 'authorization_pending', 'token_type': 'Bearer'})
    cloud = Cloud('https://dashboard.example', httpx.Client(transport=httpx.MockTransport(handle)))
    enrollment = cloud.begin('f' * 32)
    assert cloud.poll(enrollment['device_code']) == {'access_token': 'authorization_pending'}
    assert calls == [('/auth/device/code', {'client_id': ['airkeep-relay'], 'installation_id': ['f' * 32]}),
                     ('/auth/token', {'client_id': ['airkeep-relay'], 'device_code': ['private-device-code'],
                      'grant_type': ['urn:ietf:params:oauth:grant-type:device_code']})]
    assert 'printer_code' not in str(calls)
    cloud.close()


@pytest.mark.parametrize('error', ['authorization_pending', 'slow_down', 'expired_token', 'access_denied'])
def test_enrollment_rfc_errors(error):
    cloud = Cloud('https://dashboard.example', httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(400, json={'error': error}))))
    assert cloud.poll('private') == {'error': error}
    cloud.close()


def test_parser_malformed_nested_shapes_and_indices():
    for payload in [None, [], {'print': []}, {'print': {'ams': 'oops'}}, {'print': {'ams': {'ams': 3}}}]:
        assert parse_readings(payload) == []
    for identifier in [None, {}, True, 'a' * 1000, '0/report']:
        assert parse_readings({'print': {'ams': {'ams': [{'id': identifier, 'humidity_raw': 24}]}}}) == []
    reading = parse_readings({'print': {'ams': {'ams': [{'id': '0', 'humidity_raw': 24, 'humidity': float('inf')}]}}})
    assert reading[0].rawIndex is None


def test_serial_verification_closes_mismatched_socket_before_credentials():
    settings = PrinterSettings('192.168.1.9', '0123456789ABCDEF', '12345678')
    collector = Collector(settings, SimpleNamespace(save_reading=lambda _: None))
    class Socket:
        closed = False
        def __init__(self, subject):
            self.subject = subject
        def getpeercert(self):
            return {'subject': self.subject}
        def close(self):
            self.closed = True
    good = Socket(((('commonName', settings.printer_serial),),))
    collector.verify_serial(None, None, good)
    assert not good.closed
    for subject in [(), ((('commonName', 'OTHER'),),), ((('commonName', settings.printer_serial), ('commonName', 'OTHER')), )]:
        bad = Socket(subject)
        with pytest.raises(ssl.CertificateError):
            collector.verify_serial(None, None, bad)
        assert bad.closed
    context = collector.client._ssl_context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_printer_settings_stay_private_and_restrict_target():
    settings = PrinterSettings.validate({'printer_ip': '192.168.1.9', 'printer_serial': '0123456789ABCDEF', 'printer_code': '12345678'})
    assert '12345678' not in repr(settings)
    for address in ['example.com', '127.0.0.1', '8.8.8.8', '224.0.0.1', '::1']:
        with pytest.raises(ValueError):
            PrinterSettings.validate(dict(settings.private_config(), printer_ip=address))


def test_wizard_one_time_secret_csrf_host_validation_and_no_secret_echo(tmp_path, spool):
    setup = Setup(tmp_path, spool, 8788, printer_test=lambda _: True)
    from http.server import HTTPServer
    server = HTTPServer(('127.0.0.1', 0), setup.handler())
    setup.origin = f'http://127.0.0.1:{server.server_port}'
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    def request(path='/', fields=None, cookie=None, origin=None, host=None):
        from urllib.parse import urlencode
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
        headers = {'Host': host or f'127.0.0.1:{server.server_port}'}
        if cookie:
            headers['Cookie'] = cookie
        if fields is not None:
            headers.update({'Origin': origin or setup.origin, 'Content-Type': 'application/x-www-form-urlencoded'})
        connection.request('GET' if fields is None else 'POST', path, body=None if fields is None else urlencode(fields), headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        connection.close()
        return result
    secret = setup.secret
    try:
        assert (tmp_path / 'setup-secret').stat().st_mode & 0o777 == 0o600
        assert request(host='evil.example')[0] == 403
        assert request('/login', {'secret': secret}, origin='https://evil.example')[0] == 403
        assert request('/login', {'secret': 'é'})[0] == 403
        status, headers, body = request('/login', {'secret': secret})
        assert status == 200
        assert 'HttpOnly' in headers['Set-Cookie'] and 'SameSite=Strict' in headers['Set-Cookie']
        assert not (tmp_path / 'setup-secret').exists()
        assert secret not in body
        assert request('/login', {'secret': secret})[0] == 403
        assert request('/printer', {'printer_ip': '192.168.1.9'})[0] == 403
        status, _, body = request('/printer', {'printer_ip': '192.168.1.9', 'printer_serial': '0123456789ABCDEF',
            'printer_code': '12345678', 'origin': 'https://dashboard.example'}, cookie=headers['Set-Cookie'].split(';')[0])
        assert status == 200 and 'verified' in body
        assert '12345678' not in body and '0123456789ABCDEF' not in body
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
        setup.close()


def test_setup_obeys_poll_interval_slowdown_expiry_and_completes(tmp_path, spool, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('agent.setup.time.monotonic', lambda: clock[0])
    setup = Setup(tmp_path, spool, 8788)
    results = iter([{'error': 'slow_down'}, {'error': 'authorization_pending'}, {'access_token': 'opaque'}])
    setup.cloud = SimpleNamespace(origin='https://dashboard.example', poll=lambda _: next(results), close=lambda: None)
    setup.printer = PrinterSettings('192.168.1.9', '0123456789ABCDEF', '12345678')
    setup.enrollment = {'device_code': 'private'}
    setup.next_poll, setup.expires_at = 105, 700
    setup.tick()
    assert setup.next_poll == 105
    clock[0] = 105
    setup.tick()
    assert setup.interval == 10 and setup.next_poll == 115
    clock[0] = 115
    setup.tick()
    assert spool.configuration() is None
    clock[0] = 125
    setup.tick()
    assert spool.configuration()['token'] == 'opaque'
    assert len(spool.configuration()['installation_id']) == 32
    assert setup.completed_at == 125
    setup.close()


@pytest.mark.parametrize('serial,allowed', [('0123456789ABCDEF', True), ('OTHER', False)])
def test_real_paho_reconnect_checks_serial_before_mqtt_connect(monkeypatch, serial, allowed):
    collector = Collector(PrinterSettings('192.168.1.9', '0123456789ABCDEF', '12345678'), SimpleNamespace(save_reading=lambda _: None))
    sent = []
    socket = SimpleNamespace(setblocking=lambda _: None, close=lambda: None,
        getpeercert=lambda: {'subject': ((('commonName', serial),),)})
    monkeypatch.setattr(collector.client, '_create_socket', lambda: socket)
    monkeypatch.setattr(collector.client, '_send_connect', lambda _: sent.append('MQTT CONNECT'))
    collector.client.connect_async('192.168.1.9', 8883, 30)
    if allowed:
        collector.client.reconnect()
        assert sent == ['MQTT CONNECT']
    else:
        with pytest.raises(ssl.CertificateError):
            collector.client.reconnect()
        assert sent == []


def test_complete_setup_expiry_does_not_replace_saved_identity(tmp_path, spool, monkeypatch):
    spool.configure({'token': 'previous-account'})
    monkeypatch.setattr('agent.setup.time.monotonic', lambda: 100)
    setup = Setup(tmp_path, spool, 8788)
    setup.enrollment = {'device_code': 'private'}
    setup.expires_at = 99
    setup.tick()
    assert setup.enrollment is None
    assert spool.configuration() == {'token': 'previous-account'}
    setup.close()


def test_cli_first_run_keeps_setup_secret_out_of_process_output(tmp_path):
    import os
    import socket
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    environment = dict(os.environ, PYTHONPATH=str(root / 'server'))
    process = subprocess.Popen([sys.executable, '-m', 'agent', '--data-dir', str(tmp_path), '--setup-port', str(port)],
                               cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / 'setup-secret').exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.02)
        secret = (tmp_path / 'setup-secret').read_text().strip()
        assert len(secret) >= 32
        assert tmp_path.stat().st_mode & 0o777 == 0o700
        with httpx.Client(trust_env=False) as client:
            response = client.get(f'http://127.0.0.1:{port}')
            assert response.status_code == 200
            assert secret not in response.text
        process.terminate()
        output, errors = process.communicate(timeout=5)
        assert secret not in output + errors
        assert not (tmp_path / 'setup-secret').exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_new_live_observation_precedes_backlog_after_outage(spool, monkeypatch):
    clock = [100000.0]
    monkeypatch.setattr('agent.spool.time.time', lambda: clock[0])
    for index in range(110):
        spool.save_reading(sample(clock[0] - 5000 + index * 30))
    spool.save_reading(sample(clock[0]))
    requests = []
    responses = iter([503, 204])
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(next(responses))
    cloud = Cloud('https://dashboard.example', httpx.Client(transport=httpx.MockTransport(handle)))
    uploader = Uploader(cloud, spool, 'opaque-token')
    uploader.step(True, 100)
    assert requests[0]['readings'][0]['replay'] is False
    failed_id = requests[0]['readings'][0]['eventID']
    clock[0] += 31
    spool.save_reading(sample(clock[0]))
    new_live = spool.batch()[0]
    assert new_live['replay'] is False
    assert new_live['eventID'] != failed_id
    uploader.step(True, 200)
    delivered = requests[1]['readings']
    assert delivered[0] == new_live
    assert all(item['replay'] for item in delivered[1:])
    assert len(delivered) == 100
    assert all(item['eventID'] != new_live['eventID'] for item in spool.batch())
    assert any(item['eventID'] == failed_id for item in spool.batch())
    cloud.close()


def test_only_non_replay_observations_within_30_seconds_are_live(spool, monkeypatch):
    now = 100000.0
    monkeypatch.setattr('agent.spool.time.time', lambda: now)
    for index, age in enumerate([0, 5, 30, 30.001, -1]):
        spool.save_reading(sample(now - age, str(index)))
    batch = spool.batch()
    assert [item['amsID'] for item in batch] == ['0', '1', '2', '3', '4']
    assert [item['replay'] for item in batch] == [False, False, False, True, True]
    spool.mark_replay()
    spool.save_reading(sample(now, '5'))
    assert spool.batch()[0]['amsID'] == '5'
    assert spool.batch()[0]['replay'] is False
    assert all(item['replay'] for item in spool.batch()[1:])


@pytest.mark.parametrize('status', [400, 422])
def test_permanent_rejection_quarantines_only_invalid_row_and_delivers_valid_history(spool, status):
    from uuid import UUID
    now = time.time()
    for index in range(8):
        reading = sample(now - 1000 + index * 30)
        if index == 3:
            reading = reading.model_copy(update={'humidity': 101})
        spool.save_reading(reading)
    original = spool.batch()
    assert all(str(UUID(item['eventID'])) == item['eventID'] for item in original)
    invalid = next(item for item in original if item['humidity'] == 101)
    valid_ids = {item['eventID'] for item in original} - {invalid['eventID']}
    delivered = []
    def handle(request):
        readings = json.loads(request.content)['readings']
        if any(item['humidity'] == 101 for item in readings):
            return httpx.Response(status, json={'detail': 'private upstream error body'})
        delivered.extend(readings)
        return httpx.Response(204)
    cloud = Cloud('https://dashboard.example', httpx.Client(transport=httpx.MockTransport(handle)))
    uploader = Uploader(cloud, spool, 'opaque-token')
    statuses = []
    for attempt in range(30):
        uploader.step(True, 100 + attempt)
        statuses.append(uploader.status)
        if not spool.batch():
            break
    assert spool.batch() == []
    assert {item['eventID'] for item in delivered} == valid_ids
    assert len(delivered) == len(valid_ids)
    assert spool.quarantined_count() == 1
    quarantined = spool.db.execute('SELECT * FROM quarantine').fetchone()
    assert quarantined['event_id'] == invalid['eventID']
    assert quarantined['status'] == status
    assert json.loads(quarantined['data']) == invalid
    assert 'invalid_reading_quarantined' in statuses
    assert uploader.status == 'connected_with_quarantined_readings'
    assert all('private' not in value for value in statuses)
    cloud.close()


def test_rejected_clock_skew_does_not_block_subsequent_live_readings(spool, monkeypatch):
    clock = [100000.0]
    monkeypatch.setattr('agent.spool.time.time', lambda: clock[0])
    spool.save_reading(sample(clock[0] + 1000))
    future_id = spool.batch()[0]['eventID']
    spool.save_reading(sample(clock[0]))
    current = spool.batch()[0]
    assert current['replay'] is False
    delivered = []
    def handle(request):
        rows = json.loads(request.content)['readings']
        if any(item['observedAt'] > clock[0] + 90 for item in rows):
            return httpx.Response(400)
        delivered.extend(rows)
        return httpx.Response(204)
    cloud = Cloud('https://dashboard.example', httpx.Client(transport=httpx.MockTransport(handle)))
    uploader = Uploader(cloud, spool, 'opaque-token')
    for attempt in range(10):
        uploader.step(True, 100 + attempt)
        if not spool.batch():
            break
    assert delivered == [current]
    assert spool.db.execute('SELECT event_id FROM quarantine').fetchone()[0] == future_id
    clock[0] += 30
    spool.save_reading(sample(clock[0]))
    assert spool.batch()[0]['replay'] is False
    cloud.close()


def test_quarantine_is_bounded_and_expires_after_24_hours(spool, monkeypatch):
    now = 100000.0
    monkeypatch.setattr('agent.spool.time.time', lambda: now)
    for index in range(105):
        row = dict(sample(now).model_dump(), eventID=str(index), replay=True)
        spool.quarantine(row, 422, now=now + index)
    assert spool.quarantined_count() == 100
    spool.batch(now + 86400 + 106)
    assert spool.quarantined_count() == 0
    spool.quarantine(dict(sample(now).model_dump(), eventID='private-event', replay=True), 400)
    spool.configure({'token': 'new-account'})
    assert spool.quarantined_count() == 0


def test_observation_arriving_during_failed_request_remains_live(spool):
    spool.save_reading(sample(ams='0'))
    original_id = spool.batch()[0]['eventID']
    def handle(request):
        spool.save_reading(sample(ams='1'))
        return httpx.Response(503)
    cloud = Cloud('https://dashboard.example', httpx.Client(transport=httpx.MockTransport(handle)))
    uploader = Uploader(cloud, spool, 'opaque-token')
    uploader.step(True, 100)
    batch = spool.batch()
    assert batch[0]['amsID'] == '1'
    assert batch[0]['replay'] is False
    assert batch[1]['eventID'] == original_id
    assert batch[1]['replay'] is True
    cloud.close()
