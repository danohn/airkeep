"""One-time, loopback-only browser setup. Printer secrets never leave this host."""
import html
import ipaddress
from pathlib import Path
import re
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs
import uuid

import httpx

from .cloud import Cloud, DEFAULT_ORIGIN, https_origin
from .collector import BAMBU_CA, Collector


@dataclass(repr=False)
class PrinterSettings:
    printer_ip: str
    printer_serial: str
    printer_code: str = field(repr=False)
    ca_path: Path = BAMBU_CA

    @classmethod
    def validate(cls, fields):
        address = ipaddress.ip_address(fields['printer_ip'].strip())
        local = (ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('172.16.0.0/12'),
                 ipaddress.ip_network('192.168.0.0/16'), ipaddress.ip_network('169.254.0.0/16'),
                 ipaddress.ip_network('fc00::/7'), ipaddress.ip_network('fe80::/10'))
        if not any(address in network for network in local):
            raise ValueError('Enter the printer’s private LAN IP address.')
        serial = fields['printer_serial'].strip()
        code = fields['printer_code'].strip()
        if not re.fullmatch(r'[A-Za-z0-9]{8,32}', serial):
            raise ValueError('Enter the serial shown in printer settings.')
        if not re.fullmatch(r'[A-Za-z0-9]{8}', code):
            raise ValueError('Enter the eight-character LAN access code.')
        return cls(str(address), serial, code)

    def private_config(self):
        return {'printer_ip': self.printer_ip, 'printer_serial': self.printer_serial, 'printer_code': self.printer_code}


class DiscardReadings:
    def save_reading(self, reading):
        pass


def test_printer(settings, timeout=12):
    collector = Collector(settings, DiscardReadings())
    try:
        collector.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if collector.connected:
                return True
            time.sleep(0.1)
        return False
    except (OSError, ValueError):
        return False
    finally:
        collector.stop()


