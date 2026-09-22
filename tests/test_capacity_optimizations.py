import concurrent.futures
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask, g, jsonify, request

from bit.sync_capacity import SyncBudget, due_batch
from mercado_api.request_budget import RequestBudget
from bit.local_agent_hub import LocalAgentStore
from bit.local_agent_bundle import build_business_bundle
from bit.background_exports import ExportQueue, install_exports
from bit import service_split
from erp.query_cache import QueryCache


def test_store_budget_reserves_orders_and_releases_after_failure():
    budget = SyncBudget(2, 1)
    acquired = threading.Event()
    release = threading.Event()
    def background():
        with budget.slot('links', 1):
            acquired.set()
            assert release.wait(3)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        future = pool.submit(background)
        assert acquired.wait(2)
        with pytest.raises(RuntimeError), budget.slot('orders', 2):
            assert budget.snapshot()['active'] == 2
            raise RuntimeError('simulated task failure')
        release.set()
        future.result()
    assert budget.snapshot()['active'] == 0
    assert budget.snapshot()['completed'] == 2


def test_mixed_store_tasks_respect_global_limit_and_same_store_serialization():
    budget = SyncBudget(4, 1)
    active, peak, keys = 0, 0, set()
    lock = threading.Lock()
    def task(i):
        nonlocal active, peak
        kind = 'orders' if i % 4 == 0 else 'links'
        key = (kind, i % 3)
        with budget.slot(*key):
            with lock:
                assert key not in keys
                keys.add(key); active += 1; peak = max(peak, active)
            time.sleep(.002)
            with lock:
                keys.remove(key); active -= 1
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        list(pool.map(task, range(100)))
    assert peak <= 4
    assert budget.snapshot()['completed'] == 100
    assert budget.snapshot()['waiting'] == 0


def test_network_slots_are_released_on_timeout_and_limit_accounts():
    budget = RequestBudget(total=3, per_account=1)
    lock = threading.Lock()
    active = set()
    def request_once(i):
        token = str(i % 5)
        with budget.slot(token):
            with lock:
                assert token not in active
                active.add(token)
                assert len(active) <= 3
            time.sleep(.002)
            with lock:
                active.remove(token)
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        list(pool.map(request_once, range(80)))
    with pytest.raises(TimeoutError), budget.slot('secret'):
        raise TimeoutError()
    assert budget.active == 0 and not budget.accounts


def test_auto_dispatch_is_bounded_without_discarding_input(monkeypatch):
    monkeypatch.setenv('MERCADO_SYNC_AUTO_BATCH_STORES', '8')
    due = list(range(1000))
    assert due_batch(due) == list(range(8))
    assert len(due) == 1000


def test_idle_and_running_claims_do_not_take_write_transactions(tmp_path, monkeypatch):
    store = LocalAgentStore(tmp_path / 'hub.sqlite3')
    store.heartbeat('agent-a', name='A', session_id='session', now=100)
    traces = []
    original = store._connect
    def connection():
        db = original(); db.set_trace_callback(traces.append); return db
    monkeypatch.setattr(store, '_connect', connection)
    assert store.claim_job('agent-a', session_id='session', now=101) is None
    assert not any('BEGIN IMMEDIATE' in sql for sql in traces)
    store.enqueue_job('job-a', 'agent-a', 'daily_task', {}, now=102)
    store.claim_job('agent-a', session_id='session', now=103)
    traces.clear()
    assert store.claim_job('agent-a', session_id='session', now=104) is None
    assert not any('BEGIN IMMEDIATE' in sql for sql in traces)


def test_concurrent_claims_deliver_job_once(tmp_path):
    store = LocalAgentStore(tmp_path / 'hub.sqlite3')
    store.heartbeat('agent-a', name='A', session_id='session', now=100)
    store.enqueue_job('job-a', 'agent-a', 'daily_task', {}, now=101)
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        claims = list(pool.map(lambda _: store.claim_job('agent-a', session_id='session', now=102), range(100)))
    assert sum(job is not None for job in claims) == 1


def test_bundle_unchanged_skips_file_content_reads_and_detects_same_size_edit(tmp_path, monkeypatch):
    (tmp_path / 'bit').mkdir()
    source = tmp_path / 'bit' / 'one.py'
    source.write_text('value=1')
    first = build_business_bundle(tmp_path)
    original = Path.read_bytes
    def counted(path):
        if path == source:
            raise AssertionError('unchanged source should not be read')
        return original(path)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_bytes', counted)
        assert build_business_bundle(tmp_path) is first
    source.write_text('value=2')
    second = build_business_bundle(tmp_path)
    assert second['version'] != first['version']


