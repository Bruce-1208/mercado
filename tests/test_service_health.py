from bit import service_health


def test_independent_failures_do_not_hide_other_services(monkeypatch):
    def broken():
        raise RuntimeError('secret hostname/password')
    monkeypatch.setattr(service_health, 'database_probe', broken)
    result = service_health.service_snapshot(lambda: True)['services']
    assert [row['status'] for row in result] == ['ok', 'error', 'ok']
    assert all(row['checked_at'].endswith('+00:00') for row in result)
    assert 'secret' not in str(result)


def test_false_health_response_is_not_healthy(monkeypatch):
    monkeypatch.setattr(service_health, 'database_probe', lambda: {
        'status': 'unknown', 'checked_at': None, 'detail': '未检测'})
    result = service_health.service_snapshot(lambda: False)['services']
    assert result[1]['status'] == 'unknown'
    assert result[1]['checked_at'] is None
    assert result[2]['status'] == 'error'


def test_database_probe_executes_query_and_closes_connection(monkeypatch):
    import sys
    from types import SimpleNamespace
    calls = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql): calls.append(sql)
        def fetchone(self): return {'healthy': 1}
    class Connection:
        def cursor(self): return Cursor()
        def close(self): calls.append('close')
    from bit import bit_db_api
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'mysql')
    monkeypatch.setitem(sys.modules, 'bit.bit_mysql', SimpleNamespace(config={}))
    import pymysql
    def connect(**kwargs):
        assert kwargs['read_timeout'] == 3
        return Connection()
    monkeypatch.setattr(pymysql, 'connect', connect)
    assert service_health.database_probe()['status'] == 'ok'
    assert calls == ['SELECT 1 AS healthy', 'close']


def test_legacy_remote_health_is_unknown(monkeypatch):
    from bit import bit_db_api
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    monkeypatch.setattr(bit_db_api, 'get_database_api_health', lambda: {'role': 'server'})
    assert service_health.database_probe()['status'] == 'unknown'
