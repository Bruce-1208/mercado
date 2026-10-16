import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from flask import Flask

from bit import automation_center as center
from erp.package_update_weight import package_update_weight_g


@pytest.mark.parametrize('hours,expected', [(-1, False), (0, False), (.01, True), (24, True), (24.001, False)])
def test_deadline_window(hours, expected):
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    assert center.due_for_reply((now + timedelta(hours=hours)).isoformat(), now) is expected


def test_beijing_naive_deadline_and_bad_dates():
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    assert center.due_for_reply('2026-10-12 08:00:00', now)
    assert not center.due_for_reply('2026-10-12 08:00:01', now)
    assert not center.due_for_reply(None, now)
    assert not center.due_for_reply('invalid', now)


@pytest.mark.parametrize('actual,declared,expected', [('500', '2000', '500'), ('499', '2000', '499'), ('501', '100', '1000')])
def test_automatic_weight_threshold_only_uses_actual(actual, declared, expected):
    assert package_update_weight_g(dict(actual_weight_g=actual, declared_weight_g=declared,
        actual_dimensions_cm='30x20x10', _automation_actual_weight_only=True)) == expected


def test_large_weight_missing_dimensions_is_not_guessed():
    with pytest.raises(ValueError):
        package_update_weight_g(dict(actual_weight_g='501', _automation_actual_weight_only=True))


def test_disabled_rule_has_no_job_or_side_effect(monkeypatch):
    monkeypatch.setattr(center, 'enabled', lambda *a: False)
    begin, operation = Mock(), Mock()
    monkeypatch.setattr(center, 'begin', begin)
    center.execute_job('company-a', 'freight', 'job', 'subject', operation)
    begin.assert_not_called()
    operation.assert_not_called()


def test_completed_or_ambiguous_job_is_not_repeated(monkeypatch):
    monkeypatch.setattr(center, 'enabled', lambda *a: True)
    monkeypatch.setattr(center, 'begin', lambda *a: None)
    operation = Mock()
    center.execute_job('company-a', 'rights_reply', 'job', 'subject', operation)
    operation.assert_not_called()


def test_operation_failure_is_journaled(monkeypatch):
    monkeypatch.setattr(center, 'enabled', lambda *a: True)
    monkeypatch.setattr(center, 'begin', lambda *a: 7)
    journal = Mock()
    monkeypatch.setattr(center, 'journal', journal)
    center.execute_job('company-a', 'freight', 'job', 'subject', Mock(side_effect=ValueError('missing size')))
    assert journal.call_args.args == (7, {'error': 'missing size'}, 'failed')


def test_latest_measurements_exclude_other_enterprises_and_old_orders():
    base = dict(title='Same', _token_id=1, _dimension_compensation_confirmed=True)
    old = dict(base, time='2026-10-10', order_number='1')
    new = dict(base, time='2026-10-11', order_number='2')
    other = dict(new, _token_id=2, time='2026-10-12')
    unconfirmed = dict(new, _dimension_compensation_confirmed=False, time='2026-10-13')
    assert center.latest_freight_records([new, old, other, unconfirmed], [1]) == [new]


def test_reply_rechecks_authoritative_deadline_before_send(monkeypatch):
    from bit import mercado_rights_reply as replies
    client = Mock()
    monkeypatch.setattr(center, 'journal', Mock())
    monkeypatch.setattr(replies, 'store_client', lambda _: (client, {}))
    monkeypatch.setattr(replies, 'read_case', lambda *a: {'current_status':'WAITING_DOCUMENTATION', 'due_date':(datetime.now(timezone.utc)+timedelta(days=3)).isoformat()})
    send = Mock()
    monkeypatch.setattr(replies, 'reply_case', send)
    result = center.reply({'id':1}, {'infraction_id':'2', 'item_id':'MLM1'}, 3)
    assert result['automation_status'] == 'skipped'
    send.assert_not_called()
    client.session.close.assert_called_once()


