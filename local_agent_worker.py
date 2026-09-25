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
from urllib.parse import urlsplit


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
    os.environ["BIT_EXECUTION_JOB_ID"] = str(job.get("job_id") or "").strip()
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
    mode = str(payload.get("mode") or "人工客服")
    appeal_copy_mode = str(payload.get("appeal_copy_mode") or "普通模式")
    shensu_kwargs = {"loop_count": loop_count, "stop_event": stop_event}
    if appeal_copy_mode == "AI话术模式":
        shensu_kwargs["appeal_copy_mode"] = appeal_copy_mode
        shensu_kwargs["deepseek_api_key"] = str(
            payload.get("deepseek_api_key") or ""
        )
    for chunk in bit_interface.shensu_logic(
        name,
        sites,
        forms,
        str(payload.get("message") or ""),
        mode,
        **shensu_kwargs,
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


def _agent_awp_actor(job, payload):
    """Build the signed-in workbench actor used by the remote AWP store."""
    actor = payload.get("actor") if isinstance(payload, dict) else None
    if isinstance(actor, dict) and actor.get("id") not in (None, ""):
        return {
            "id": actor.get("id"),
            "username": actor.get("username") or "",
            "display_name": actor.get("display_name") or actor.get("username") or "",
        }
    created_by_id = (job or {}).get("created_by_id")
    if created_by_id in (None, ""):
        raise ValueError("AI核重核价 Agent 任务缺少创建账号")
    return {
        "id": created_by_id,
        "username": str((job or {}).get("created_by_name") or "").strip(),
        "display_name": str((job or {}).get("created_by_name") or "").strip(),
    }


def _agent_awp_api_key(job_id, env_name="DASHSCOPE_API_KEY"):
    """Read the account-bound DashScope key without putting it in job JSON."""
    from erp.ai_weight_price.credentials import api_key

    configured = api_key(env_name or "DASHSCOPE_API_KEY")
    if configured:
        return configured
    from bit.bit_db_api import _request

    data = _request(
        "GET",
        f"/api/local-agents/jobs/{job_id}/credentials",
        params={"provider": "dashscope"},
        timeout=30,
    ) or {}
    key = str(data.get("api_key") or "").strip()
    if not key:
        raise ValueError("当前账号未配置可用的 DashScope API Key")
    return key


def _agent_awp_runtime_api_key(service, job_id):
    config = service.config.load()
    if urlsplit(str(config.get("api_base_url") or "")).hostname in {
        "localhost", "127.0.0.1", "::1",
    }:
        return ""
    return _agent_awp_api_key(job_id, config.get("api_key_env"))


def run_ai_weight_price(job, stop_event, job_file):
    """Run the existing Playwright AWP service on the Agent's Edge."""
    from erp.ai_weight_price.service import Service

    payload = dict((job or {}).get("payload") or {})
    # job.json lives at <agent-data>/jobs/<job-id>/job.json. Keep the Edge
    # profile and local visual frames beside the jobs directory so releases
    # can be replaced without logging the Agent computer out.
    root = job_file.parents[2] / "ai-weight-price"
    root.mkdir(parents=True, exist_ok=True)
    service = Service(root, storage_backend="api", migrate_legacy_state=False)
    actor = _agent_awp_actor(job, payload)
    service.bind_actor(actor, view_all=False)
    identity = {
        "target": "agent",
        "agent_id": os.environ.get("BIT_EXECUTION_AGENT_ID", ""),
        "agent_name": os.environ.get("BIT_EXECUTION_AGENT_NAME", ""),
        "hostname": os.environ.get("BIT_EXECUTION_HOSTNAME", socket.gethostname()),
        "job_id": str((job or {}).get("job_id") or ""),
    }
    service.store.set_state("execution_identity", identity)

    remote_log = service.store.log

    def log(message, task_id=None, level="INFO"):
        _write_log(f"[AI核重核价] {message}")
        return remote_log(message, task_id, level)

    # Browser/service code already records the same event to the central store;
    # this wrapper only mirrors it to the Agent's live stdout for the console.
    service.store.log = log
    action = str(payload.get("action") or "").strip().lower()
    if action not in {
        "login/open", "login/confirm", "login/supplier", "categories/refresh",
        "start", "continue", "stop", "terminate", "skip-current", "retry",
        "manual-execute", "probe", "weight-dimensions-update",
    }:
        raise ValueError("AI核重核价 Agent 操作无效")

    if action == "login/open":
        service.open_login()
        return {"status": "success", "message": "已在 Agent 本机打开智赢和1688登录页面"}
    if action == "login/supplier":
        service.open_supplier_login()
        return {"status": "success", "message": "已在 Agent 本机打开1688登录页面"}
    if action == "login/confirm":
        result = service.confirm_login()
        return {"status": "success", "message": "Agent 已确认智赢登录", "login": result}
    if action == "categories/refresh":
        options = service.categories()
        return {"status": "success", "message": f"Agent 已刷新智赢分类：{len(options)} 个", "options": options}
    if action == "stop":
        service.stop()
        return {"status": "stopped", "message": "Agent 已请求停止核重核价"}
    if action == "terminate":
        return {"status": "success", "message": "Agent 已终止当前核重核价批次", **service.terminate_current()}
    if action == "manual-execute":
        result = service.manual_execute(
            payload.get("task_id"),
            {
                "weight_g": payload.get("weight_g"),
                "net_income_usd": payload.get("net_income_usd"),
                "note": payload.get("note") or "",
            },
            actor.get("display_name") or actor.get("username") or "Agent",
        )
        return {"status": "success", "message": "Agent 已完成人工核验回写", "task": result}
    if action == "weight-dimensions-update":
        rows = payload.get("records") or []
        if not isinstance(rows, list) or not rows:
            raise ValueError("智赢重量尺寸更新缺少产品记录")
        results = []

        def browser_log(message, *args, **kwargs):
            level = kwargs.get("level") or "INFO"
            log(message, level=level)

        with service.weight_dimensions_browser(browser_log) as browser:
            for item in rows:
                item = dict(item or {})
                order_number = str(item.get("order_number") or "").strip()
                product_id = str(item.get("product_id") or "").strip()
                try:
                    if not product_id:
                        raise ValueError("缺少智赢产品 id")
                    result = browser.update_package_by_product_id(
                        product_id,
                        str(item.get("actual_weight_g") or "").strip(),
                        str(item.get("actual_dimensions_cm") or "").strip(),
                    ) or {}
                    results.append({
                        "order_number": order_number,
                        "product_id": product_id,
                        "status": "success",
                        "message": (
                            f"重量 {result.get('weight_g', item.get('actual_weight_g'))}g、"
                            f"尺寸 {result.get('dimensions_cm', item.get('actual_dimensions_cm'))}cm、"
                            "产品级别“重点”已保存并回读确认"
                        ),
                    })
                except Exception as exc:
                    results.append({
                        "order_number": order_number,
                        "product_id": product_id,
                        "status": "error",
                        "message": str(exc)[:500],
                    })
        success_count = sum(row.get("status") == "success" for row in results)
        return {
            "status": "success" if success_count == len(results) else "partial",
            "message": f"Agent 已完成智赢产品更新，成功 {success_count}/{len(results)} 条",
            "rows": results,
        }
    if action == "probe":
        service.start("probe")
    elif action == "start":
        runtime_api_key = _agent_awp_runtime_api_key(
            service, str((job or {}).get("job_id") or "")
        )
        service.start(
            str(payload.get("mode") or "pipeline"),
            task_id=payload.get("task_id") or None,
            selection=payload.get("selection"),
            max_items=payload.get("max_items", 10),
            resume=payload.get("resume") is True,
            runtime_api_key=runtime_api_key,
        )
    elif action == "continue":
        runtime_api_key = _agent_awp_runtime_api_key(
            service, str((job or {}).get("job_id") or "")
        )
        service.continue_after_human(runtime_api_key=runtime_api_key)
    elif action == "skip-current":
        service.skip_current_exception()
    elif action == "retry":
        runtime_api_key = _agent_awp_runtime_api_key(
            service, str((job or {}).get("job_id") or "")
        )
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("重试任务缺少商品编号")
        service.retry(task_id)
        service.start("process", task_id=task_id, runtime_api_key=runtime_api_key)

    # start/continue/skip/retry launch Service's own background thread. Keep
    # the Agent job alive until that thread reaches a terminal state so the
    # queue lease and cancellation file control the complete browser run.
    while service.thread is not None and service.thread.is_alive():
        if stop_event.is_set() and not service.stop_event.is_set():
            service.stop()
            if service.store.state("agent_terminate_requested") in {
                str((job or {}).get("job_id") or ""),
                True,
            }:
                service.terminate_current()
            _write_log("Agent 收到停止指令，正在结束当前浏览器操作")
        stop_event.wait(0.5)
    if service.thread is not None:
        service.thread.join(timeout=1)
    return service.status()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-file", required=True)
    parser.add_argument("--cancel-file", required=True)
    args = parser.parse_args(argv)
    job_path = Path(args.job_file)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    payload = dict(job.get("payload") or {})
    sensitive_removed = False
    for key in ("deepseek_api_key", "dashscope_api_key", "runtime_api_key"):
        if payload.pop(key, None) is not None:
            sensitive_removed = True
    if sensitive_removed:
        sanitized_job = dict(job)
        sanitized_job["payload"] = payload
        job_path.write_text(
            json.dumps(sanitized_job, ensure_ascii=False), encoding="utf-8"
        )
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
        elif job_type == "ai_weight_price":
            job_file = Path(args.job_file)
            result = run_ai_weight_price(job, stop_event, job_file)
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
