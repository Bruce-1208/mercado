"""一次性执行声誉和侵权采集，默认使用 MySQL 直连。"""

import argparse
import json
import os
import sys


def build_parser():
    parser = argparse.ArgumentParser(
        description="一次性串行执行声誉采集和侵权采集"
    )
    parser.add_argument("--db-mode", default="mysql", choices=("mysql", "api"))
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--stagger-min-seconds", type=float, default=5)
    parser.add_argument("--stagger-max-seconds", type=float, default=10)
    parser.add_argument("--wait-min-seconds", type=float, default=180)
    parser.add_argument("--wait-max-seconds", type=float, default=300)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONUNBUFFERED"] = "1"
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    os.environ["BIT_DB_MODE"] = args.db_mode
    os.environ["BIT_REPUTATION_MAX_WORKERS"] = str(max(1, args.workers))
    os.environ["BIT_INFRACTION_MAX_WORKERS"] = str(max(1, args.workers))
    os.environ["BIT_COLLECTION_STAGGER_MIN_SECONDS"] = str(
        max(0, args.stagger_min_seconds)
    )
    os.environ["BIT_COLLECTION_STAGGER_MAX_SECONDS"] = str(
        max(0, args.stagger_max_seconds)
    )
    os.environ["BIT_REPUTATION_INFRACTION_WAIT_MIN_SECONDS"] = str(
        max(0, args.wait_min_seconds)
    )
    os.environ["BIT_REPUTATION_INFRACTION_WAIT_MAX_SECONDS"] = str(
        max(0, args.wait_max_seconds)
    )

    # 必须先设置环境变量再导入，bit_db_api 会在导入时固定本进程的数据源模式。
    from bit import bit_db_api
    from bit.bit_main import run_reputation_infraction_then_daily

    if bit_db_api.DB_MODE != args.db_mode:
        raise RuntimeError(
            f"数据库模式不一致：要求 {args.db_mode}，实际 {bit_db_api.DB_MODE}"
        )

    print(
        "一次性采集启动："
        f"db_mode={bit_db_api.DB_MODE}, workers={max(1, args.workers)}, "
        f"stagger={max(0, args.stagger_min_seconds)}-"
        f"{max(0, args.stagger_max_seconds)}s",
        flush=True,
    )
    result = run_reputation_infraction_then_daily()
    print(
        "COLLECTION_FINAL_RESULT="
        + json.dumps(result, ensure_ascii=False, default=str),
        flush=True,
    )
    if result is None:
        return 1
    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