@pytest.mark.parametrize('role,code', [('member',403), ('enterprise_admin',200), ('super_admin',200)])
def test_routes_require_manager_and_derive_scope(role, code, monkeypatch):
    from bit import bit_db_api
    call = Mock(return_value={'settings':{},'runs':[]})
    monkeypatch.setattr(bit_db_api, '_mercado_action_center_call', call)
    app=Flask(__name__)
    app.register_blueprint(center.create_blueprint(login_required=lambda f:f,
        current_user=lambda:{'id':9,'role_key':role,'organization_key':'company-a'},
        selected_organization=lambda _: '', has_permission=lambda *a:True))
    response=app.test_client().patch('/api/automation-center?organization_key=company-b',json={'freight':True})
    assert response.status_code == code
    if code == 200:
        assert call.call_args.args[1]['organization_key'] == 'company-a'
        assert call.call_args.args[1]['data'] == {'freight':True}
    else:
        call.assert_not_called()


def test_retry_route_accepts_single_or_batch_ids_and_keeps_enterprise_scope(monkeypatch):
    from bit import bit_db_api
    call = Mock(return_value={
        'settings': {}, 'runs': [], 'next_before': None,
        'retry': {
            'accepted': [{'source_run_id': 3, 'run_id': 30}],
            'rejected': [{'source_run_id': 4, 'reason': '执行结果待核实'}],
        },
    })
    monkeypatch.setattr(bit_db_api, '_mercado_action_center_call', call)
    app = Flask(__name__)
    app.register_blueprint(center.create_blueprint(
        login_required=lambda function: function,
        current_user=lambda: {'id': 9, 'role_key': 'enterprise_admin', 'organization_key': 'company-a'},
        selected_organization=lambda _: '',
        has_permission=lambda *args: True,
    ))

    response = app.test_client().post(
        '/api/automation-center?organization_key=company-b',
        json={'run_ids': [3, 4]},
    )

    assert response.status_code == 202
    payload = call.call_args.args[1]
    assert payload['organization_key'] == 'company-a'
    assert payload['actor_id'] == 9
    assert payload['action'] == 'retry'
    assert payload['data'] == {'run_ids': [3, 4]}
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.get_json()['data'] == {
        'requested_count': 2,
        'queued_count': 1,
        'skipped_count': 1,
        'items': [
            {'run_id': 3, 'status': 'queued', 'retry_run_id': 30},
            {'run_id': 4, 'status': 'skipped', 'reason': '执行结果待核实'},
        ],
    }


@pytest.mark.parametrize('fail_backfill', [True,False])
def test_local_package_must_succeed_before_net_proceeds(monkeypatch, fail_backfill):
    from bit import weight_dimensions_records as weights
    row=dict(order_number='1', query_status='已读取', _dimension_compensation_confirmed=True,
             _token_id=1, _global_item_id='CBT1', marketplace_item_id='MLM1',
             actual_weight_g='600', actual_dimensions_cm='30x20x10',
             _automation_run_id=1, _automation_organization='company-a', _automation_actual_weight_only=True)
    calls=[]
    client=Mock(access_token='test',timeout=10)
    client.get_marketplace_item.return_value={'id':'MLM1','title':'Same','cbt_item_id':'CBT1','net_proceeds':[{'currency_id':'USD','amount':11}]}
    client.update_global_item.side_effect=lambda *a,**kw:calls.append('net')
    def backfill(*a):
        calls.append('products')
        if fail_backfill: raise ValueError('local write failed')
        return {'product_count':2}
    monkeypatch.setattr(center,'backfill_local_package',backfill)
    monkeypatch.setattr(center,'journal',Mock())
    monkeypatch.setattr(center,'require_enabled',Mock())
    monkeypatch.setattr(weights,'_tasks',{'task':{'owner':'automation','records':[row],'authorized_ids':[1]}})
    monkeypatch.setattr(weights.bit_mysql,'get_mercado_store_token',lambda _: {})
    monkeypatch.setattr(weights,'_client_and_token',lambda _: (client,{}))
    monkeypatch.setattr(weights,'MercadoLibreClient',lambda *a,**kw:client)
    monkeypatch.setattr(weights.bit_db_api,'list_mercado_store_links',lambda **kw:{'pages':1,'rows':[]})
    monkeypatch.setattr(weights.bit_db_api,'list_mercado_store_site_settings',lambda *a,**kw:[])
    monkeypatch.setattr(weights.bit_db_api,'append_weight_dimensions_record_log',Mock())
    monkeypatch.setattr(weights,'_package_attributes',lambda *a:calls.append('package'))
    weights._run_execute('task','automation','zeshun')
    assert calls == (['package','products'] if fail_backfill else ['package','products','net'])
    assert row['_zeshun_status'] == ('失败' if fail_backfill else '成功')