def test_scoped_count_cache_invalidation_cannot_resurrect_old_value():
    cache = QueryCache(ttl=60, max_entries=2)
    assert cache.get_or_load(('org-a', (1,)), lambda: 10) == 10
    assert cache.get_or_load(('org-b', (2,)), lambda: 20) == 20
    assert cache.get_or_load(('org-a', (1,)), lambda: 99) == 10
    def racing_write():
        cache.clear()
        return 30
    assert cache.get_or_load(('org-c', (3,)), racing_write) == 30
    assert not cache.entries
    assert cache.get_or_load(('org-c', (3,)), lambda: 40) == 40


def test_export_queue_coalesces_duplicate_requests_and_bounds_user_backlog(tmp_path):
    queue = ExportQueue(tmp_path)
    user = {'id': 1}
    a = queue.enqueue(user, 'scope-a', '/api/reputation/latest/export')
    assert queue.enqueue(user, 'scope-a', '/api/reputation/latest/export')['id'] == a['id']
    queue.enqueue(user, 'scope-a', '/api/infractions/latest/export')
    with pytest.raises(OverflowError):
        queue.enqueue(user, 'scope-a', '/api/risk-check/results/export')
    with pytest.raises(ValueError):
        queue.enqueue(user, 'scope-a', 'https://example.com/api/reputation/latest/export')
    assert queue.process_one(lambda job: (b'xlsx', 'application/test', 'attachment; filename="a.xlsx"'))
    reopened = ExportQueue(tmp_path)
    assert reopened.get(a['id'])['status'] == 'ready'
    assert (tmp_path / (a['id'] + '.bin')).read_bytes() == b'xlsx'


def test_export_status_and_download_require_same_owner_and_current_scope(tmp_path):
    app = Flask(__name__); app.secret_key = 'test'; app.testing = True
    current = {'id': 1, 'organization_key': 'one', 'permissions': ['read']}
    scopes = [1, 2]
    install_exports(app, tmp_path, lambda: current, lambda user: scopes)
    client = app.test_client()
    result = client.post('/api/exports', json={'url': '/api/reputation/latest/export'})
    assert result.status_code == 202
    job_id = result.json['data']['id']
    queue = ExportQueue(tmp_path)
    queue.process_one(lambda job: (b'xlsx', 'application/test', 'attachment; filename="a.xlsx"'))
    response = client.get(f'/api/exports/{job_id}/download')
    assert response.status_code == 200 and response.data == b'xlsx'
    response.close()
    current['id'] = 2
    assert client.get(f'/api/exports/{job_id}').status_code == 404
    current['id'] = 1
    scopes[:] = [1]
    assert client.get(f'/api/exports/{job_id}/download').status_code == 404
    current.clear()
    assert client.get(f'/api/exports/{job_id}').status_code == 401


def test_worker_requires_signed_request_and_preserves_original_remote_address(monkeypatch):
    app = Flask(__name__); app.secret_key = 'secret'
    @app.post('/api/db/order-sync/start')
    def start():
        return jsonify(remote=g.worker_original_remote_addr, body=request.json)
    service_split.install_service_routing(app)
    monkeypatch.setenv('BIT_SERVICE_MODE', 'worker')
    client = app.test_client()
    assert client.post('/api/db/order-sync/start', json={}).status_code == 403
    body = b'{"token_ids":[1]}'
    path = '/api/db/order-sync/start'
    timestamp = str(int(time.time()))
    original_ip = '198.51.100.1'
    headers = {'Content-Type':'application/json', 'X-Workbench-Original-IP':original_ip,
               service_split.HOP_HEADER: timestamp + ':' + service_split.signature(app.secret_key, 'POST', path, body, timestamp, original_ip)}
    response = client.post(path, data=body, headers=headers)
    assert response.status_code == 200
    assert response.json == {'remote': original_ip, 'body':{'token_ids':[1]}}
    headers['X-Workbench-Original-IP'] = '127.0.0.1'
    assert client.post(path, data=body, headers=headers).status_code == 403
    headers['X-Workbench-Original-IP'] = original_ip
    assert client.post(path, data=b'{}', headers=headers).status_code == 403


