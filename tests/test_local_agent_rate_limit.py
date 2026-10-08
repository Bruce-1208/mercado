import json
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import local_agent
from local_agent import (
    AgentAuthorizationError, AgentRateLimitError,
    AgentServiceUnavailableError, LocalAgent,
)


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
    agent.shutdown_event.wait = lambda seconds: (sleep(seconds), False)[1]
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


@pytest.mark.parametrize("status,error_type", [
    (401, AgentAuthorizationError), (403, AgentAuthorizationError),
    (500, AgentServiceUnavailableError), (502, AgentServiceUnavailableError),
    (503, AgentServiceUnavailableError), (504, AgentServiceUnavailableError),
])
def test_auth_rejection_is_distinct_from_restart_outage(runtime, status, error_type):
    agent, _clock = runtime
    agent.session.request.return_value = response(status)
    with pytest.raises(error_type):
        agent.heartbeat(current_job_id="active-job")


def test_rejected_result_upload_keeps_agent_responsive(runtime):
    agent, clock = runtime
    agent.session.request.return_value = response(403)

    assert agent.send_event_until_success("active-job", status="stopped") is None
    assert agent.session.request.call_count == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("failure", [
    requests.ConnectionError("offline"), requests.Timeout("restart timeout"),
    AgentServiceUnavailableError("HTTP 503"), AgentRateLimitError("HTTP 429", 60),
])
@pytest.mark.parametrize("stop_after_reconnect", [False, True])
def test_worker_survives_transient_control_outage_and_obeys_reconnected_stop(
    runtime, monkeypatch, failure, stop_after_reconnect
):
    agent, clock = runtime
    agent.current_release = "outage-test"
    terminated = []
    events = []
    output = iter(["first progress\n", "second progress\n", None])

    class WorkerOutput:
        def get(self, timeout):
            clock.now += 900
            return next(output)

    process = SimpleNamespace(
        pid=123,
        poll=lambda: -9 if terminated else 0 if clock.now >= 3700 else None,
        wait=lambda **_kwargs: -9 if terminated else 0,
    )
    guard = SimpleNamespace(process=process, close=lambda: None,
                            terminate=lambda: terminated.append(True))
    monkeypatch.setattr(local_agent.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(local_agent, "WorkerProcessGuard", lambda _process: guard)
    monkeypatch.setattr(local_agent, "terminate_worker_tree", lambda _pid: None)
    monkeypatch.setattr(local_agent.threading, "Thread", lambda **_kwargs: Mock())
    monkeypatch.setattr(local_agent.queue, "Queue", WorkerOutput)
    monkeypatch.setattr(agent, "send_event", lambda _job_id, **data: events.append(data))

    calls = []
    alive_at_reconnect = []
    def heartbeat(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise failure
        if len(calls) == 2:
            alive_at_reconnect.append(not terminated)
        return {"cancel_job_ids": ["outage-job"] if stop_after_reconnect else []}

    monkeypatch.setattr(agent, "heartbeat", heartbeat)
    agent.run_job({"job_id": "outage-job", "job_type": "daily_task", "payload": {}})

    assert len(calls) >= 2
    assert alive_at_reconnect == [True], "A restart outage must not cancel the authorized worker"
    assert all(call["current_job_id"] == "outage-job" for call in calls)
    assert bool(terminated) == stop_after_reconnect
    assert events[-1]["status"] == ("stopped" if stop_after_reconnect else "success")


def test_auth_rejection_stops_active_worker_without_waiting_for_lease(runtime, monkeypatch):
    agent, clock = runtime
    agent.current_release = "auth-test"
    terminated = []
    events = []
    output = iter(["running\n", None])

    class WorkerOutput:
        def get(self, timeout):
            clock.now += 1
            return next(output)

    process = SimpleNamespace(pid=123, poll=lambda: -9 if terminated else None,
                              wait=lambda **_kwargs: -9)
    guard = SimpleNamespace(process=process, close=lambda: None,
                            terminate=lambda: terminated.append(True))
    monkeypatch.setattr(local_agent.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(local_agent, "WorkerProcessGuard", lambda _process: guard)
    monkeypatch.setattr(local_agent, "terminate_worker_tree", lambda _pid: None)
    monkeypatch.setattr(local_agent.threading, "Thread", lambda **_kwargs: Mock())
    monkeypatch.setattr(local_agent.queue, "Queue", WorkerOutput)
    monkeypatch.setattr(agent, "send_event", lambda _job_id, **data: events.append(data))
    monkeypatch.setattr(agent, "heartbeat", Mock(side_effect=AgentAuthorizationError("HTTP 403")))

    agent.run_job({"job_id": "rejected-job", "job_type": "daily_task", "payload": {}})

    assert terminated
    assert clock.now - 1000 < local_agent.JOB_CONTROL_LOSS_STOP_SECONDS
    assert events[-1]["status"] == "stopped"


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


def test_run_uses_job_claimed_by_heartbeat(runtime, monkeypatch):
    agent, _clock = runtime
    agent.config.once = True
    job = {"job_id": "atomic-job", "job_type": "daily_task", "payload": {}}
    monkeypatch.setattr(agent, "ensure_enrolled", Mock())
    monkeypatch.setattr(
        agent,
        "heartbeat",
        Mock(return_value={"bundle": {}, "job": job, "queue_id": "queue-one"}),
    )
    monkeypatch.setattr(agent, "ensure_release", Mock())
    monkeypatch.setattr(
        agent,
        "claim_job",
        Mock(side_effect=AssertionError("new server must not need a second claim request")),
    )
    executed = Mock()
    monkeypatch.setattr(agent, "run_job", executed)

    assert agent.run() == 0
    executed.assert_called_once_with(job)


def test_run_falls_back_to_claim_endpoint_for_old_server(runtime, monkeypatch):
    agent, _clock = runtime
    agent.config.once = True
    job = {"job_id": "legacy-job", "job_type": "appeal", "payload": {}}
    monkeypatch.setattr(agent, "ensure_enrolled", Mock())
    monkeypatch.setattr(agent, "heartbeat", Mock(return_value={"bundle": {}}))
    monkeypatch.setattr(agent, "ensure_release", Mock())
    claim = Mock(return_value=job)
    monkeypatch.setattr(agent, "claim_job", claim)
    executed = Mock()
    monkeypatch.setattr(agent, "run_job", executed)

    assert agent.run() == 0
    claim.assert_called_once_with()
    executed.assert_called_once_with(job)


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