def test_freight_preflight_records_missing_global_id_as_an_explicit_failure(monkeypatch):
    from bit import weight_dimensions_records as weights
    monkeypatch.setattr(weights.bit_db_api, 'append_weight_dimensions_record_log', Mock())
    row = {
        'order_number': 'ORDER-1', 'query_status': '已读取',
        '_dimension_compensation_confirmed': True, '_token_id': 7,
        '_global_item_id': '', 'marketplace_item_id': 'MCO123456789',
        'actual_weight_g': '600',
    }

    assert weights._expand_same_title_records([row], {7}) == []
    assert row['_zeshun_status'] == '失败'
    assert row['execution_status'] == '失败'
    assert row['execution_error'] == '商品没有可更新的 Global Selling 产品编号'
    assert row['execution_logs'][-1]['message'] == row['execution_error']


def test_local_package_resets_ai_original_reviews_when_measurements_change(monkeypatch):
    from erp import mercadolibre_collection_store as products
    from erp import mercadolibre_store_link_store as links
    from erp import rights_holder_policy, short_transaction

    connection, cursor = Mock(), Mock()
    connection.cursor.return_value.__enter__ = Mock(return_value=cursor)
    connection.cursor.return_value.__exit__ = Mock(return_value=False)
    snapshot = {'marketplace_listings': {
        'amazon': {'review_status': 'approved'},
        'yandex': {'review_status': 'approved'},
    }}
    locked = [{
        'id': 4, 'title': 'Same', 'source_type': 'ai_original',
        'weight_g': 100, 'package_length_cm': 1, 'package_width_cm': 2,
        'package_height_cm': 3, 'review_status': 'approved',
        'source_snapshot_json': json.dumps(snapshot),
    }]
    monkeypatch.setattr(center, 'connect', lambda: connection)
    monkeypatch.setattr(center, 'require_enabled', lambda *args: None)
    monkeypatch.setattr(products, 'ensure_collection_tables', lambda _: None)
    monkeypatch.setattr(links, 'ensure_store_link_table', lambda _: None)
    monkeypatch.setattr(rights_holder_policy, 'lock_matching_products', lambda *args: locked)
    monkeypatch.setattr(short_transaction, 'run', lambda _connection, operation: operation())

    result = center.backfill_local_package({
        '_automation_organization': 'company-a', '_token_id': 7,
        'marketplace_item_id': 'MLM1', 'actual_dimensions_cm': '10x20x30',
        '_history_context': {'link_name': 'Same'},
    }, 600)

    assert result['product_count'] == 1
    reset = next(call.args for call in cursor.execute.call_args_list
                 if "`review_status`='unreviewed'" in call.args[0])
    saved = json.loads(reset[1][0])
    assert saved['marketplace_listings']['amazon']['review_status'] == 'unreviewed'
    assert saved['marketplace_listings']['yandex']['review_status'] == 'unreviewed'