def test_unavailable_worker_never_retries_post(monkeypatch):
    import requests
    app = Flask(__name__); app.secret_key = 'secret'
    service_split.install_service_routing(app)
    monkeypatch.setenv('BIT_SERVICE_MODE', 'web')
    calls = []
    class Transport:
        def request(self, *args, **kwargs):
            calls.append((args, kwargs))
            assert not self.trust_env
            raise requests.Timeout()
        def close(self): pass
    monkeypatch.setattr(service_split.requests, 'Session', Transport)
    response = app.test_client().post('/api/order-sync/start', json={'token_ids':[1]})
    assert response.status_code == 503
    assert len(calls) == 1


def test_shared_browser_extension_and_executor_tasks_follow_same_worker(monkeypatch):
    # Alternate entry points call the same in-memory task implementations.
    # Route all of them to the owner so status and cancellation cannot diverge.
    monkeypatch.setenv('BIT_SERVICE_MODE', 'web')
    app = Flask(__name__); app.secret_key = 'test'
    service_split.install_service_routing(app)
    paths = ['/api/zying-collection/start', '/api/zying-collection/status',
             '/api/browser-extension/zying/start', '/api/browser-extension/zying/stop',
             '/api/browser-extension/zying/status', '/api/tasks/daily/start',
             '/api/local-executor/tasks/daily/status', '/api/local-executor/tasks/daily/stop',
             '/api/run_shensu', '/api/local-executor/run_shensu/stop']
    with patch.object(service_split.requests.Session, 'request', side_effect=service_split.requests.ConnectionError) as send:
        for path in paths:
            assert app.test_client().get(path).status_code == 503
        assert send.call_count == len(paths)


def test_old_agent_heartbeats_coalesce_writes_but_cancel_and_session_change_are_immediate(tmp_path, monkeypatch):
    store = LocalAgentStore(tmp_path / 'hub.sqlite3')
    args = dict(name='A', session_id='old', current_job_id='job-a')
    store.heartbeat('agent-a', **args, now=100)
    store.enqueue_job('job-a', 'agent-a', 'daily_task', {}, now=100)
    store.claim_job('agent-a', session_id='old', now=100)
    original = store._connect
    calls = []
    def connect():
        calls.append(1)
        return original()
    monkeypatch.setattr(store, '_connect', connect)
    for now in range(101, 105):
        store.heartbeat('agent-a', **args, now=now)
    assert calls == []
    store.request_cancel('job-a', now=104)
    assert store.cancellation_job_ids('agent-a') == ['job-a']
    store.heartbeat('agent-a', **{**args, 'session_id':'new'}, now=104)
    assert store.get_job('job-a')['status'] == 'stopped'
    store.heartbeat('agent-a', **args, now=110)
    assert store.get_agent('agent-a')['last_seen'] == 110


def test_profit_refresh_sql_can_be_bound_by_real_pymysql(monkeypatch):
    import pymysql
    from erp import mercadolibre_collection_store as collection
    connection = pymysql.connections.Connection(defer_connect=True)
    connection.server_status = 0
    formatter = connection.cursor()
    statements = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, args=None):
            statements.append(formatter.mogrify(sql, args))
        def fetchall(self): return []
    class Connection:
        def cursor(self): return Cursor()
        def commit(self): pass
        def close(self): pass
    monkeypatch.setattr(collection, 'ensure_collection_tables', lambda cursor: None)
    assert collection.list_stale_profitability_items(stale_before='2026-09-20 00:00:00', connection_factory=Connection) == []
    assert any("LIKE 'fixed_commission_15_pct%'" in sql for sql in statements)


def test_split_child_environments_share_role_but_have_separate_pool_budgets(monkeypatch):
    from bit.workbench_services import child_environment
    monkeypatch.setenv('MYSQL_POOL_MAX_CONNECTIONS', '12')
    monkeypatch.setenv('WORKBENCH_SECRET_KEY', 'shared-secret')
    web, worker = child_environment('web'), child_environment('worker')
    assert web['BIT_SERVICE_MODE'] == 'web' and worker['BIT_SERVICE_MODE'] == 'worker'
    assert web['MYSQL_POOL_MAX_CONNECTIONS'] == '6'
    assert worker['MYSQL_POOL_MAX_CONNECTIONS'] == '8'
    assert web['WORKBENCH_SECRET_KEY'] == worker['WORKBENCH_SECRET_KEY']
    assert worker['BIT_RUNTIME_ROLE'] == web['BIT_RUNTIME_ROLE'] == 'server'


