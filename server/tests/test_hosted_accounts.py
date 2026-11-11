"""Account-link and outbox regressions using a local DB and mocked Twilio HTTP."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import HTTPException, Response
import httpx
import pytest

from accounts import OAuth, PASSWORD, revoke_user
from hosted_accounts import CompleteLink, DeleteAccount, EmailAddress, EmailSettings, HostedAccounts, login_limit
from storage import Store

ORIGIN = 'https://airkeep.example.test'
PASSWORD_TEXT = 'one long account password'


@pytest.fixture
def hosted(tmp_path):
    store = Store(tmp_path / 'accounts.db')
    settings = SimpleNamespace(web_origin=ORIGIN, jwt_signing_key='k'*64,
                               web_secure_cookies=True, public_signup=True)
    oauth = OAuth(store, settings)
    store.db.executescript((Path(__file__).parents[1] / 'migrations/002_hosted_accounts.sql').read_text())
    store.db.execute('CREATE TABLE oauth_device_requests(owner TEXT)')
    store.db.execute('CREATE TABLE account_deletions(account_id TEXT PRIMARY KEY,deleted_at REAL)')
    store.tenant = lambda user: nullcontext()
    store.purge_tenant = lambda user: None
    service = HostedAccounts(oauth, settings, EmailSettings(twilio_api_key='test-key', twilio_api_secret='test-secret',
                             email_from='noreply@example.test', email_link_key=Fernet.generate_key().decode()))
    request = SimpleNamespace(headers={'origin': ORIGIN}, client=SimpleNamespace(host='192.0.2.1'))
    yield service, request
    store.close()


def token_for(service):
    row = service.store.rows('SELECT sealed_secret FROM hosted_email_outbox WHERE sealed_secret IS NOT NULL ORDER BY created DESC LIMIT 1')[0]
    return service.cipher.decrypt(row['sealed_secret'].encode()).decode()


def signup(service, request):
    service.request_link(EmailAddress(email='Alice@Example.Test'), request, 'signup')
    service.complete(CompleteLink(token=token_for(service), password=PASSWORD_TEXT), request, 'signup')
    return service.store.rows('SELECT * FROM accounts')[0]


def test_signup_verifies_before_account_exists_and_consumes_link(hosted):
    service, request = hosted
    generic = service.request_link(EmailAddress(email=' Alice@Example.Test '), request, 'signup')
    assert not service.store.rows('SELECT * FROM accounts')
    token = token_for(service)
    assert service.store.rows('SELECT email FROM hosted_account_links')[0]['email'] == 'alice@example.test'
    assert token not in str([dict(row) for row in service.store.rows('SELECT * FROM hosted_account_links')])
    service.complete(CompleteLink(token=token, password=PASSWORD_TEXT), request, 'signup')
    account = service.store.rows('SELECT * FROM accounts')[0]
    assert account['username'] == 'alice@example.test' and account['role'] == 'member'
    assert PASSWORD.verify(account['hash'], PASSWORD_TEXT)
    assert not service.store.rows('SELECT * FROM hosted_account_links')
    assert service.store.rows('SELECT sealed_secret FROM hosted_email_outbox')[0]['sealed_secret'] is None
    with pytest.raises(HTTPException):
        service.complete(CompleteLink(token=token, password=PASSWORD_TEXT), request, 'signup')
    assert service.request_link(EmailAddress(email='alice@example.test'), request, 'signup') == generic


def test_reset_revokes_grants_and_other_reset_links(hosted):
    service, request = hosted
    account = signup(service, request)
    with service.store.transaction():
        tokens = service.oauth.grant(account['id'], 'apple-ios', 'phone')
    service.request_link(EmailAddress(email=account['username']), request, 'reset')
    token = token_for(service)
    service.request_link(EmailAddress(email=account['username']), request, 'reset')
    service.complete(CompleteLink(token=token, password='a new long account password'), request, 'reset')
    changed = service.store.rows('SELECT * FROM accounts')[0]
    assert changed['id'] == account['id'] and changed['version'] == account['version'] + 1
    assert not service.oauth.active(tokens['grant_id'])
    assert service.oauth.exchange({'grant_type': 'refresh_token', 'client_id': 'apple-ios', 'refresh_token': tokens['refresh_token']}) is None
    assert not service.store.rows('SELECT * FROM hosted_account_links')
    assert all(row['sealed_secret'] is None for row in service.store.rows('SELECT sealed_secret FROM hosted_email_outbox'))


def test_limits_retain_hmac_buckets_and_reject_exhaustion(hosted):
    service, _ = hosted
    for _ in range(5):
        login_limit(service.store, service.oauth.key, 'private@example.test', '192.0.2.7')
    with pytest.raises(HTTPException) as caught:
        login_limit(service.store, service.oauth.key, 'private@example.test', '192.0.2.7')
    assert caught.value.status_code == 429
    buckets = str([dict(row) for row in service.store.rows('SELECT * FROM hosted_account_limits')])
    assert 'private@example.test' not in buckets and '192.0.2.7' not in buckets


@pytest.mark.asyncio
async def test_twilio_retry_accept_scrubbing_and_operation_poll(hosted):
    service, request = hosted
    service.request_link(EmailAddress(email='alice@example.test'), request, 'signup')
    token = token_for(service)
    seen = []

    def reply(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(429)
        if request.method == 'POST':
            return httpx.Response(202, json={'operationId': 'comms_operation_test'})
        return httpx.Response(200, json={'status': 'COMPLETED', 'stats': {'sent': 1}})

    service.client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    try:
        await service.tick()
        row = service.store.rows('SELECT * FROM hosted_email_outbox')[0]
        assert row['state'] == 'pending' and row['attempts'] == 1
        assert service.cipher.decrypt(row['sealed_secret'].encode()).decode() == token
        service.store.execute('UPDATE hosted_email_outbox SET next_attempt=0')
        await service.tick()
        row = service.store.rows('SELECT * FROM hosted_email_outbox')[0]
        assert row['state'] == 'accepted' and row['sealed_secret'] is None
        assert row['operation_id'] == 'comms_operation_test'
        assert str(seen[1].url) == 'https://comms.twilio.com/v1/Emails'
        service.store.execute('UPDATE hosted_email_outbox SET next_attempt=0')
        await service.tick()
        assert service.store.rows('SELECT state FROM hosted_email_outbox')[0]['state'] == 'sent'
        assert str(seen[-1].url).endswith('/Emails/Operations/comms_operation_test')
    finally:
        await service.close()


def test_deletion_requires_password_and_cancels_pending_mail(hosted):
    service, request = hosted
    account = signup(service, request)
    with service.store.transaction():
        session = service.oauth.grant(account['id'], 'dashboard', None)
    identity = service.oauth.authenticate(session['access_token'], browser=True)
    service.request_link(EmailAddress(email=account['username']), request, 'reset')
    with pytest.raises(HTTPException) as caught:
        service.delete(DeleteAccount(password='wrong', confirmation='DELETE'), request, Response(), identity)
    assert caught.value.status_code == 401 and service.store.rows('SELECT * FROM accounts')
    response = Response()
    service.delete(DeleteAccount(password=PASSWORD_TEXT, confirmation='DELETE'), request, response, identity)
    assert not service.store.rows('SELECT * FROM accounts')
    assert not service.store.rows('SELECT * FROM oauth_grants')
    assert not service.store.rows('SELECT * FROM hosted_account_links')
    assert all(row['sealed_secret'] is None for row in service.store.rows('SELECT sealed_secret FROM hosted_email_outbox'))
    assert 'Max-Age=0' in response.headers['set-cookie']


def test_closed_signup_without_mail_credentials_never_enqueues(hosted):
    service, request = hosted
    settings = SimpleNamespace(web_origin=ORIGIN, public_signup=False)
    closed = HostedAccounts(service.oauth, settings, EmailSettings(email_link_key=Fernet.generate_key().decode()))
    assert not closed.configured
    assert closed.request_link(EmailAddress(email='closed@example.test'), request, 'signup')
    assert not closed.store.rows('SELECT * FROM hosted_account_links')


def test_browser_account_guard_rejects_reads_and_writes_after_cookie_switch(hosted):
    service, request = hosted
    account = signup(service, request)
    with service.store.transaction():
        session = service.oauth.grant(account['id'], 'dashboard', None)
    request.cookies = {service.oauth.cookie_name: session['access_token']}
    request.headers['x-airkeep-account'] = 'another-tenant'
    for method in ('GET', 'POST', 'DELETE'):
        request.method = method
        with pytest.raises(HTTPException) as caught:
            service.oauth.browser_session(request)
        assert caught.value.status_code == 409
        assert caught.value.headers['X-Airkeep-Account-Changed'] == '1'


def test_password_revocation_disables_hosted_relay_credentials(hosted):
    service, request = hosted
    account = signup(service, request)
    service.store.execute('CREATE TABLE relay_credentials(hash TEXT,user TEXT,revoked INTEGER DEFAULT 0)')
    service.store.execute('INSERT INTO relay_credentials(hash,user) VALUES(?,?)', ('relay-hash',account['id']))
    service.store.hosted = True
    try:
        with service.store.transaction():
            revoke_user(service.store, account['id'])
        assert service.store.rows('SELECT revoked FROM relay_credentials')[0]['revoked'] == 1
    finally:
        service.store.hosted = False
