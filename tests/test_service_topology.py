import pytest
from unittest.mock import Mock
from flask import Flask
from bit import service_split, service_topology as topology, workbench_services


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setenv('BIT_WORKER_ISOLATION', '1')
    monkeypatch.setenv('BIT_SERVICE_MODE', 'worker')
    monkeypatch.setenv('BIT_WORKER_GROUP', 'orders')


def test_five_services_have_independent_bounded_budgets(monkeypatch):
    monkeypatch.setenv('MERCADO_PROFIT_REFRESH_WORKERS', '32')
    envs = {name: workbench_services.child_environment(name) for name in ('web', *topology.GROUPS)}
    assert topology.validate_topology(envs) == 20
    assert [envs[name]['MYSQL_POOL_MAX_CONNECTIONS'] for name in envs] == ['6', '4', '4', '4', '2']
    assert len({envs[name]['BIT_LISTEN_PORT'] for name in topology.GROUPS}) == 4
    assert envs['maintenance']['MERCADO_PROFIT_REFRESH_WORKERS'] == '1'
    assert envs['orders']['MERCADO_ORDER_PRINT_STORE_WORKERS'] == '2'
    monkeypatch.setenv('BIT_TOTAL_DB_CONNECTIONS', '19')
    with pytest.raises(ValueError, match='超过总预算'):
        topology.validate_topology(envs)
    envs['stores']['BIT_LISTEN_PORT'] = envs['orders']['BIT_LISTEN_PORT']
    with pytest.raises(ValueError, match='端口重复'):
        topology.validate_topology(envs)


@pytest.mark.parametrize('path,group', [
    ('/api/order-sync/start', 'orders'), ('/api/db/order-sync/status', 'orders'),
    ('/api/orders/print', 'orders'), ('/api/db/orders/labels', 'orders'),
    ('/api/orders/purchase-tracking/status', 'orders'),
    ('/api/orders/zying-sync/start', 'orders'),
    ('/api/orders/zying-sync/confirm-login', 'orders'),
    ('/api/orders/zying-sync/status', 'orders'),
    ('/api/local-agents/zying-sync/test-job', 'orders'),
    ('/api/store-links/sync/start', 'stores'), ('/api/db/store-links/sync/status', 'stores'),
    ('/api/store-links/bulk-update', 'stores'), ('/api/db/store-links/bulk-update/status', 'stores'),
    ('/api/overview-auto-sync', 'stores'), ('/api/mercado-tokens/exchange', 'stores'),
    ('/api/mercado-shipping-rates/refresh', 'maintenance'),
    ('/api/mercado-products/publish', 'worker'), ('/api/mercado-products/publish/status', 'worker'),
])
def test_task_requests_route_to_their_owner_without_retry(isolated, monkeypatch, path, group):
    monkeypatch.setenv('BIT_SERVICE_MODE', 'web')
    app = Flask(__name__)
    app.secret_key = 'test'
    service_split.install_service_routing(app)
    transport = Mock()
    transport.request.side_effect = service_split.requests.Timeout
    monkeypatch.setattr(service_split.requests, 'Session', lambda: transport)
    assert app.test_client().post(path).status_code == 503
    transport.request.assert_called_once()
    assert transport.request.call_args.args[1] == f'http://127.0.0.1:{topology.group_port(group)}{path}'


def test_background_ownership_prevents_lazy_scheduler_leaks(isolated, monkeypatch):
    from bit import bit_order_sync
    monkeypatch.setattr(bit_order_sync.threading, 'Thread', Mock(side_effect=AssertionError('wrong owner')))
    assert not bit_order_sync.ensure_order_financial_backfill_worker()
    assert not bit_order_sync.ensure_order_image_backfill_worker()
    monkeypatch.setenv('BIT_WORKER_GROUP', 'maintenance')
    assert not bit_order_sync.ensure_order_sync_scheduler()
    assert topology.owns_background('maintenance')
    monkeypatch.setenv('BIT_SERVICE_MODE', 'web')
    assert not topology.owns_background('maintenance')


def test_automatic_print_retries_admission_before_any_print(monkeypatch):
    from bit import bit_order_sync, bit_print
    from bit.db_pool import PoolAcquireTimeout
    monkeypatch.delenv(bit_order_sync.AUTO_PRINT_DISABLED_ENV, raising=False)
    jobs = Mock(side_effect=[PoolAcquireTimeout('busy'), [{'token_id': 7}]])
    monkeypatch.setattr(bit_print, 'build_print_jobs', jobs)
    monkeypatch.setattr(bit_order_sync, '_automatic_print_candidate_starts', lambda *a, **k: {})
    wait = Mock(return_value=False)
    monkeypatch.setattr(bit_order_sync, '_background_shutdown_event', Mock(wait=wait))
    lock = Mock()
    monkeypatch.setattr(bit_print, 'acquire_order_print_lock', lambda **k: lock)
    printing = Mock(return_value={'printed_order_count': 1, 'shipment_count': 1})
    monkeypatch.setattr(bit_print, 'print_orders_all', printing)
    assert bit_order_sync._run_automatic_order_print([7])['status'] == 'completed'
    assert jobs.call_count == 2
    wait.assert_called_once_with(2)
    printing.assert_called_once()
    lock.release.assert_called_once()