class Setup:
    def __init__(self, spool, port, printer_test=test_printer, cloud_factory=Cloud):
        self.spool = spool
        self.origin = f'http://127.0.0.1:{port}'
        self.printer_test, self.cloud_factory = printer_test, cloud_factory
        self.printer = None
        self.cloud = None
        self.enrollment = None
        self.message = ''
        self.completed_at = None
        self.installation_id = uuid.uuid4().hex
        self.interval = 5
        self.next_poll = 0

    def tick(self):
        if not self.enrollment or self.completed_at is not None:
            return
        now = time.monotonic()
        if now >= self.expires_at:
            self.enrollment = None
            self.message = 'The sign-in code expired. Begin sign-in again.'
            return
        if now < self.next_poll:
            return
        self.next_poll = now + self.interval
        try:
            result = self.cloud.poll(self.enrollment['device_code'])
        except (ValueError, KeyError, httpx.HTTPError):
            self.message = 'Waiting for the dashboard connection. Retrying.'
            return
        error = result.get('error')
        if error == 'slow_down':
            self.interval += 5
            self.next_poll = now + self.interval
        elif error in ('access_denied', 'expired_token'):
            self.enrollment = None
            self.message = 'Sign-in ended. Begin sign-in again.'
        elif not error:
            self.spool.configure(dict(self.printer.private_config(), origin=self.cloud.origin,
                                      installation_id=self.installation_id, token=result['access_token']))
            self.completed_at = time.monotonic()

    def close(self):
        if self.cloud:
            self.cloud.close()

    def page(self):
        content = '<h1>Connect Airkeep</h1>'
        if self.completed_at is not None:
            content += '<p>Setup complete. The agent is collecting printer readings and sending them to your dashboard. You can close this window.</p>'
        elif self.enrollment:
            uri = html.escape(self.enrollment['verification_uri'], quote=True)
            code = html.escape(self.enrollment['user_code'])
            content += f'<p>Open <a href="{uri}" target="_blank" rel="noreferrer">your Airkeep dashboard</a>, sign in, and enter this code.</p><p><strong>{code}</strong></p><p>This page checks approval automatically.</p>'
        elif self.printer:
            content += '<p>Printer TLS, serial and LAN access code verified.</p><form method="post" action="/enroll"><button>Begin dashboard sign-in</button></form>'
        else:
            content += f'''<p>Keep your P2S on the same LAN, enable LAN access, and copy its settings here.</p>
            <form method="post" action="/printer">
            <label>Printer LAN IP <input name="printer_ip" required autocomplete="off"></label>
            <label>Printer serial <input name="printer_serial" required autocomplete="off"></label>
            <label>LAN access code <input name="printer_code" type="password" required autocomplete="off"></label>
            <label>Dashboard HTTPS origin <input name="origin" value="{DEFAULT_ORIGIN}" type="url" required></label>
            <button>Test printer connection</button></form>'''
        if self.message:
            content += '<p role="status">' + html.escape(self.message) + '</p>'
        refresh = '<meta http-equiv="refresh" content="3">' if self.enrollment and self.completed_at is None else ''
        return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{refresh}<title>Airkeep setup</title>
        <style>body{{font:18px system-ui;max-width:38rem;margin:4rem auto;padding:1rem;background:#faf8f1;color:#20251e}}label{{display:block;margin:1rem 0}}input{{display:block;width:95%;font:inherit;padding:.5rem}}button{{font:inherit;padding:.6rem 1rem}}strong{{font:2rem monospace}}</style><main>{content}</main></html>'''.encode()

    def handler(self):
        setup = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                # Keep form values and enrollment codes out of logs.
                pass

            def valid_host(self):
                return self.headers.get('Host') == setup.origin.removeprefix('http://')

            def respond(self, status=200, content=b''):
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Cache-Control', 'no-store')
                # Browsers need to send the real Origin on local form posts.
                self.send_header('Referrer-Policy', 'same-origin')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
                if status == 303:
                    self.send_header('Location', '/')
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self):
                if not self.valid_host() or self.path != '/':
                    self.respond(403)
                    return
                self.respond(content=setup.page())

            def do_POST(self):
                if (not self.valid_host() or self.headers.get('Origin') != setup.origin
                        or setup.completed_at is not None):
                    self.respond(403)
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if (not 0 <= length <= 4096 or (length == 0 and self.path != '/enroll')
                            or self.headers.get('Content-Type') != 'application/x-www-form-urlencoded'):
                        self.respond(400)
                        return
                    fields = {key: value[0] for key, value in parse_qs(self.rfile.read(length).decode(), max_num_fields=8).items() if len(value) == 1}
                except (ValueError, UnicodeError):
                    self.respond(400)
                    return
                try:
                    if self.path == '/printer' and setup.enrollment is None:
                        printer = PrinterSettings.validate(fields)
                        origin = https_origin(fields['origin'].strip())
                        if not setup.printer_test(printer):
                            setup.message = 'Printer test failed. Check LAN reachability, serial, access code and LAN mode, then try again.'
                        else:
                            setup.printer = printer
                            if setup.cloud:
                                setup.cloud.close()
                            setup.cloud = setup.cloud_factory(origin)
                            setup.message = ''
                    elif self.path == '/enroll' and setup.printer and setup.enrollment is None:
                        setup.enrollment = setup.cloud.begin(setup.installation_id)
                        setup.interval = float(setup.enrollment.get('interval', 5))
                        setup.next_poll = time.monotonic() + setup.interval
                        setup.expires_at = time.monotonic() + float(setup.enrollment['expires_in'])
                        setup.message = ''
                    else:
                        self.respond(404)
                        return
                except (ValueError, KeyError, httpx.HTTPError):
                    setup.message = 'Setup could not complete. Check the settings and dashboard connection, then try again.'
                # The enrollment page refreshes automatically, so serve it at GET /.
                self.respond(303)

        return Handler


def run_setup(spool, port, stopped=None):
    setup = Setup(spool, port)
    try:
        with HTTPServer(('127.0.0.1', port), setup.handler()) as server:
            server.timeout = 0.5
            print(f'Open {setup.origin} to set up your printer.', flush=True)
            while setup.completed_at is None or time.monotonic() - setup.completed_at < 4:
                if stopped is not None and stopped.is_set():
                    raise SystemExit('Local setup stopped.')
                server.handle_request()
                setup.tick()
    finally:
        setup.close()
