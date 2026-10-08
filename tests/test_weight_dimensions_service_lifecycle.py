from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bit import bit_interface as web


def service(monkeypatch, mode="worker", disabled=False):
    events = []
    locks = []

    def lock_factory(key, **kwargs):
        lock = Mock()
        lock.acquire.return_value = True
        lock.release.side_effect = lambda: events.append(("release", key))
        locks.append(lock)
        return lock

    monkeypatch.setattr(web, "service_mode", lambda: mode)
    monkeypatch.setattr(web, "RUNTIME_SETTINGS", SimpleNamespace(is_server=True, role="server"))
    monkeypatch.setattr(web, "interface_background_services_disabled", lambda: disabled)
    monkeypatch.setattr(web, "InterProcessLock", lock_factory)
    monkeypatch.setattr(web.weight_dimensions_records.bit_mysql, "recover_weight_dimensions_record_executions",
                        lambda: events.append("recover"))
    monkeypatch.setattr(web, "start_interface_background_services", lambda: events.append("background"))
    monkeypatch.setattr(web, "start_background_exports", lambda: None)
    monkeypatch.setattr(web, "start_yandex_console_bootstrap", lambda: None)
    monkeypatch.setattr(web.weight_dimensions_records, "shutdown_executions",
                        lambda **kwargs: events.append(("shutdown", kwargs["timeout"])))
    monkeypatch.setattr(web, "close_all_pools", lambda: events.append("close-pools"))
    return events, locks


def test_sigterm_drains_weight_updates_before_releasing_database_and_owner(monkeypatch):
    events, locks = service(monkeypatch)
    previous = object()
    handlers = []
    monkeypatch.setattr(web.signal, "getsignal", lambda sig: previous)
    monkeypatch.setattr(web.signal, "signal", lambda sig, handler: handlers.append(handler))

    def serve():
        assert all(lock.acquire.called for lock in locks)
        assert events == ["recover", "background"]
        handlers[0](web.signal.SIGTERM, None)

    monkeypatch.setattr(web, "serve_wsgi_application", serve)
    with pytest.raises(SystemExit) as stopped:
        web.run_interface_server()
    assert stopped.value.code == 0
    assert events[2:] == [
        ("shutdown", 15), "close-pools",
        ("release", "bit_interface_worker_singleton"),
        ("release", "workbench_scheduler_owner"),
    ]
    assert handlers[-1] is previous


@pytest.mark.parametrize("mode,disabled", [("web", False), ("combined", True)])
def test_web_and_standby_do_not_recover_another_workers_records(monkeypatch, mode, disabled):
    events, _ = service(monkeypatch, mode, disabled)
    monkeypatch.setattr(web, "serve_wsgi_application", lambda: None)
    assert web.run_interface_server()
    assert "recover" not in events
    assert events.index(("shutdown", 15)) < events.index("close-pools")