def test_wrong_worker_cannot_execute_a_signed_mutation(isolated):
    import time
    app = Flask(__name__)
    app.secret_key = 'test'
    service_split.install_service_routing(app)
    mutation = Mock(return_value='mutated')
    path = '/api/store-links/sync/start'
    app.add_url_rule(path, endpoint='mutation', view_func=lambda: mutation(), methods=['POST'])
    stamp = str(int(time.time()))
    signed = service_split.signature(app.secret_key, 'POST', path, b'', stamp)
    response = app.test_client().post(path, headers={service_split.HOP_HEADER: stamp + ':' + signed})
    assert response.status_code == 409
    mutation.assert_not_called()


def test_private_health_requires_signature_and_reports_actual_group(isolated):
    import time
    app = Flask(__name__)
    app.secret_key = 'test'
    service_split.install_service_routing(app)
    path = '/api/internal/worker-health'
    assert app.test_client().get(path).status_code == 403
    stamp = str(int(time.time()))
    signed = service_split.signature(app.secret_key, 'GET', path, b'', stamp)
    response = app.test_client().get(path, headers={service_split.HOP_HEADER: stamp + ':' + signed})
    assert response.status_code == 200
    assert response.json['group'] == 'orders'


def test_orders_worker_accepts_signed_zying_agent_callback(isolated):
    import time
    app = Flask(__name__)
    app.secret_key = 'test'
    service_split.install_service_routing(app)
    path = '/api/local-agents/zying-sync/test-job'
    callback = Mock(return_value='ready')
    app.add_url_rule(path, view_func=lambda: callback(), endpoint='callback', methods=['POST'])
    stamp = str(int(time.time()))
    signed = service_split.signature(app.secret_key, 'POST', path, b'', stamp)
    response = app.test_client().post(path, headers={service_split.HOP_HEADER: stamp + ':' + signed})
    assert response.status_code == 200
    callback.assert_called_once()


def test_crashing_group_does_not_restart_healthy_siblings(monkeypatch, tmp_path):
    monkeypatch.setenv('BIT_SERVICE_LOG_DIR', str(tmp_path))
    class Stop:
        loops = 0
        def is_set(self): return self.loops >= 8
        def wait(self, _seconds): self.loops += 1
        def set(self): self.loops = 8
    stop = Stop()
    monkeypatch.setattr(workbench_services.threading, 'Event', lambda: stop)
    monkeypatch.setattr(workbench_services.signal, 'signal', lambda *a: None)
    monkeypatch.setattr(workbench_services.time, 'monotonic', lambda: stop.loops * 100)
    monkeypatch.setattr(workbench_services, 'InterProcessLock', lambda *a, **k: Mock(acquire=lambda **k: True))
    spawned = []
    def spawn(*a, **kwargs):
        env = kwargs['env']
        group = 'web' if env['BIT_SERVICE_MODE'] == 'web' else env['BIT_WORKER_GROUP']
        spawned.append(group)
        return Mock(pid=len(spawned), poll=lambda: 1 if group == 'maintenance' else None)
    monkeypatch.setattr(workbench_services.subprocess, 'Popen', spawn)
    workbench_services.main()
    assert spawned.count('maintenance') > 1
    for group in ('web', 'orders', 'stores', 'worker'):
        assert spawned.count(group) == 1


@pytest.mark.parametrize('group,expected', [
    ('orders', {'orders', 'yandex'}),
    ('stores', {'links', 'link_recovery', 'prohibited', 'infractions', 'reputation'}),
    ('maintenance', {'profit', 'financial', 'images'}),
])
def test_bootstrap_starts_only_owned_schedulers(isolated, monkeypatch, group, expected):
    from bit import bit_interface as interface, yandex_order_sync
    monkeypatch.setenv('BIT_WORKER_GROUP', group)
    monkeypatch.setattr(interface, 'interface_background_services_disabled', lambda: False)
    monkeypatch.setattr(interface, 'start_browser_cleanup', lambda: None)
    monkeypatch.setattr(interface.bit_db_api, 'initialize_mercado_action_center_tables', Mock(side_effect=AssertionError('other group schema init')))
    called = set()
    for name, tag in [('ensure_mercado_profit_refresh_worker', 'profit'),
                      ('start_store_link_scheduler_bootstrap', 'links'),
                      ('start_prohibited_listing_scheduler_bootstrap', 'prohibited'),
                      ('start_official_infraction_scheduler_bootstrap', 'infractions'),
                      ('start_api_reputation_scheduler_bootstrap', 'reputation')]:
        monkeypatch.setattr(interface, name, lambda tag=tag: called.add(tag))
    for name, tag in [('ensure_order_sync_scheduler', 'orders'),
                      ('ensure_order_financial_backfill_worker', 'financial'),
                      ('ensure_order_image_backfill_worker', 'images')]:
        monkeypatch.setattr(interface.bit_order_sync, name, lambda tag=tag: called.add(tag))
    monkeypatch.setattr(yandex_order_sync, 'ensure_scheduler', lambda: called.add('yandex'))
    monkeypatch.setattr(interface.bit_store_link_remote_update, 'start_store_link_remote_update_recovery', lambda: called.add('link_recovery'))
    interface.start_interface_background_services()
    assert called == expected
    called.clear()
    monkeypatch.setattr(interface.app, 'testing', False)
    interface._start_order_sync_scheduler()
    assert called == (expected & {'orders', 'yandex', 'financial', 'images'})
