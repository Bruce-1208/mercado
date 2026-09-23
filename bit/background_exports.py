"""Bounded, durable, read-only exports with owner and current-scope checks."""
from contextlib import contextmanager
import hashlib
import logging
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlsplit

from flask import jsonify, request, send_file
from bit.bit_runtime_lock import InterProcessLock
from bit.service_split import service_mode, signature, HOP_HEADER

EXPORT_PATHS = frozenset({
    "/api/infractions/latest/export", "/api/reputation/latest/export",
    "/api/official-infractions/export", "/api/official-ip-rights/export",
    "/api/prohibited-listings/export", "/api/risk-check/results/export",
})


class ExportQueue:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "exports.sqlite3"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS exports (
                id TEXT PRIMARY KEY, owner INTEGER NOT NULL, scope TEXT NOT NULL,
                user_json TEXT NOT NULL, url TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, message TEXT NOT NULL DEFAULT '',
                content_type TEXT NOT NULL DEFAULT '', disposition TEXT NOT NULL DEFAULT '')""")
            db.execute("CREATE INDEX IF NOT EXISTS exports_status ON exports(status,created)")
        self.wakeup = threading.Event()
        self.guard = threading.Lock()
        self.thread = None

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM exports WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def enqueue(self, user, scope, url):
        parts = urlsplit(url)
        if parts.scheme or parts.netloc or parts.fragment or parts.path not in EXPORT_PATHS or len(url) > 8192:
            raise ValueError("不支持的导出地址")
        owner = int(user["id"])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM exports WHERE owner=? AND scope=? AND url=? "
                "AND status IN ('queued','running')", (owner, scope, url)).fetchone()
            if existing:
                return dict(existing)
            own_pending = db.execute("SELECT COUNT(*) FROM exports WHERE owner=? "
                "AND status IN ('queued','running')", (owner,)).fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM exports WHERE status IN ('queued','running')").fetchone()[0]
            if own_pending >= 2 or pending >= 100:
                raise OverflowError("导出队列繁忙，请等待已有任务完成")
            job_id = uuid.uuid4().hex
            now = time.time()
            db.execute("INSERT INTO exports(id,owner,scope,user_json,url,status,created,updated) "
                       "VALUES(?,?,?,?,?,'queued',?,?)",
                       (job_id, owner, scope, json.dumps(user, ensure_ascii=False), url, now, now))
        self.wakeup.set()
        return self.get(job_id)

    def process_one(self, render):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM exports WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
            if row is None:
                return False
            job = dict(row)
            db.execute("UPDATE exports SET status='running',updated=? WHERE id=?", (time.time(), job['id']))
        temporary = self.root / (job['id'] + '.tmp')
        try:
            content, content_type, disposition = render(job)
            with temporary.open('wb') as handle:
                handle.write(content)
            os.replace(temporary, self.root / (job['id'] + '.bin'))
            with self.connect() as db:
                db.execute("UPDATE exports SET status='ready',updated=?,content_type=?,disposition=? WHERE id=?",
                           (time.time(), content_type, disposition, job['id']))
        except Exception:
            logging.exception("后台导出失败：job=%s", job["id"])
            # Do not persist raw database errors, URLs or account information.
            with self.connect() as db:
                db.execute("UPDATE exports SET status='error',updated=?,message=? WHERE id=?",
                           (time.time(), '生成失败或权限已变化，请重新提交导出', job['id']))
        finally:
            temporary.unlink(missing_ok=True)
        return True

    def cleanup(self):
        cutoff = time.time() - 86400
        with self.connect() as db:
            expired = db.execute("SELECT id FROM exports WHERE updated<? AND status IN ('ready','error')", (cutoff,)).fetchall()
            for row in expired:
                (self.root / (row['id'] + '.bin')).unlink(missing_ok=True)
            db.execute("DELETE FROM exports WHERE updated<? AND status IN ('ready','error')", (cutoff,))

    def start(self, render):
        with self.guard:
            if self.thread and self.thread.is_alive():
                return
            def work():
                lock = InterProcessLock('workbench-export-worker', owner='exports')
                if not lock.acquire(timeout=0):
                    return
                try:
                    with self.connect() as db:
                        db.execute("UPDATE exports SET status='error',message='服务重启中断导出，请重新提交',updated=? "
                                   "WHERE status='running'", (time.time(),))
                    while True:
                        self.wakeup.clear()
                        self.cleanup()
                        if not self.process_one(render):
                            self.wakeup.wait(5)
                finally:
                    lock.release()
            self.thread = threading.Thread(target=work, name='workbench-exports', daemon=True)
            self.thread.start()


def install_exports(app, root, get_user, get_scope):
    queue = None
    guard = threading.Lock()
    def store():
        nonlocal queue
        with guard:
            if queue is None:
                queue = ExportQueue(root)
        return queue

    def fingerprint(user):
        allowed = get_scope(user)
        payload = {k: user.get(k) for k in (
            'id', 'organization_key', 'permissions', 'role_key', 'own_store_only',
            'is_platform_admin', 'username', 'display_name', 'salesperson')}
        payload['token_ids'] = None if allowed is None else sorted(allowed)
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def render(job):
        with app.test_client() as client:
            with client.session_transaction() as session:
                session['workbench_user'] = json.loads(job['user_json'])
            # Run live authorization before generating, including scope changes
            # since enqueue. This signed private request uses the same handlers.
            timestamp = str(int(time.time()))
            headers = {"X-Workbench-Original-IP": "127.0.0.1"}
            headers[HOP_HEADER] = timestamp + ':' + signature(app.secret_key, 'GET', job['url'], b'', timestamp, '127.0.0.1')
            with app.test_request_context(job['url']):
                from flask import session
                session['workbench_user'] = json.loads(job['user_json'])
                user = get_user()
                if not user or fingerprint(user) != job['scope']:
                    raise PermissionError('scope changed')
            response = client.get(job['url'], headers=headers)
            try:
                disposition = response.headers.get('Content-Disposition', '')
                if response.status_code != 200 or 'attachment' not in disposition.lower():
                    raise RuntimeError('export failed')
                return response.get_data(), response.content_type, disposition
            finally:
                response.close()

    def start():
        if service_mode() != 'web' and not app.testing:
            store().start(render)

    def identity():
        user = get_user()
        if not user or not user.get('id'):
            return None
        return user

    @app.post('/api/exports')
    def enqueue_export():
        user = identity()
        if not user:
            return jsonify(status='error', message='请先登录'), 401
        try:
            job = store().enqueue(user, fingerprint(user), str((request.get_json(silent=True) or {}).get('url') or ''))
        except ValueError as exc:
            return jsonify(status='error', message=str(exc)), 400
        except OverflowError as exc:
            return jsonify(status='error', message=str(exc)), 429
        start()
        return jsonify(status='success', data={'id': job['id'], 'status': job['status']}), 202

    def authorized_job(job_id):
        user = identity()
        if not user:
            return None, (jsonify(status='error', message='请先登录'), 401)
        job = store().get(job_id)
        if not job or job['owner'] != int(user['id']) or job['scope'] != fingerprint(user):
            return None, (jsonify(status='error', message='导出不存在或当前无权访问'), 404)
        if job['status'] in ('ready', 'error') and job['updated'] < time.time() - 86400:
            return None, (jsonify(status='error', message='导出已过期，请重新生成'), 410)
        return job, None

    @app.get('/api/exports/<job_id>')
    def export_status(job_id):
        job, error = authorized_job(job_id)
        if error:
            return error
        response = jsonify(status='success', data={k: job[k] for k in ('id','status','message')})
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/exports/<job_id>/download')
    def export_download(job_id):
        job, error = authorized_job(job_id)
        if error:
            return error
        if job['status'] != 'ready':
            return jsonify(status='error', message='导出尚未完成'), 409
        path = store().root / (job['id'] + '.bin')
        if not path.is_file():
            return jsonify(status='error', message='导出文件已不可用，请重新生成'), 410
        response = send_file(path, mimetype=job['content_type'], as_attachment=True, download_name='export')
        response.headers['Content-Disposition'] = job['disposition']
        response.headers['Cache-Control'] = 'private, no-store'
        return response
    return start