def test_web_mode_does_not_start_central_background_services(monkeypatch):
    from bit import bit_interface as interface
    monkeypatch.setenv('BIT_SERVICE_MODE', 'web')
    monkeypatch.setattr(interface, 'USE_DB_API', False)
    monkeypatch.setattr(interface, 'start_browser_cleanup', lambda: None)
    def unexpected():
        pytest.fail('Web process must not start a central scheduler')
    monkeypatch.setattr(interface, 'start_store_link_scheduler_bootstrap', unexpected)
    interface.start_interface_background_services()


def test_background_export_runs_existing_view_with_current_permissions(tmp_path, monkeypatch):
    app = Flask(__name__); app.secret_key = 'export-test'
    user = {'id':1, 'permissions':['read'], 'organization_key':'a'}
    scopes = [1]
    calls = []
    @app.get('/api/reputation/latest/export')
    def existing_export():
        from flask import Response, session
        assert session['workbench_user']['id'] == 1
        calls.append(1)
        return Response(b'generated-xlsx', content_type='application/test',
                        headers={'Content-Disposition':'attachment; filename="report.xlsx"'})
    callbacks = []
    monkeypatch.setattr(ExportQueue, 'start', lambda self, render: callbacks.append(render))
    install_exports(app, tmp_path, lambda: user, lambda user: scopes)
    service_split.install_service_routing(app)
    monkeypatch.setenv('BIT_SERVICE_MODE', 'combined')
    client = app.test_client()
    response = client.post('/api/exports', json={'url':'/api/reputation/latest/export'})
    job_id = response.json['data']['id']
    queue = ExportQueue(tmp_path)
    assert queue.process_one(callbacks[0])
    assert queue.get(job_id)['status'] == 'ready' and len(calls) == 1
    second = client.post('/api/exports', json={'url':'/api/reputation/latest/export'}).json['data']['id']
    scopes[:] = []
    assert queue.process_one(callbacks[0])
    assert queue.get(second)['status'] == 'error'
    assert len(calls) == 1


def test_private_forwarding_does_not_grant_public_db_request_loopback_access(monkeypatch):
    from bit import bit_interface as interface
    monkeypatch.setenv('BIT_SERVICE_MODE', 'worker')
    monkeypatch.delenv('BIT_DB_API_TOKEN', raising=False)
    monkeypatch.setattr(interface, '_verify_local_agent_credential', lambda token: None)
    path = '/api/db/order-sync/start'
    body = b'{}'
    timestamp = str(int(time.time()))
    ip = '198.51.100.2'
    headers = {'Content-Type':'application/json', 'X-Workbench-Original-IP':ip,
        service_split.HOP_HEADER:timestamp + ':' + service_split.signature(interface.app.secret_key, 'POST', path, body, timestamp, ip)}
    response = interface.app.test_client().post(path, data=body, headers=headers)
    assert response.status_code == 403


def test_hop_signature_survives_normal_url_encoding():
    assert service_split.signature('s','GET','/api/order-sync/status?q=%41&tail=?',b'','1') == service_split.signature('s','GET','/api/order-sync/status?q=A&tail=?',b'','1')


@pytest.mark.parametrize('module_name, start_name, request_name, status_name', [
    ('bit.bit_prohibited_listing_sync','start_prohibited_listing_sync','request_prohibited_sync','prohibited_listing_sync_status'),
    ('bit.mercado_infraction_sync','start_official_infraction_sync','request_infraction_sync','official_infraction_sync_status'),
])
def test_busy_sync_persists_other_store_request(monkeypatch, module_name, start_name, request_name, status_name):
    import importlib
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, '_token_records', lambda ids: [{'id':i} for i in ids])
    monkeypatch.setattr(module, '_sync_state', {'running':True})
    monkeypatch.setattr(module, status_name, lambda: {'running':True})
    queued = []
    monkeypatch.setattr(module, request_name, lambda ids: queued.extend(ids))
    started, state = getattr(module, start_name)([7])
    assert not started and state['running']
    assert queued == [7]


