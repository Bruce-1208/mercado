import json
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import local_agent
from local_agent import AgentRateLimitError, LocalAgent


def response(status=200, payload=None, *, text=None, retry_after=None):
    result = requests.Response()
    result.status_code = status
    result._content = (
        text if text is not None else json.dumps(payload or {"status": "success", "data": {}})
    ).encode("utf-8")
    if retry_after is not None:
        result.headers["Retry-After"] = retry_after
    return result


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1000.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(local_agent.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(local_agent.time, "time", lambda: clock.now)
    monkeypatch.setattr(local_agent.time, "sleep", sleep)
    monkeypatch.setattr(local_agent.random, "uniform", lambda *_args: 0.0)
    config = SimpleNamespace(
        data_dir=tmp_path, server_url="https://workbench.example",
        agent_token="test-only", enrollment_token="enrollment-only",
        agent_id="agent-test", name="test", once=False, poll_seconds=10,
        db_api_token="test-only", heartbeat_seconds=10,
    )

    def save_token(token):
        config.agent_token = token

    config.save_agent_token = Mock(side_effect=save_token)
    agent = LocalAgent(config)
    agent.session = SimpleNamespace(request=Mock(), get=Mock(), headers={})
    return agent, clock


@pytest.mark.parametrize("status,body,retry_after,expected", [
    (429, "<html>Too Many Requests</html>", None, 60),
    (429, "[]", "180", 180),
    (429, "null", "0", 60),
    (429, "{}", "NaN", 60),
    (429, "{}", "inf", 60),
    (429, "{}", "invalid", 60),
    (429, "{}", "-10", 60),
    (429, "{}", formatdate(1420, usegmt=True), 420),
    (429, "{}", formatdate(900, usegmt=True), 60),
    (502, "<html>Connections Exceed</html>", None, 60),
    (503, "Connections Exceed", "90", 90),
])
def test_limit_response_sets_shared_cooldown(runtime, status, body, retry_after, expected):
    agent, clock = runtime
    agent.session.request.return_value = response(status, text=body, retry_after=retry_after)
    with pytest.raises(AgentRateLimitError, match=f"HTTP {status}") as caught:
        agent.heartbeat()
    assert caught.value.retry_after == expected

    # All network paths must reject locally while the shared cooldown is active.
    agent.config.agent_token = ""
    for operation in (
        agent.heartbeat, agent.claim_job, agent.ensure_enrolled,
        lambda: agent.send_event("job-1", content="keep this log"),
        lambda: agent.ensure_release({"version": "v1", "sha256": "abc"}),
    ):
        with pytest.raises(AgentRateLimitError):
            operation()
    assert agent.session.request.call_count == 1
    agent.session.get.assert_not_called()
    assert clock.sleeps == []

    clock.now += expected
    agent.session.request.return_value = response()
    assert agent.heartbeat() == {}
    assert agent.session.request.call_count == 2


def test_bundle_limit_also_pauses_heartbeat(runtime):
    agent, _clock = runtime
    agent.session.get.return_value = response(429, retry_after="120")
    with pytest.raises(AgentRateLimitError):
        agent.ensure_release({"version": "v1", "sha256": "abc"})
    with pytest.raises(AgentRateLimitError):
        agent.heartbeat()
    agent.session.request.assert_not_called()


@pytest.mark.parametrize("status,body", [
    (401, "<html>Unauthorized</html>"),
    (502, "Bad Gateway"),
    (503, "Service Unavailable"),
    (500, "[]"),
])
def test_other_http_errors_are_not_mislabeled_as_limits(runtime, status, body):
    agent, _clock = runtime
    agent.session.request.return_value = response(status, text=body)
    with pytest.raises(RuntimeError, match=f"HTTP {status}") as caught:
        agent.heartbeat()
    assert not isinstance(caught.value, AgentRateLimitError)
    assert agent._cooldown_remaining() == 0


def test_polling_backoff_survives_successful_heartbeat_and_resets_after_recovery(runtime, monkeypatch):
    agent, clock = runtime
    monkeypatch.setattr(agent, "ensure_release", lambda _bundle: None)
    agent.session.request.side_effect = [
        response(), response(429),
        response(), response(429),
        response(), response(429),
        response(), response(429),
        response(), response(),  # Successful heartbeat and claim complete a poll.
        response(429),
        KeyboardInterrupt(),
    ]

    assert agent.run() == 0
    assert clock.sleeps == [60, 120, 240, 300, 10, 60]


def test_registration_is_retried_then_saves_credential(runtime, monkeypatch):
    agent, clock = runtime
    agent.config.agent_token = ""
    monkeypatch.setattr(agent, "ensure_release", lambda _bundle: None)
    agent.session.request.side_effect = [
        response(429, text="<html>Too Many Requests</html>"),
        response(payload={"status": "success", "data": {"agent_token": "new-test-token"}}),
        response(), response(), KeyboardInterrupt(),
    ]

    assert agent.run() == 0
    assert clock.sleeps == [60, 10]
    agent.config.save_agent_token.assert_called_once_with("new-test-token")
    assert agent.session.headers["X-Local-Agent-Token"] == "new-test-token"
    assert agent.session.request.call_args_list[0].args[1].endswith("/enroll")
    assert agent.session.request.call_args_list[1].args[1].endswith("/enroll")


def test_final_upload_waits_for_existing_cooldown_without_losing_payload(runtime):
    agent, clock = runtime
    agent.session.request.return_value = response(429, retry_after="180")
    with pytest.raises(AgentRateLimitError):
        agent.heartbeat()
    agent.session.request.reset_mock()
    agent.session.request.side_effect = [response(429), response()]

    agent.send_event_until_success("job-1", content="preserved log", status="success")

    assert clock.sleeps == [180, 120]
    assert agent.session.request.call_count == 2
    payloads = [call.kwargs["json"] for call in agent.session.request.call_args_list]
    assert payloads == [
        {"agent_id": "agent-test", "content": "preserved log", "status": "success"},
    ] * 2


def test_normal_connection_errors_never_speed_up_polling(runtime, monkeypatch):
    agent, clock = runtime
    monkeypatch.setattr(agent, "heartbeat", Mock(side_effect=[
        requests.ConnectionError("offline"), requests.ConnectionError("offline"),
        requests.ConnectionError("offline"), KeyboardInterrupt(),
    ]))
    assert agent.run() == 0
    assert clock.sleeps == [10, 20, 40]


def test_once_mode_reports_limit_without_retrying(runtime):
    agent, clock = runtime
    agent.config.once = True
    agent.session.request.return_value = response(429)
    assert agent.run() == 1
    assert clock.sleeps == []
    assert agent.session.request.call_count == 1


def test_running_worker_keeps_draining_logs_during_cooldown(runtime, monkeypatch):
    agent, clock = runtime
    agent.current_release = "test-release"
    lines = ["x" * 40000, "buffered progress\n", "final progress\n"]
    output = iter([*lines, None])

    class WorkerOutput:
        def get(self, timeout):
            clock.now += 1
            return next(output)

    process = SimpleNamespace(poll=lambda: 0 if clock.now >= 1004 else None, wait=lambda: 0)
    monkeypatch.setattr(local_agent.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(local_agent.threading, "Thread", lambda **_kwargs: Mock())
    monkeypatch.setattr(local_agent.queue, "Queue", WorkerOutput)
    sleep = local_agent.time.sleep

    def sleep_after_exit(seconds):
        assert process.poll() == 0, "Server cooldown must not block monitoring the worker"
        sleep(seconds)

    monkeypatch.setattr(local_agent.time, "sleep", sleep_after_exit)
    agent.session.request.side_effect = [response(429), response(), response()]

    agent.run_job({"job_id": "job-1", "job_type": "daily_task", "payload": {}})

    assert clock.sleeps == [57]
    calls = agent.session.request.call_args_list
    assert len(calls) == 3  # The heartbeat was suppressed locally during cooldown.
    assert calls[1].kwargs["json"]["content"] == "".join(lines)
    assert calls[2].kwargs["json"]["status"] == "success"
