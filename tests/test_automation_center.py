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