@pytest.mark.parametrize('status,age,skip', [('success',9000,True),('unknown',9000,True),('running',20,True),('failed',20,True),('failed',3601,False),('running',3601,False)])
def test_durable_job_dedup_and_retry_cooldown(monkeypatch,status,age,skip):
    cursor=Mock()
    cursor.fetchone.return_value={'id':1,'status':status,'age':age}
    cursor.lastrowid=2
    connection=Mock()
    connection.cursor.return_value.__enter__=Mock(return_value=cursor)
    connection.cursor.return_value.__exit__=Mock(return_value=False)
    monkeypatch.setattr(center,'connect',lambda:connection)
    monkeypatch.setattr(center,'ensure_tables',lambda _:None)
    assert center.begin('company-a','freight','identity','subject') == (None if skip else 2)
    assert any('ORDER BY id DESC LIMIT 1 FOR UPDATE' in call.args[0]
               for call in cursor.execute.call_args_list)
    writes=[call for call in cursor.execute.call_args_list if call.args[0].startswith('INSERT')]
    assert len(writes)==(0 if skip else 1)
    if status=='running' and not skip:
        assert any("status='interrupted'" in call.args[0] for call in cursor.execute.call_args_list)


def test_log_filters_are_scoped_and_applied_before_pagination():
    sql, args = center.log_filter_sql('company-a', {'before':90,'filters':{
        'salesperson':'王_50%', 'store':'墨西哥店', 'type':'freight','status':'failed',
        'date_from':'2026-10-10T00:00','date_to':'2026-10-11T23:59',
    }})
    assert sql.startswith('organization_key=%s')
    assert 'JSON_VALID(workbench_automation_runs.detail_json)' in sql
    assert 'JSON_VALID(event.detail_json)' in sql
    assert 'JSON_VALID(token_event.detail_json)' in sql
    assert 'rule_key=%s' in sql and 'status=%s' in sql
    assert "'$**.salesperson'" in sql and "'$**.source_token_id'" in sql
    assert 'mercado_store_tokens' in sql and 't.organization_key' in sql
    assert args[:3]==('company-a','freight','failed')
    assert datetime(2026,10,12) in args
    assert '%王!_50!%%' in args
    assert sql.endswith('id < %s') and args[-1]==90
    assert '墨西哥店' not in sql


@pytest.mark.parametrize('filters', [
    {'type':'other'}, {'status':'other'}, {'date_from':'bad'},
    {'date_from':'2026-10-12T00:00','date_to':'2026-10-11T00:00'},
    {'store':'x'*129}, {'salesperson':'x'*129},
])
def test_invalid_log_filters_are_rejected(filters):
    with pytest.raises(ValueError):
        center.log_filter_sql('company-a', {'filters':filters})


def test_log_filter_timezones_and_reset():
    sql,args=center.log_filter_sql('company-a',{'filters':{'date_from':'2026-10-11T00:00:00Z'}})
    assert args[-1]==datetime(2026,10,11,8)
    assert center.log_filter_sql('company-a',{})==('organization_key=%s',('company-a',))


def test_retry_ids_are_unique_sorted_positive_json_integers():
    assert center._retry_ids({'run_ids': [7, 2, 7]}) == [2, 7]


@pytest.mark.parametrize('data', [
    None,
    {},
    {'run_ids': []},
    {'run_ids': '7'},
    {'run_ids': [0]},
    {'run_ids': [-1]},
    {'run_ids': [True]},
    {'run_ids': [1.5]},
    {'run_ids': ['7']},
])
def test_retry_ids_reject_malformed_or_coercible_values(data):
    with pytest.raises(ValueError, match='重试|编号'):
        center._retry_ids(data)


def test_retry_ids_enforce_batch_limit():
    with pytest.raises(ValueError, match='50'):
        center._retry_ids({'run_ids': list(range(1, center.MAX_MANUAL_RETRIES + 2))})