def test_web_and_private_worker_roundtrip_over_real_http(monkeypatch):
    import os
    import socket
    import subprocess
    import sys
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    source = '''
import os
from flask import Flask, g, request, session, jsonify
from werkzeug.serving import make_server
from bit.service_split import install_service_routing
app = Flask(__name__); app.secret_key = 'shared-test-secret'
@app.route('/api/order-sync/start', methods=['POST'])
def start():
    if not session.get('user'): return jsonify(error='login'), 401
    response = jsonify(user=session['user'], data=request.json, query=request.query_string.decode(), remote=g.worker_original_remote_addr)
    response.set_cookie('one','1'); response.set_cookie('two','2')
    return response
install_service_routing(app)
make_server('127.0.0.1', int(os.environ['BIT_WORKER_PORT']), app).serve_forever()
'''
    env = {**os.environ, 'BIT_SERVICE_MODE':'worker', 'BIT_WORKER_PORT':str(port)}
    process = subprocess.Popen([sys.executable, '-c', source], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2): break
            except OSError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail('private test worker did not start')
                time.sleep(.02)
        monkeypatch.setenv('BIT_SERVICE_MODE','web')
        monkeypatch.setenv('BIT_WORKER_PORT',str(port))
        app = Flask(__name__); app.secret_key = 'shared-test-secret'
        service_split.install_service_routing(app)
        client = app.test_client()
        assert client.post('/api/order-sync/start', json={}).status_code == 401
        with client.session_transaction() as state:
            state['user'] = 7
        response = client.post('/api/order-sync/start?search=%41&tail=?', json={'token_ids':[4]},
            headers={'X-Workbench-Original-IP':'127.0.0.1', 'X-Forwarded-For':'127.0.0.1'},
            environ_overrides={'REMOTE_ADDR':'198.51.100.10'})
        assert response.status_code == 200
        assert response.json == {'user':7, 'data':{'token_ids':[4]}, 'query':'search=A&tail=?', 'remote':'198.51.100.10'}
        assert len(response.headers.getlist('Set-Cookie')) == 2
        response.close()
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_hundred_agent_heartbeats_over_http_with_four_workers(tmp_path, monkeypatch):
    import urllib.request
    from waitress import create_server
    from bit import bit_interface as interface
    store = LocalAgentStore(tmp_path / 'agents.sqlite3')
    monkeypatch.setattr(interface, 'USE_DB_API', False)
    monkeypatch.setattr(interface, 'get_current_workbench_user', lambda: None)
    monkeypatch.setattr(interface, 'get_local_agent_store', lambda: store)
    monkeypatch.setattr(interface, 'current_local_agent_bundle', lambda: {'version':'test','sha256':'test','size':0})
    monkeypatch.setattr(interface, '_verify_local_agent_credential',
                        lambda token: {'agent_id':token,'user_id':0} if token.startswith('load-agent-') else None)
    monkeypatch.setenv('BIT_SERVICE_MODE','combined')
    server_map = {}
    server = create_server(interface.app, host='127.0.0.1', port=0, threads=4, map=server_map)
    stopping = threading.Event()
    def run():
        while not stopping.is_set():
            server.asyncore.loop(timeout=.02, count=1, map=server_map)
    thread = threading.Thread(target=run, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.effective_port}'
    def heartbeat(index):
        agent = f'load-agent-{index}'
        body = json.dumps({'agent_id':agent,'name':agent,'session_id':'load-session',
                           'capabilities':['heartbeat_claim']}).encode()
        req = urllib.request.Request(base + '/api/local-agents/heartbeat', data=body,
              headers={'Content-Type':'application/json','X-Local-Agent-Token':agent})
        started = time.monotonic()
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.load(response)['data']
        assert data['job'] is None and data['agent']['agent_id'] == agent
        return time.monotonic() - started
    try:
        with concurrent.futures.ThreadPoolExecutor(32) as pool:
            timings = sorted(pool.map(heartbeat, range(100)))
        assert len(store.list_agents()) == 100
        print(f'Isolated Agent heartbeat: 100 requests, 4 HTTP workers, P95={timings[94]:.3f}s, max={timings[-1]:.3f}s')
    finally:
        stopping.set(); thread.join(timeout=2)
        server.task_dispatcher.shutdown(); server.close()
        for channel in list(server_map.values()): channel.close()
