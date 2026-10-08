from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import pytest
from erp.store_link_visit_schedule import interval_days, next_refresh, enqueue
from bit import store_link_visit_scheduler as scheduler

NOW=datetime(2026,10,7,12)

@pytest.mark.parametrize('listed,days',[
    (NOW,1),(NOW-timedelta(days=13,hours=23,minutes=59),1),
    (NOW-timedelta(days=14),7),(NOW-timedelta(days=100),7),
    (None,7),('invalid',7),(NOW+timedelta(days=1),7),
    ('2026-10-07T20:00:00+08:00',1),
])
def test_actual_listing_age_boundary(listed,days):
    assert interval_days(listed,NOW)==days
    assert next_refresh(listed,NOW)==NOW+timedelta(days=days)


def test_failure_retries_without_waiting_a_week():
    assert next_refresh(None,NOW,success=False)==NOW+timedelta(hours=6)


def test_enqueue_does_not_reset_weekly_due_on_regular_listing_sync():
    cursor=Mock()
    enqueue(cursor,[{'token_id':1,'item_id':'MLM1','listed_at':None}])
    sql,values=cursor.executemany.call_args.args
    assert 'AND next_sync_at>UTC_TIMESTAMP()+INTERVAL 1 DAY' in sql
    assert 'UTC_TIMESTAMP(),next_sync_at)' in sql
    assert values==[(1,'MLM1',None)]


def test_worker_skips_ineligible_and_preserves_failed_values(monkeypatch):
    db=Mock();cursor=Mock();db.cursor.return_value.__enter__=Mock(return_value=cursor)
    db.cursor.return_value.__exit__=Mock(return_value=False)
    monkeypatch.setattr(scheduler,'connect',lambda:db)
    monkeypatch.setattr(scheduler.time,'sleep',lambda _:None)
    client=Mock();monkeypatch.setattr(scheduler,'MercadoLibreClient',lambda *a,**k:client)
    fetch=Mock(side_effect=[{'total_visits':0,'visits_updated_at':NOW,'visits_error':''},
                           {'total_visits':None,'visits_updated_at':None,'visits_error':'failed'}])
    monkeypatch.setattr(scheduler,'fetch_total_visits',fetch)
    save=Mock();monkeypatch.setattr(scheduler,'save_visits',save)
    rows=[{'token_id':1,'item_id':f'MLM{i}','listed_at':None,'enabled':True,'access_token':'test','is_current':True} for i in range(4)]
    rows[2]['enabled']=False;rows[3]['is_current']=False
    assert scheduler.run_batch(rows)=={'checked':2,'updated':1,'failed':1}
    assert fetch.call_count==2
    saved=save.call_args.args[1]
    assert saved[0]['total_visits']==0 and saved[1]['total_visits'] is None
    assert len(cursor.executemany.call_args.args[1])==4
    db.commit.assert_called_once()
    db.close.assert_called_once()
