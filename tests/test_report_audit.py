"""Regressions from the 2026-10-11 production audit; no business API writes."""
import copy
from unittest.mock import Mock, MagicMock

import pytest
from flask import Flask

from bit.bit_ad_analysis import _derived_metrics, _summary, _filtered_snapshot
from bit.bit_appeal_state import business_outcome
from bit.mercado_action_center import _today_store_matches
from erp.short_transaction import run
from erp.rights_holder_policy import lock_matching_products
from erp.mercadolibre_translation import translate_texts, title_language_issue
from bit.wipo_sync import WipoSyncService, DEFAULT_STATE


def test_ratios_use_raw_totals_including_legacy_values():
    metrics = _derived_metrics(dict(cost=184.56, total_amount=898.51, prints=1805460,
        clicks=3742, units_quantity=27, roas=27.58, ctr=.92, cpc=8, acos=999, cvr=40))
    assert metrics['roas'] == pytest.approx(898.51 / 184.56)
    assert metrics['ctr'] == pytest.approx(3742 / 1805460 * 100)
    assert metrics['cpc'] == pytest.approx(184.56 / 3742)
    assert metrics['acos'] == pytest.approx(184.56 / 898.51 * 100)
    assert metrics['cvr'] == pytest.approx(27 / 3742 * 100)
    rows = [dict(currency_id='MXN', metrics=dict(cost=100, total_amount=200, roas=2)),
            dict(currency_id='MXN', metrics=dict(cost=575.54, total_amount=2486.81, roas=4850.15))]
    summary = _summary(rows, rows, [])
    assert summary['roas'] == pytest.approx(2686.81 / 675.54)
    assert summary['currencies'][0]['metrics']['roas'] == summary['roas']


def test_unknown_and_mixed_currencies_are_not_aggregated():
    rows = [dict(currency_id=c, metrics=dict(cost=10, total_amount=40)) for c in ('MXN', 'BRL', '')]
    summary = _summary(rows, rows, [])
    assert summary['unknown_currency_count'] == 1
    assert summary['cost'] is None and summary['roas'] is None
    assert [row['currency_id'] for row in summary['currencies']] == ['BRL', 'MXN']
    assert all(row['metrics']['cost'] == 10 for row in summary['currencies'])
    assert not _summary(rows[-1:], [], [])['currencies']


def test_snapshot_read_repairs_ratios_without_mutation_or_currency_guess(monkeypatch):
    snapshot = {'accounts': [{'token_id':1, 'advertiser_id':2, 'site_id':'MLM',
        'metrics':{'cost':10,'total_amount':40,'roas':1234}}],
        'links':[{'token_id':1,'advertiser_id':2,'site_id':'MLM','item_id':'MLM1',
        'metrics':{'cost':10,'total_amount':40,'roas':1234}}]}
    original = copy.deepcopy(snapshot)
    result = _filtered_snapshot(snapshot, [1], page=1, summary_only=True)
    assert result['accounts'][0]['metrics']['roas'] == 4
    assert result['summary']['unknown_currency_count'] == 1
    assert result['summary']['cost'] is None
    assert snapshot == original


@pytest.mark.parametrize('currencies,advertiser_currency,expected', [
    (['', ''], 'USD', 'USD'), (['MXN', 'BRL'], '', ''), (['MXN', ''], '', ''),
])
def test_ad_account_currency_requires_consistent_explicit_billing_currency(monkeypatch, currencies, advertiser_currency, expected):
    from bit import bit_ad_analysis as analysis
    monkeypatch.setattr(analysis, '_campaigns', lambda *args: {i: {'currency_id': value} for i,value in enumerate(currencies, 1)})
    monkeypatch.setattr(analysis, '_campaign_groups', lambda *args: ([], {'cost': 10, 'total_amount': 40}))
    monkeypatch.setattr(analysis, '_enrich_images', lambda *args: None)
    result = analysis._advertiser_analysis(None, {'id':1},
        {'advertiser_id':2,'site_id':'MLM','currency_id':advertiser_currency}, '2026-10-01', '2026-10-10')
    assert result['account']['currency_id'] == expected


