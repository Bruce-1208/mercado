import concurrent.futures
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mercado_api import client as client_module
from mercado_api.client import MercadoAPIError, MercadoLibreClient
from mercado_api.request_budget import AdmissionTimeout, RequestBudget


def test_admission_timeout_preserves_existing_slots_and_releases_successful_requests():
    budget = RequestBudget(total=2, per_account=1)
    token = "private-test-token"
    with budget.slot(token):
        with pytest.raises(AdmissionTimeout) as error:
            with budget.slot(token, timeout=0.02):
                pytest.fail("A second request for this account must wait")
        assert token not in str(error.value)
        assert budget.active == 1
        assert list(budget.accounts.values()) == [1]
        # A timed-out account must not use or release another account's slot.
        with budget.slot("another-account", timeout=0):
            assert budget.active == 2
            with pytest.raises(AdmissionTimeout):
                with budget.slot("third-account", timeout=0):
                    pytest.fail("The global budget is full")
            assert budget.active == 2
        assert budget.active == 1
    assert budget.active == 0
    assert not budget.accounts
    with pytest.raises(ValueError), budget.slot(token, timeout=0):
        raise ValueError("network failed after admission")
    assert budget.active == 0
    assert not budget.accounts


def test_default_admission_still_waits_for_capacity(monkeypatch):
    budget = RequestBudget(total=1, per_account=1)
    waiting = threading.Event()
    acquired = threading.Event()
    original_wait = budget.condition.wait

    def observe_wait(timeout=None):
        assert timeout is None
        waiting.set()
        return original_wait(timeout)

    monkeypatch.setattr(budget.condition, "wait", observe_wait)

    def request_once():
        with budget.slot("account"):
            acquired.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        with budget.slot("account"):
            future = executor.submit(request_once)
            assert waiting.wait(1)
            assert not acquired.is_set()
            assert budget.active == 1
        future.result(timeout=1)
    assert acquired.is_set()
    assert budget.active == 0


@pytest.mark.parametrize("method_name", ["request", "request_bytes"])
def test_client_queue_timeout_never_sends_or_retries_request(monkeypatch, method_name):
    budget = RequestBudget(total=1, per_account=1)
    admission = Mock(side_effect=budget.slot)
    session = Mock()
    sleep = Mock(side_effect=AssertionError("Queue timeouts must not retry"))
    monkeypatch.setattr(client_module, "admission", admission)
    monkeypatch.setattr(client_module.time, "sleep", sleep)
    token = "private-test-token"
    client = MercadoLibreClient(token, session=session, admission_timeout=0)
    with budget.slot(token):
        with pytest.raises(MercadoAPIError, match="请求队列繁忙") as error:
            getattr(client, method_name)("GET", "/items/MLB123", max_attempts=4)
        assert token not in str(error.value)
        assert budget.active == 1
    admission.assert_called_once_with(token, timeout=0)
    session.request.assert_not_called()
    sleep.assert_not_called()
    assert budget.active == 0
    assert not budget.accounts


@pytest.mark.parametrize("method_name", ["request", "request_bytes"])
@pytest.mark.parametrize("admission_timeout", [None, 2])
def test_client_admission_option_keeps_default_call_compatible(
    monkeypatch, method_name, admission_timeout
):
    observed = []

    @contextmanager
    def admission(token, **kwargs):
        observed.append(kwargs)
        yield

    response = SimpleNamespace(
        status_code=200, ok=True, content=b"pdf-content", json=lambda: {"id": "MLB123"}
    )
    session = Mock()
    session.request.return_value = response
    monkeypatch.setattr(client_module, "admission", admission)
    client = MercadoLibreClient(
        "test-token", timeout=7, session=session, admission_timeout=admission_timeout
    )
    result = getattr(client, method_name)("GET", "/items/MLB123")
    assert observed == ([{}] if admission_timeout is None else [{"timeout": 2}])
    assert session.request.call_args.kwargs["timeout"] == 7
    assert result == ({"id": "MLB123"} if method_name == "request" else b"pdf-content")