def test_failure_reason_prefers_latest_failed_summary_and_keeps_diagnostics():
    detail = [
        {'status': 'error', 'errors': ['平台拒绝修改']},
        {'status': 'success', 'message': '这是成功提示，不是失败原因'},
        {'result': {
            'automation_status': 'failed',
            'message': '最终仍有 1 条链接未处理',
            'links': [{'status': 'partial', 'execution_error': '本地回写超时'}],
        }},
    ]

    reason = center.failure_reason(detail)

    assert reason.startswith('最终仍有 1 条链接未处理')
    assert '本地回写超时' in reason
    assert '平台拒绝修改' in reason
    assert '这是成功提示' not in reason


def test_serialized_failure_reason_is_bounded_for_list_responses():
    row = {
        'status': 'failed', 'rule_key': 'freight', 'subject': '订单 1',
        'failure_reason': '错' * 5000, 'is_latest': True,
        'started_at': '2026-10-14 10:00:00', 'updated_at': '2026-10-14 10:00:01',
    }

    result = center.serialize_run(row, {'freight': True})

    assert len(result['failure_reason']) == 1200


@pytest.mark.parametrize('status', ['unknown', 'success', 'running', 'queued', 'skipped'])
def test_retry_safety_blocks_non_retryable_statuses(status):
    reason = center.retry_blocked_reason(
        {'rule_key': 'freight', 'status': status, 'subject': '订单 1', 'is_latest': True},
        {'freight': True},
    )
    assert reason
    if status == 'unknown':
        assert '避免重复写入' in reason


@pytest.mark.parametrize('status', ['failed', 'interrupted'])
def test_retry_safety_allows_only_latest_enabled_failures(status):
    run = {'rule_key': 'freight', 'status': status, 'subject': '订单 1', 'is_latest': True}
    assert center.retry_blocked_reason(run, {'freight': True}) == ''
    assert center.retry_blocked_reason({**run, 'is_latest': False}, {'freight': True})
    assert center.retry_blocked_reason(run, {'freight': False})


def test_retry_context_uses_initial_source_not_nested_target_results():
    detail = [
        {'token_id': 7, 'reports': [{'source_id': 'case-1', 'title': '源商品'}]},
        {'result': {'links': [{'token_id': 99, 'item_id': 'MCO123456789'}]}},
    ]

    assert center.retry_context('rights_cleanup', detail) == {
        'token_id': 7,
        'source_ids': ['case-1'],
        'title': '源商品',
    }


def test_cleanup_retry_keeps_an_explicitly_empty_historical_title(monkeypatch):
    rows = [{'source_id': 'case-1', 'title': '', 'item_id': 'MCO123'}]
    token = {'id': 7, 'organization_key': 'company-a'}
    monkeypatch.setattr(center, '_scoped_tokens', lambda _organization: [token])
    monkeypatch.setattr(center, '_current_cleanup_reports', lambda _token_id: rows)
    cleanup = Mock(return_value={'automation_status': 'success'})
    monkeypatch.setattr(center, 'cleanup', cleanup)

    result = center._retry_cleanup(
        {'organization_key': 'company-a', 'subject': 'MCO123', 'id': 30},
        {'token_id': 7, 'source_ids': ['case-1'], 'title': ''},
    )

    assert result == {'automation_status': 'success'}
    cleanup.assert_called_once_with(token, rows, 30)


def test_cleanup_identity_preserves_legacy_hash_until_target_eligibility_changes():
    reports = [{'source_id': 'case-1'}]
    enabled = [{
        'token_id': 8, 'item_id': 'MCO123456789', 'site_id': 'MCO',
        'store_enabled': True, 'is_current': 1, 'status': 'active',
    }]
    legacy = json.dumps([7, '源商品', ['case-1'], [(8, 'MCO123456789')]])

    assert center._cleanup_identity(7, '源商品', reports, enabled) == legacy
    disabled_identity = center._cleanup_identity(
        7, '源商品', reports, [{**enabled[0], 'store_enabled': False}],
    )
    invalid_identity = center._cleanup_identity(
        7, '源商品', reports, [{**enabled[0], 'site_id': 'MPE'}],
    )

    assert disabled_identity != legacy
    assert invalid_identity != legacy
    assert disabled_identity != invalid_identity
    assert center._cleanup_identity(7, '源商品', reports, enabled) == legacy


