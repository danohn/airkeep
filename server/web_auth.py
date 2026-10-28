"""Single-owner browser sessions, separate from native API bearer credentials."""
import hashlib
import secrets
import threading
import time
from collections import OrderedDict

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request

COOKIE = 'airkeep_session'
LIFETIME = 8 * 3600


class BrowserAuth:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.passwords = PasswordHasher()
        self.attempts = OrderedDict()
        self.lock = threading.Lock()
        self.verification = threading.Semaphore(1)

    @property
    def configured(self):
        return bool(self.settings.web_password_hash and self.settings.web_origin)

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def same_origin(self, request: Request):
        origin = request.headers.get('origin')
        if not self.configured or origin != self.settings.web_origin.rstrip('/'):
            raise HTTPException(403, 'Open Airkeep at its configured address and try again.')

    def session(self, request: Request):
        if not self.configured:
            raise HTTPException(401, 'Set up browser access on your relay.')
        value = request.cookies.get(COOKIE, '')
        rows = self.store.rows('SELECT * FROM web_sessions WHERE id=? AND expires>? AND version=?',
            (self.digest(value), time.time(), self.digest(self.settings.web_password_hash))) if value else []
        if not rows:
            raise HTTPException(401, 'Sign in to Airkeep.')
        return rows[0]

    def require(self, request: Request):
        session = self.session(request)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            self.same_origin(request)
            csrf = request.headers.get('x-airkeep-csrf', '')
            if not secrets.compare_digest(csrf.encode(), session['csrf'].encode()):
                raise HTTPException(403, 'Refresh this page and try again.')
        return session

    def login(self, request, password):
        self.same_origin(request)
        address = request.client.host if request.client else 'unknown'
        now = time.time()
        with self.lock:
            # Bound memory even when a proxy exposes many distinct client addresses.
            recent = [at for at in self.attempts.get(address, []) if at > now - 300]
            if len(recent) >= 5:
                raise HTTPException(429, 'Please wait five minutes before trying again.', headers={'Retry-After': '300'})
            self.attempts[address] = recent + [now]
            self.attempts.move_to_end(address)
            while len(self.attempts) > 1024:
                self.attempts.popitem(last=False)
        try:
            with self.verification:
                self.passwords.verify(self.settings.web_password_hash, password)
        except (VerificationError, InvalidHashError):
            raise HTTPException(401, 'Check your password and try again.') from None
        with self.lock:
            self.attempts.pop(address, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.store.execute('INSERT INTO web_sessions VALUES(?,?,?,?,?)',
            (self.digest(token), csrf, now, now + LIFETIME, self.digest(self.settings.web_password_hash)))
        return token, csrf
