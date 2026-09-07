import ast
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest

from bit import bit_appeal_ai as ai, bit_daily_task as daily, chat_log
from bit.bit_ai_chat_protocol import ChatMessages, new_messages, read_snapshot
from bit.bit_appeal_state import AppealExecutionError, result_from_logs, task_execution_counts


@pytest.fixture(autouse=True)
def isolate_storage(monkeypatch, tmp_path):
    from bit import bit_mysql
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kwargs: pytest.fail("test attempted real DB access"))
    monkeypatch.setattr(ai, "write_local_record", lambda *a, **k: None)
    monkeypatch.setattr(ai, "insert_ai_appeal_record", lambda *a, **k: None)
    monkeypatch.setattr(chat_log, "CHAT_DB_ENABLED", False)
    monkeypatch.setenv("MERCADO_AI_CHAT_LOG", str(tmp_path / "chat.jsonl"))


def snapshot(*messages, epoch="one", busy=False):
    return {"epoch": epoch, "conversation_id": "case-1", "busy": busy,
            "messages": [{"role": role, "id": key, "text": text} for role, key, text in messages]}


def test_duplicate_text_with_new_message_id_survives_virtual_list_truncation():
    before = snapshot(("assistant", "old", "请确认站点"))
    after = snapshot(("assistant", "new", "请确认站点"))
    assert new_messages(before, after, "assistant")[0]["id"] == "new"
    assert new_messages(before, before, "assistant") == []


def test_changed_conversation_is_unknown_not_a_reply():
    with pytest.raises(AppealExecutionError) as exc:
        new_messages(snapshot(), snapshot(epoch="another"), "assistant")
    assert exc.value.status == "sent_unknown"


