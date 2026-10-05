import pytest
from bit.ai_weight_price_alerts import check_pauses
from bit.ai_weight_price_client import dispatch, fail, login_open
from erp.ai_weight_price.service import Service


def service(tmp_path):
    s = Service(tmp_path)
    s.bind_actor({'id': 7})
    return s


def test_pause_mail_retries_until_email_sent_and_survives_restart(tmp_path):
    s = service(tmp_path)
    s.store.set_state('circuit', {'reason': '请完成人机验证', 'at': 100})
    calls = []
    def sender(*args):
        calls.append(args)
        if len(calls) == 1:
            raise OSError('SMTP temporarily down')
        return {'sent': True}
    check_pauses(s.store, 'secret', now=399, sender=sender)
    assert not calls
    check_pauses(s.store, 'secret', now=400, sender=sender)
    assert len(calls) == 1
    assert not s.store.state('pause_email')['sent']
    check_pauses(s.store, 'secret', now=430, sender=sender)
    assert len(calls) == 1
    check_pauses(s.store, 'secret', now=460, sender=sender)
    assert s.store.state('pause_email')['sent']
    restarted = service(tmp_path)
    check_pauses(restarted.store, 'secret', now=800, sender=sender)
    assert len(calls) == 2
    restarted.store.set_state('circuit', None)
    check_pauses(restarted.store, 'secret', now=900, sender=sender)
    assert len(calls) == 2


def test_late_login_open_preserves_confirmation(tmp_path):
    s = service(tmp_path)
    s.store.set_state('login', {'confirmed': True, 'confirmed_at': 50})
    login_open(s)
    assert s.store.state('login')['confirmed']


def test_captcha_failure_is_resumable_pause(tmp_path):
    s = service(tmp_path)
    s.store.set_state('run', {'run_id': 'r', 'outcome': 'running'})
    fail(s, {'error': '1688需要完成人机验证', 'action': 'search'})
    assert s.store.state('run')['outcome'] == 'blocked'
    assert s.store.state('circuit')['at'] > 0


def test_stale_batch_and_duplicate_step_cannot_mutate_current_run(tmp_path):
    s = service(tmp_path)
    s.store.set_state('run', {'run_id': 'new', 'outcome': 'running'})
    with pytest.raises(ValueError, match='旧批次'):
        dispatch(s, 'fail', {'run_id': 'old', 'error': 'old failure'})
    s.store.set_state('client_step', {'token': 'current'})
    with pytest.raises(ValueError, match='过期'):
        dispatch(s, 'fail', {'run_id': 'new', 'step_token': 'old'})
    assert s.store.state('run')['outcome'] == 'running'
    s.store.set_state('stop_requested', True)
    with pytest.raises(ValueError, match='停止'):
        dispatch(s, 'fail', {'run_id': 'new', 'step_token': 'current'})
    assert not s.store.state('circuit')
