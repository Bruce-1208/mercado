import threading
from unittest.mock import MagicMock, patch

import pytest

from bit import mercado_tokens


def _refresh(**kwargs):
    return mercado_tokens.refresh_and_save(
        123,
        get_token=MagicMock(),
        update_token=MagicMock(),
        **kwargs,
    )


def test_busy_token_thread_lock_fails_without_starting_oauth_or_process_lock():
    held_lock = threading.Lock()
    held_lock.acquire()
    try:
        with patch.object(mercado_tokens, '_token_refresh_lock', return_value=held_lock), \
                patch.object(mercado_tokens, '_refresh_and_save_unlocked') as refresh, \
                patch.object(mercado_tokens, 'InterProcessLock') as process_lock:
            with pytest.raises(mercado_tokens.MercadoTokenError, match='其他任务中刷新'):
                _refresh(lock_timeout=0.01)
        refresh.assert_not_called()
        process_lock.assert_not_called()
        # A failed waiter must not release the active job's lock.
        assert held_lock.locked()
    finally:
        held_lock.release()


def test_process_lock_receives_only_the_remaining_combined_wait_budget():
    thread_lock = MagicMock()
    process_lock = MagicMock()
    with patch.object(mercado_tokens, '_token_refresh_lock', return_value=thread_lock), \
            patch.object(mercado_tokens, 'InterProcessLock', return_value=process_lock), \
            patch.object(mercado_tokens.time, 'monotonic', side_effect=[100, 101.25]), \
            patch.object(mercado_tokens, '_refresh_and_save_unlocked', return_value={'id': 123}):
        assert _refresh(lock_timeout=2, timeout=8) == {'id': 123}
    thread_lock.acquire.assert_called_once_with(timeout=2)
    process_lock.acquire.assert_called_once_with(timeout=0.75)
    process_lock.release.assert_called_once()
    thread_lock.release.assert_called_once()


def test_busy_process_lock_releases_thread_lock_and_skips_oauth():
    thread_lock = threading.Lock()
    process_lock = MagicMock()
    process_lock.acquire.return_value = False
    with patch.object(mercado_tokens, '_token_refresh_lock', return_value=thread_lock), \
            patch.object(mercado_tokens, 'InterProcessLock', return_value=process_lock), \
            patch.object(mercado_tokens, '_refresh_and_save_unlocked') as refresh:
        with pytest.raises(mercado_tokens.MercadoTokenError, match='其他任务中刷新'):
            _refresh(lock_timeout=0)
    refresh.assert_not_called()
    process_lock.acquire.assert_called_once_with(timeout=0)
    process_lock.release.assert_not_called()
    assert not thread_lock.locked()


def test_refresh_failure_releases_both_acquired_locks():
    thread_lock = threading.Lock()
    process_lock = MagicMock()
    with patch.object(mercado_tokens, '_token_refresh_lock', return_value=thread_lock), \
            patch.object(mercado_tokens, 'InterProcessLock', return_value=process_lock), \
            patch.object(mercado_tokens, '_refresh_and_save_unlocked', side_effect=RuntimeError('OAuth unavailable')):
        with pytest.raises(RuntimeError, match='OAuth unavailable'):
            _refresh(lock_timeout=2)
    process_lock.release.assert_called_once()
    assert not thread_lock.locked()


def test_default_refresh_preserves_existing_lock_wait_behavior():
    thread_lock = MagicMock()
    process_lock = MagicMock()
    with patch.object(mercado_tokens, '_token_refresh_lock', return_value=thread_lock), \
            patch.object(mercado_tokens, 'InterProcessLock', return_value=process_lock), \
            patch.object(mercado_tokens, '_refresh_and_save_unlocked', return_value={'id': 123}):
        assert _refresh() == {'id': 123}
    thread_lock.acquire.assert_called_once_with()
    process_lock.acquire.assert_called_once_with(timeout=35)
    process_lock.release.assert_called_once()
    thread_lock.release.assert_called_once()