def test_lock_discovery_never_locks_unrelated_scan_rows():
    cursor = Mock()
    cursor.fetchall.side_effect = [[{'id':4}, {'id':2}], [{'id':2,'review_status':'approved'}]]
    rows = lock_matching_products(cursor, 'a', 'Exact title', 'id,review_status')
    discovery, locked = cursor.execute.call_args_list
    assert 'FOR UPDATE' not in discovery.args[0]
    assert 'FORCE INDEX (PRIMARY)' in locked.args[0] and 'id IN (%s,%s)' in locked.args[0]
    assert locked.args[1] == (2,4,'a','Exact title','Exact title','Exact title')
    assert rows == [{'id':2,'review_status':'approved'}]


def fake_connection():
    db = MagicMock()
    cursor = db.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {'lock_timeout':50}
    return db, cursor


@pytest.mark.parametrize('code', [1205,1213])
def test_short_writes_rollback_then_retry_and_restore_session(monkeypatch, code):
    monkeypatch.setattr('erp.short_transaction.time.sleep', lambda _: None)
    db, cursor = fake_connection()
    operation = Mock(side_effect=[RuntimeError(code,'lock'), {'ok':True}])
    assert run(db, operation) == {'ok':True}
    assert operation.call_count == 2
    db.rollback.assert_called_once()
    db.commit.assert_called_once()
    assert cursor.execute.call_args.args == ('SET SESSION innodb_lock_wait_timeout = %s', (50,))


def test_short_writes_stop_after_three_attempts_and_never_retry_unknown_errors(monkeypatch):
    monkeypatch.setattr('erp.short_transaction.time.sleep', lambda _:None)
    for error, count in [(RuntimeError(1205,'lock'),3),(RuntimeError(2006,'ambiguous commit'),1)]:
        db, _ = fake_connection()
        operation=Mock(side_effect=error)
        with pytest.raises(RuntimeError): run(db, operation)
        assert operation.call_count == count
        assert db.rollback.call_count == count
        db.commit.assert_not_called()


def test_known_madrid_retention_error_requires_repair_before_rescheduling(tmp_path):
    service=WipoSyncService('secret',path=tmp_path/'wipo.sqlite3')
    settings=service.settings()
    with service.database() as db:
        state={**DEFAULT_STATE,'status':'action_required','madrid_available_from':'2026-10-01'}
        service._write(db,'state',state)
    assert not service.tick()
    with pytest.raises(ValueError,match='官方可用范围'):
        service.save_settings({'madrid_from_date':'2026-09-01'})
    service.save_settings({'madrid_from_date':'2026-10-01'})
    assert service.status()['status']=='idle'
    assert service.status()['next_run_at']


def test_glossary_corrects_wind_up_question_without_model():
    model=Mock(side_effect=AssertionError('Known phrase should not need a model'))
    assert translate_texts(['São de dar corda?'], 'pt-BR','zh-CN',translator=model)==['是上发条的吗？']
    assert title_language_issue('Juguete 三段式 para niños', 'es')
    assert title_language_issue('Beautiful Wooden Toy Train For Small Children', 'es')
    assert not title_language_issue('Tren de madera para niños pequeños', 'es')


def test_appeal_send_never_implies_resolution():
    assert business_outcome({'sent':True,'acknowledged':True})=='pending_confirmation'
    assert business_outcome({'sent':True}, 'Please confirm the shipping method or switch to Fulfillment.')=='needs_human'
    assert business_outcome({'sent':True}, '请提供物流方式')=='needs_human'
    assert business_outcome({'business_status':'resolved'})=='resolved'


def test_closed_stores_require_explicit_history_scope():
    app=Flask(__name__)
    with app.test_request_context('/today'):
        assert not _today_store_matches({'enabled':False}, {})
        assert _today_store_matches({'enabled':True}, {})
    with app.test_request_context('/today?include_inactive=1'):
        assert _today_store_matches({'enabled':False}, {})
