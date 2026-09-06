"""Versioned business worker downloaded and launched by ``local_agent.py``."""

from __future__ import annotations

import argparse
import json
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


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-file", required=True)
    parser.add_argument("--cancel-file", required=True)
    args = parser.parse_args(argv)
    job = json.loads(Path(args.job_file).read_text(encoding="utf-8"))
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
        else:
            raise ValueError(f"不支持的 Agent 任务类型：{job_type}")
        return 2 if stop_event.is_set() else 0
    except Exception as exc:
        _write_log(f"本机 Agent 业务执行失败：{exc}\n")
        traceback.print_exc()
        return 2 if stop_event.is_set() else 1


if __name__ == "__main__":
    raise SystemExit(main())