def test_orphaned_runs_make_replies_unknown_but_idempotent_rules_interrupted():
    cursor = Mock()

    center._recover_interrupted_runs(cursor)

    sql = cursor.execute.call_args.args[0]
    assert "WHEN rule_key='rights_reply' THEN 'unknown'" in sql
    assert "ELSE 'interrupted'" in sql
    assert "WHERE status='running'" in sql


def test_scheduler_scans_regular_rules_before_draining_manual_retries(monkeypatch):
    from bit import bit_db_api
    cursor, connection = Mock(), Mock()
    connection.cursor.return_value.__enter__ = Mock(return_value=cursor)
    connection.cursor.return_value.__exit__ = Mock(return_value=False)
    cursor.fetchone.return_value = {'acquired': 1}
    cursor.fetchall.return_value = [{
        'organization_key': 'company-a', 'rights_reply': 1,
        'rights_cleanup': 0, 'freight': 0,
    }]
    events = []
    monkeypatch.setattr(center, 'connect', lambda: connection)
    monkeypatch.setattr(center, 'ensure_tables', lambda _cursor: None)
    monkeypatch.setattr(center, '_recover_interrupted_runs', lambda _cursor: None)
    monkeypatch.setattr(center, '_backfill_run_summaries', lambda _cursor: None)
    monkeypatch.setattr(bit_db_api, 'list_mercado_store_tokens', lambda: {
        'rows': [{'id': 7, 'enabled': 1, 'organization_key': 'company-a'}],
    })
    monkeypatch.setattr(center, 'scan_rule',
                        lambda rule, *_args: events.append(f'scan:{rule}'))
    monkeypatch.setattr(center, '_claim_queued_retry',
                        lambda _cursor: events.append('claim') or None)

    center.run_cycle()

    assert events == ['scan:rights_reply', 'claim']


def test_private_retry_context_is_never_returned_in_public_detail():
    detail = [{
        'stage': '手动重试已排队',
        center._RETRY_CONTEXT_KEY: {'token_id': 7, 'case_id': 'case-1'},
        'nested': {center._RETRY_CONTEXT_KEY: {'order_number': 'secret'}, 'message': '可见'},
    }]

    assert center.public_detail(detail) == [{
        'stage': '手动重试已排队',
        'nested': {'message': '可见'},
    }]


@pytest.mark.parametrize("stored,expected", [(None, False), ({}, False), ({"publish_infringement": 0}, False), ({"publish_infringement": 1}, True)])
def test_publish_policy_defaults_off_and_is_scoped(monkeypatch, stored, expected):
    cursor, connection = Mock(), Mock()
    connection.cursor.return_value.__enter__ = Mock(return_value=cursor)
    connection.cursor.return_value.__exit__ = Mock(return_value=False)
    cursor.fetchone.return_value = stored
    monkeypatch.setattr(center, "connect", lambda: connection)
    monkeypatch.setattr(center, "ensure_tables", lambda cursor: None)
    assert center.local({"action": "publish_policy", "organization_key": "company-b"}) == {"enabled": expected}
    assert cursor.execute.call_args.args[1] == ("company-b",)
    connection.close.assert_called_once()
    assert "publish_infringement" not in center.SCHEDULED_RULES


def test_publish_switch_migrates_existing_settings_default_off(monkeypatch):
    cursor = Mock()
    cursor.fetchone.return_value = None
    monkeypatch.setattr(center, "_publish_setting_ready", False)
    monkeypatch.setattr(center, "_status_index_ready", True)
    center.ensure_tables(cursor)
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("ALTER TABLE workbench_automation_settings ADD COLUMN publish_infringement TINYINT NOT NULL DEFAULT 0" in sql for sql in statements)