def test_reply_waits_for_streaming_to_finish(monkeypatch):
    now = [0.0]
    states = iter([
        snapshot(("assistant", "a", "正在"), busy=True),
        snapshot(("assistant", "a", "已完成核查")),
        snapshot(("assistant", "a", "已完成核查")),
        snapshot(("assistant", "a", "已完成核查")),
    ])
    monkeypatch.setattr(ai.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(ai, "_appeal_pause", lambda driver, seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(ai, "safe_get_agent_messages", lambda driver: ChatMessages(next(states)))
    response, _ = ai.wait_for_ai_agent_reply(SimpleNamespace(), ChatMessages(snapshot()), timeout=8, poll_interval=1)
    assert response == "已完成核查"
    assert now[0] == 3


def group_setup(monkeypatch, replies):
    sent, records = [], []
    reply_iter = iter(replies)
    monkeypatch.setattr(ai, "safe_get_agent_messages", lambda driver: [])
    monkeypatch.setattr(ai, "send_ai_chat_message", lambda driver, text: sent.append(text) or {"acknowledged": True})
    monkeypatch.setattr(ai, "wait_for_ai_agent_reply", lambda *a, **k: next(reply_iter))
    monkeypatch.setattr(ai, "append_chat_log", lambda name, site, event, **kw: records.append({"event": event, **kw}))
    return sent, records


def test_only_site_question_gets_local_reply(monkeypatch):
    sent, records = group_setup(monkeypatch, [
        ("Which country? Brazil, Chile, Argentina", ["Which country? Brazil, Chile, Argentina"]),
        ("已完成核查", ["已完成核查"]),
    ])
    result = ai.send_infraction_message_with_retry(SimpleNamespace(), "MLB123456 请复核", "MLB123456", "店", "巴西", 1, 1)
    assert sent == ["MLB123456 请复核", "Brazil"]
    assert result["status"] == "sent"
    assert result["reply_status"] == "replied"
    assert result_from_logs(records)["metrics"]["replied"] == 1


def test_timeout_is_preserved_and_forces_new_conversation(monkeypatch):
    sent, records = group_setup(monkeypatch, [("", [])])
    driver = SimpleNamespace()
    result = ai.send_infraction_message_with_retry(driver, "请复核", "MLB123456", "店", "巴西", 1, 2)
    assert len(sent) == 1
    assert result["status"] == "sent"
    assert result["reply_status"] == "reply_timeout"
    assert result["sent"] and result["acknowledged"]
    assert driver._bit_ai_reset_before_group
    assert not daily._is_retryable_site_result(result)
    assert not daily._is_failed_appeal_result(result)
    assert result_from_logs(records)["status"] == "sent"
    assert result_from_logs(records)["metrics"]["reply_timeout"] == 1


def test_ambiguous_submit_is_never_retried_in_same_group(monkeypatch):
    sent, records = group_setup(monkeypatch, [])
    def uncertain(driver, text):
        sent.append(text)
        raise AppealExecutionError("response lost", "sent_unknown", sent=True)
    monkeypatch.setattr(ai, "send_ai_chat_message", uncertain)
    result = ai.send_infraction_message_with_retry(SimpleNamespace(), "请复核", "MLB123456", "店", "巴西", 1, 1)
    assert sent == ["请复核"]
    assert result["status"] == "sent_unknown"
    assert not daily._is_retryable_site_result(result)


class Input:
    tag_name = "textarea"
    def __init__(self):
        self.enters = 0
    def send_keys(self, value):
        self.enters += 1


class Driver:
    def __init__(self):
        self.box = Input()
        self.cleared = False
    def execute_script(self, script, *args):
        if script.startswith("return arguments[0].value"):
            return "" if self.cleared else "请复核"
        return None


def sender_setup(monkeypatch):
    driver = Driver()
    monkeypatch.setattr(ai, "activate_ai_chat_context", lambda *a, **k: ai.AI_CHAT_MODE_IFRAME)
    monkeypatch.setattr(ai, "recover_expired_ai_conversation", lambda *a, **k: False)
    monkeypatch.setattr(ai, "find_chat_input", lambda *a, **k: driver.box)
    monkeypatch.setattr(ai, "read_snapshot", lambda d: snapshot())
    return driver


def test_click_error_does_not_fall_back_to_enter(monkeypatch):
    driver = sender_setup(monkeypatch)
    monkeypatch.setattr(ai, "click_send_button", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection lost")))
    with pytest.raises(AppealExecutionError) as exc:
        ai.send_ai_chat_message(driver, "请复核")
    assert exc.value.status == "sent_unknown"
    assert driver.box.enters == 0


def test_send_confirmation_requires_new_echo_and_empty_input(monkeypatch):
    driver = sender_setup(monkeypatch)
    states = iter([snapshot(("user", "old", "请复核")), snapshot(("user", "new", "请复核"))])
    monkeypatch.setattr(ai, "read_snapshot", lambda d: next(states))
    monkeypatch.setattr(ai, "click_send_button", lambda *a, **k: setattr(driver, "cleared", True) or True)
    result = ai.send_ai_chat_message(driver, "请复核")
    assert result["acknowledged"] is True
    assert result["message_id"] == "new"


def test_periodic_restart_and_timeout_restart(monkeypatch):
    calls = []
    monkeypatch.setattr(ai, "restart_ai_conversation", lambda *a: calls.append(True) or True)
    driver = SimpleNamespace()
    ai._prepare_group_conversation(driver, "店", "巴西", 1)
    ai._prepare_group_conversation(driver, "店", "巴西", 4)
    driver._bit_ai_reset_before_group = True
    ai._prepare_group_conversation(driver, "店", "巴西", 5)
    assert calls == [True, True]


def test_mixed_explicit_site_evidence_fails_closed():
    assert not ai._site_state_matches({"currentShort": "MX", "selectedRemote": "MLB-remote"}, "墨西哥")


def test_logging_failure_does_not_break_collection(monkeypatch):
    monkeypatch.setattr(chat_log, "_rotate_log_if_needed", lambda p: (_ for _ in ()).throw(OSError("sharing violation")))
    chat_log.start_appeal_log_collection()
    try:
        path = chat_log.append_chat_log("店", "BR", "sent", response="reply")
        record = chat_log.get_appeal_log_records()[0]
        assert record["event_id"]
        assert len(list(path.parent.glob("*_recovery_*.jsonl"))) == 1
    finally:
        chat_log.stop_appeal_log_collection()


def _log_worker(path, worker):
    import os
    from bit import chat_log as log
    os.environ["MERCADO_AI_CHAT_LOG"] = path
    log.CHAT_DB_ENABLED = False
    log.MAX_CHAT_LOG_BYTES = 800
    for i in range(15):
        log.append_chat_log(str(worker), "BR", "test", message=f"{worker}:{i}")


def test_real_multiprocess_rotation_keeps_every_event(tmp_path):
    path = tmp_path / "parallel.jsonl"
    ctx = multiprocessing.get_context("spawn")
    workers = [ctx.Process(target=_log_worker, args=(str(path), i)) for i in range(3)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(15)
            assert worker.exitcode == 0
        records = [json.loads(line) for file in tmp_path.glob("*.jsonl")
                   for line in file.read_text(encoding="utf-8").splitlines()]
        assert len(records) == 45
        assert len({r["event_id"] for r in records}) == 45
        assert len({r["message"] for r in records}) == 45
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)


def test_cleanup_still_releases_lease_when_phrase_selection_fails(monkeypatch):
    monkeypatch.setattr(ai, "select_appeal_phrase", lambda form: (_ for _ in ()).throw(ValueError("没有启用话术")))
    result = ai.shensu("店", "BR", "侵权", "")
    assert result["status"] == "failed"
    assert chat_log.get_appeal_log_records() == []


def test_task_counts_do_not_count_nested_groups_twice():
    result = {"results": [{"result": {"execution_status": "reply_timeout", "groups": [{"status": "reply_timeout"}]}}]}
    assert task_execution_counts(result) == {"reply_timeout": 1}


def test_appeal_entrypoints_have_no_external_model_import_or_call():
    from bit import bit_appeal
    for module in (ai, bit_appeal):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        assert not any(isinstance(n, ast.ImportFrom) and n.module == "AI_Agent.deepseek" for n in ast.walk(tree))
        assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "chat_deepseek" for n in ast.walk(tree))


@pytest.mark.parametrize("reply", [
    "Your Brazil site appeal was rejected.",
    "确认 Brazil 商品不符合要求，无法删除。",
    "It's helpful to review the country restrictions for Brazil.",
])
def test_country_mentions_are_not_site_questions(reply):
    assert not ai.is_site_option_question(reply)


def test_driver_cleanup_failure_preserves_outcome_and_releases_lease(monkeypatch):
    calls = []
    lease = SimpleNamespace(acquire=lambda **kw: True, release=lambda: calls.append("release"))
    driver = SimpleNamespace(service=SimpleNamespace(
        stop=lambda: (_ for _ in ()).throw(RuntimeError("service already gone"))))
    monkeypatch.setattr(
        ai,
        "get_window_id_by_shop_name",
        lambda _name: pytest.fail("已有 window_id 时不应重新读取窗口列表"),
    )
    monkeypatch.setattr(ai, "current_thread_window_lease", lambda w: None)
    monkeypatch.setattr(ai, "create_window_lease", lambda *a, **kw: lease)
    monkeypatch.setattr(ai, "connect_bit_browser", lambda w: (driver, {}))
    monkeypatch.setattr(ai, "open_help_page_with_daily_validation", lambda *a, **kw: None)
    monkeypatch.setattr(ai, "select_site", lambda *a: None)
    monkeypatch.setattr(ai, "handle_infraction", lambda *a, **kw: None)
    monkeypatch.setattr(ai, "close_current_tab_keep_browser", lambda *a: calls.append("close"))
    result = ai.shensu("店", "BR", "侵权", "请复核", window_id="window")
    assert result["status"] == "no_data"
    assert calls == ["close", "release"]


def test_stop_during_reply_keeps_confirmed_send_success(monkeypatch):
    sent, records = group_setup(monkeypatch, [])
    def stop(*args, **kwargs):
        raise AppealExecutionError("已停止", "stopped")
    monkeypatch.setattr(ai, "wait_for_ai_agent_reply", stop)
    result = ai.send_infraction_message_with_retry(
        SimpleNamespace(), "请复核", "MLB123456", "店", "巴西", 1, 1,
    )
    assert result["status"] == "sent"
    assert result["post_send_status"] == "stopped"
    assert result_from_logs(records)["status"] == "sent"
    assert result_from_logs(records)["sent"]


def test_complete_role_aware_chat_is_not_truncated(monkeypatch):
    messages = [
        {"role": "user" if index % 2 else "assistant", "id": str(index), "text": "x" * 2500}
        for index in range(25)
    ]
    transcript = ChatMessages({
        "epoch": "epoch-1",
        "conversation_id": "case-1",
        "busy": False,
        "messages": messages,
    })
    chat_log.start_appeal_log_collection()
    try:
        chat_log.append_chat_log("店", "BR", "sent", chat=transcript)
        record = chat_log.get_appeal_log_records()[0]
    finally:
        chat_log.stop_appeal_log_collection()

    assert len(record["chat"]["messages"]) == 25
    assert record["chat"]["messages"][0]["role"] == "assistant"
    assert len(record["chat"]["messages"][0]["text"]) == 2500
    history = ai._collect_full_chat_history([record])
    assert len(history) == 1
    assert len(history[0]["messages"]) == 25


def test_human_chat_hands_off_without_generated_followups(monkeypatch):
    from bit import bit_appeal as human
    records = []
    monkeypatch.setattr(human, "get_human_chat_context", lambda d: "当前客服回复")
    monkeypatch.setattr(human, "insert_chat_info_by_api", lambda *a: records.append(a))
    driver = SimpleNamespace()
    human.chat_ai(driver, "店", "BR", "侵权", "请复核", "Bruce")
    assert driver._bit_human_needs_manual
    assert records[0][4] == ""


class FakeCursor:
    lastrowid = 42
    def __init__(self):
        self.calls = []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def execute(self, sql, params=None):
        if params is not None:
            assert sql.count("%s") == len(params)
        self.calls.append((sql, params))
    def fetchone(self):
        return {"acquired": 1}


class FakeConnection:
    def __init__(self):
        self.cur = FakeCursor()
        self.commits = self.rollbacks = self.closes = 0
    def cursor(self):
        return self.cur
    def commit(self):
        self.commits += 1
    def rollback(self):
        self.rollbacks += 1
    def close(self):
        self.closes += 1


@pytest.mark.parametrize("function", ["insert_appeal_chat_record", "insert_ai_appeal_record"])
def test_initialized_log_insert_has_no_ddl_and_uses_event_id(monkeypatch, function):
    from bit import bit_mysql as db
    connection = FakeConnection()
    monkeypatch.setattr(db, "_APPEAL_SCHEMA_READY", True)
    monkeypatch.setattr(db, "_appeal_connection", lambda: connection)
    event_id = "a" * 32
    assert getattr(db, function)({"event_id": event_id}) == 42
    sql, params = connection.cur.calls[0]
    assert len(connection.cur.calls) == 1
    assert "ON DUPLICATE KEY" in sql
    assert params[-1] == event_id
    assert connection.commits == connection.closes == 1


def test_ai_appeal_summary_persists_complete_chat_json(monkeypatch):
    from bit import bit_mysql as db
    connection = FakeConnection()
    monkeypatch.setattr(db, "_APPEAL_SCHEMA_READY", True)
    monkeypatch.setattr(db, "_appeal_connection", lambda: connection)
    chat_history = [{
        "conversation_id": "case-1",
        "epoch": "epoch-1",
        "messages": [
            {"id": "u1", "role": "user", "text": "请复核"},
            {"id": "a1", "role": "assistant", "text": "已收到"},
        ],
    }]

    db.insert_ai_appeal_record({"event_id": "b" * 32, "chat_history": chat_history})

    _, params = connection.cur.calls[0]
    assert json.loads(params[10]) == chat_history


def test_schema_failure_is_not_cached_and_releases_server_lock(monkeypatch):
    from bit import bit_mysql as db
    connection = FakeConnection()
    monkeypatch.setattr(db, "_APPEAL_SCHEMA_READY", False)
    monkeypatch.setattr(db, "_appeal_connection", lambda: connection)
    monkeypatch.setattr(db, "_ensure_appeal_phrases_table", lambda c: (_ for _ in ()).throw(RuntimeError("DDL unavailable")))
    with pytest.raises(RuntimeError, match="DDL unavailable"):
        db.initialize_appeal_storage()
    assert not db._APPEAL_SCHEMA_READY
    assert any("RELEASE_LOCK" in sql for sql, _ in connection.cur.calls)
    assert connection.rollbacks == connection.closes == 1


def test_replay_preview_and_event_dedup_are_isolated(monkeypatch, tmp_path):
    from bit import bit_db_api, replay_appeal_logs
    calls = []
    monkeypatch.setattr(bit_db_api, "insert_appeal_chat_record", lambda record: calls.append(("chat", record)))
    monkeypatch.setattr(bit_db_api, "insert_ai_appeal_record", lambda record: calls.append(("summary", record)))
    chat_log.append_chat_log("店", "BR", "sent")
    path = tmp_path / "chat.jsonl"
    summary = {"event_id": "a" * 32}
    chat_log.write_local_record({"event": "appeal_record", "event_id": summary["event_id"], "record": summary}, path)
    preview = replay_appeal_logs.replay([path, path])
    assert preview["events"] == 2 and preview["written"] == 0 and calls == []
    applied = replay_appeal_logs.replay([path, path], apply=True)
    assert applied["written"] == 2
    assert [kind for kind, _ in calls] == ["chat", "summary"]


def test_real_dom_snapshot_handles_shadow_root_and_iframe():
    playwright = pytest.importorskip("playwright.sync_api")
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not edge.exists():
        pytest.skip("isolated DOM test needs local Edge")
    class Adapter:
        def __init__(self, frame):
            self.frame = frame
        def execute_script(self, script):
            return self.frame.evaluate("script => new Function(script)()", script)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(edge), headless=True)
        try:
            context = browser.new_context()
            context.route("**/*", lambda route: route.abort())
            page = context.new_page()
            page.set_content('<div data-role="assistant">外部旧回复</div><div id="sa-assistant-chat"></div>')
            page.evaluate('''() => {
                document.querySelector('#sa-assistant-chat').attachShadow({mode:'open'}).innerHTML =
                  '<div data-message-id="u1" data-role="user">请复核</div>' +
                  '<div data-message-id="a1" data-role="agent">收到</div>';
            }''')
            adapter = Adapter(page)
            before = read_snapshot(adapter)
            assert [(m["role"], m["text"]) for m in before["messages"]] == [("user", "请复核"), ("assistant", "收到")]
            page.evaluate('''() => {
                const root = document.querySelector('#sa-assistant-chat').shadowRoot;
                root.innerHTML = '<div data-message-id="a2" data-role="agent">收到</div>';
            }''')
            after = read_snapshot(adapter)
            assert new_messages(before, after, "assistant")[0]["id"] == "a2"
            page.set_content('<iframe srcdoc="<div class=message-item--assistant>iframe 回复</div>"></iframe>')
            frame = page.frames[1]
            frame.wait_for_selector(".message-item--assistant")
            assert ChatMessages(read_snapshot(Adapter(frame))) == ["iframe 回复"]
        finally:
            browser.close()


def test_stop_after_click_preserves_possible_submission(monkeypatch):
    import threading
    driver = sender_setup(monkeypatch)
    driver._bit_appeal_stop_event = threading.Event()
    monkeypatch.setattr(ai, "click_send_button", lambda *a, **kw: driver._bit_appeal_stop_event.set() or True)
    with pytest.raises(AppealExecutionError) as exc:
        ai.send_ai_chat_message(driver, "请复核")
    assert exc.value.status == "stopped" and exc.value.sent


def test_api_failure_is_not_mistaken_for_no_data(monkeypatch):
    monkeypatch.setattr(ai, "_find_infraction_api_target", lambda *a: {"site_ids": ["MLB"]})
    monkeypatch.setattr(ai.mercado_infraction_sync, "collect_live_detection_infractions",
                        lambda *a, **kw: {"data": [], "failed_stores": [{"message": "timeout"}]})
    with pytest.raises(RuntimeError, match="API获取侵权订单信息失败"):
        ai.get_infraction_orders("window", "店", "BR")


def test_expired_site_budget_prevents_opening_chat(monkeypatch):
    driver = SimpleNamespace(_bit_appeal_deadline=0)
    monkeypatch.setattr(ai, "open_mercado_backend_page", lambda *a, **kw: pytest.fail("must not open chat"))
    with pytest.raises(AppealExecutionError) as exc:
        ai.open_ai_contact_window(driver, "店", "BR")
    assert exc.value.status == "deadline_exceeded"
