"""Versioned business worker downloaded and launched by ``local_agent.py``."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path


def _watch_cancel(path, event):
    while not event.is_set():
        if path.exists():
            event.set()
            return
        time.sleep(0.5)


def _write_log(text):
    text = str(text or "").replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    if text:
        print(text, end="" if text.endswith("\n") else "\n", flush=True)


def configure_execution_context(job):
    """把 Agent 身份传给业务模块及其后续创建的子进程。"""
    job = dict(job or {})
    payload = dict(job.get("payload") or {})
    agent_id = str(job.get("agent_id") or payload.get("agent_id") or "").strip()
    agent_name = str(payload.get("agent_name") or "").strip()
    hostname = str(socket.gethostname() or "未知主机").strip()
    os.environ["BIT_EXECUTION_TARGET"] = "agent"
    os.environ["BIT_EXECUTION_AGENT_ID"] = agent_id
    os.environ["BIT_EXECUTION_AGENT_NAME"] = agent_name
    os.environ["BIT_EXECUTION_HOSTNAME"] = hostname
    return {
        "target": "agent",
        "agent_id": agent_id,
        "agent_name": agent_name,
        "hostname": hostname,
    }


def run_appeal(payload, stop_event):
    from bit import bit_interface

    name = str(payload.get("name") or "").strip()
    sites = [str(value).strip() for value in payload.get("sites") or () if str(value).strip()]
    forms = [str(value).strip() for value in payload.get("forms") or () if str(value).strip()]
    if not name or not sites or not forms:
        raise ValueError("申诉任务缺少店铺、站点或任务类型")
    loop_count = bit_interface.normalize_appeal_loop_count(payload.get("loop_count"))
    _write_log(f"本机 Agent 开始执行申诉：{name} / {'、'.join(sites)} / {'、'.join(forms)}\n")
    for chunk in bit_interface.shensu_logic(
        name,
        sites,
        forms,
        str(payload.get("message") or ""),
        str(payload.get("mode") or "人工客服"),
        loop_count=loop_count,
        stop_event=stop_event,
    ):
        _write_log(chunk)


def run_daily_task(payload, stop_event, job_file):
    from bit import bit_interface

    params = bit_interface.build_daily_task_params(payload)
    task_id = json.loads(job_file.read_text(encoding="utf-8"))["job_id"]
    log_path = job_file.with_name("daily-task.log")
    bit_interface._reset_daily_task_log(log_path)
    task_lock = bit_interface.bit_daily_task.acquire_daily_task_lock(
        owner=f"local_agent:{task_id}", mode=params["mode"], task_id=task_id,
    )
    if task_lock is None:
        raise RuntimeError("无法取得 daily_task 任务锁")
    finished = threading.Event()

    def relay_control_and_logs(shared_stop):
        with log_path.open(encoding="utf-8", errors="replace") as stream:
            while True:
                if stop_event.is_set():
                    shared_stop.set()
                _write_log(stream.read())
                if finished.wait(0.2):
                    _write_log(stream.read())
                    return

    try:
        # Manager proxies can be passed to daily_task's Windows process pool.
        with multiprocessing.Manager() as manager:
            shared_stop = manager.Event()
            if stop_event.is_set():
                shared_stop.set()
            relay = threading.Thread(target=relay_control_and_logs, args=(shared_stop,), daemon=True)
            relay.start()
            bit_interface.register_thread_log_queue(bit_interface.DailyTaskLogSink(log_path))
            try:
                _write_log(f"本机 Agent 开始执行 daily_task：{task_id}\n")
                return bit_interface.execute_daily_task(
                    params, task_lock, shared_stop, task_id, log_path, manager.dict(),
                )
            finally:
                bit_interface.unregister_thread_log_queue()
                finished.set()
                relay.join()
    finally:
        task_lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-file", required=True)
    parser.add_argument("--cancel-file", required=True)
    args = parser.parse_args(argv)
    job = json.loads(Path(args.job_file).read_text(encoding="utf-8"))
    configure_execution_context(job)
    stop_event = threading.Event()
    threading.Thread(
        target=_watch_cancel,
        args=(Path(args.cancel_file), stop_event),
        name="agent-cancel-watcher",
        daemon=True,
    ).start()
    try:
        job_type = str(job.get("job_type") or "")
        if job_type == "appeal":
            run_appeal(job.get("payload") or {}, stop_event)
        elif job_type == "daily_task":
            job_file = Path(args.job_file)
            result = run_daily_task(job.get("payload") or {}, stop_event, job_file)
            job_file.with_name("result.json").write_text(
                json.dumps(result, ensure_ascii=False), encoding="utf-8",
            )
        else:
            raise ValueError(f"不支持的 Agent 任务类型：{job_type}")
        return 2 if stop_event.is_set() else 0
    except Exception as exc:
        _write_log(f"本机 Agent 业务执行失败：{exc}\n")
        traceback.print_exc()
        return 2 if stop_event.is_set() else 1


if __name__ == "__main__":
    raise SystemExit(main())
