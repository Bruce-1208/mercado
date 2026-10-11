"""Durable, worker-shared session revocation and public HTTPS enforcement."""
import ipaddress
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from flask import redirect, request
from flask.sessions import SecureCookieSessionInterface


def _loopback(value):
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value == 'localhost'


def local_request():
    return _loopback(request.remote_addr or '') and _loopback(urlsplit(request.url).hostname or '')


class RevocableSessionInterface(SecureCookieSessionInterface):
    def _connect(self, app):
        path = Path(app.config['WORKBENCH_SESSION_DB'])
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5)
        conn.execute('CREATE TABLE IF NOT EXISTS sessions (sid TEXT PRIMARY KEY, expires REAL NOT NULL)')
        return conn

    def open_session(self, app, req):
        session = super().open_session(app, req)
        sid = session.get('_sid')
        req.environ['workbench.original_sid'] = sid
        if session.get('workbench_user'):
            with self._connect(app) as conn:
                valid = sid and conn.execute('SELECT 1 FROM sessions WHERE sid=? AND expires>?', (sid, time.time())).fetchone()
            conn.close()
            if not valid:
                session.clear()
        return session

    def get_cookie_secure(self, app):
        return request.is_secure or not local_request()

    def save_session(self, app, session, response):
        original = request.environ.get('workbench.original_sid')
        sid = session.get('_sid')
        authenticated = bool(session.get('workbench_user'))
        if (original and (not authenticated or original != sid)) or (authenticated and not sid):
            with self._connect(app) as conn:
                if original:
                    conn.execute('DELETE FROM sessions WHERE sid=?', (original,))
                if authenticated and not sid:
                    sid = secrets.token_urlsafe(32)
                    session['_sid'] = sid
                    conn.execute('DELETE FROM sessions WHERE expires<=?', (time.time(),))
                    conn.execute('INSERT INTO sessions VALUES (?,?)', (sid, time.time() + app.permanent_session_lifetime.total_seconds()))
            conn.close()
        super().save_session(app, session, response)


def install_session_security(app):
    app.config.setdefault('WORKBENCH_SESSION_DB', os.environ.get('BIT_SESSION_DB_PATH') or str(Path(__file__).resolve().parents[1] / '.data' / 'workbench-sessions.sqlite3'))
    app.config.setdefault('WORKBENCH_HTTPS_ORIGIN', os.environ.get('BIT_PUBLIC_WORKBENCH_URL', ''))
    app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
    app.session_interface = RevocableSessionInterface()

    @app.before_request
    def require_https():
        # Waitress accepts scheme forwarding only from the configured local proxy.
        if not request.is_secure and not local_request() and not app.testing:
            parts = urlsplit(request.url)
            public = urlsplit(app.config['WORKBENCH_HTTPS_ORIGIN'])
            host = public.netloc if public.scheme == 'https' and public.hostname else parts.netloc
            if host.endswith(':80'):
                host = host[:-3]
            return redirect(urlunsplit(('https', host, parts.path, parts.query, '')), code=308)

    @app.after_request
    def protect_session_response(response):
        if request.is_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        if request.path in {'/login', '/logout', '/api/login', '/api/account/profile'}:
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        return response
