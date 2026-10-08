from types import SimpleNamespace

from scripts import watch_windows_agent as watch


def test_only_agent_source_and_resources_trigger_builds(tmp_path):
    for path in ["local_agent.py", "local_agent_worker.py", "MercadoLocalAgent.spec",
                 "requirements-agent-windows.txt", "bit/bit_daily_task.py",
                 "erp/ai_weight_price/service.py", "browser_extension/zeshun_collector/background.js"]:
        assert watch.relevant_path(tmp_path / path, tmp_path)
    for path in [".tools/windows-agent/prefix/core.py", "dist/MercadoLocalAgent.exe",
                 "bit/runtime_locks/worker.py", "bit/orders.xlsx", ".data/tasks.sqlite3",
                 "bit/__pycache__/module.py", "tests/test_local_agent.py", "docs/local_agent.md"]:
        assert not watch.relevant_path(tmp_path / path, tmp_path)


def test_runtime_events_do_not_delay_pending_source_build(tmp_path):
    watcher = watch.AgentBuildWatcher(tmp_path)
    watcher.on_event(SimpleNamespace(is_directory=False, event_type="modified", src_path=str(tmp_path / "local_agent.py")))
    changed = watcher.last_change
    watcher.on_event(SimpleNamespace(is_directory=False, event_type="modified", src_path=str(tmp_path / ".tools/build.log")))
    assert watcher.last_change == changed
    assert watcher.pending == {"local_agent.py"}


def test_source_rename_triggers_build(tmp_path):
    watcher = watch.AgentBuildWatcher(tmp_path)
    watcher.on_event(SimpleNamespace(is_directory=False, event_type="moved", src_path=str(tmp_path / "editor.tmp"),
                                    dest_path=str(tmp_path / "local_agent.py")))
    assert watcher.pending == {"local_agent.py"}
