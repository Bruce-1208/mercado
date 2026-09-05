"""Execution outcomes are independent of the platform's appeal decision."""

STATUS_LABELS = {
    "no_data": "无可申诉数据",
    "replied": "已收到回复，结果待确认",
    "needs_human": "需要人工处理",
    "reply_timeout": "回复超时",
    "sent_unknown": "发送结果不确定",
    "pre_send_failed": "发送前失败",
    "failed": "执行失败",
    "partial": "部分完成，需检查",
    "stopped": "已停止",
    "deadline_exceeded": "执行超时",
    "window_busy": "窗口被占用",
    "login_required": "需要登录",
    "rate_limited": "访问限频",
}


class AppealExecutionError(RuntimeError):
    def __init__(self, message, status="failed", sent=False, retryable=False):
        super().__init__(message)
        self.status = status
        self.sent = sent
        self.retryable = retryable


def execution_result(status, *, sent=False, acknowledged=False, response="", error="", **extra):
    return {
        "execution_status": status,
        "status": status,
        "message": STATUS_LABELS.get(status, status),
        "sent": bool(sent),
        "acknowledged": bool(acknowledged),
        "reply_received": bool(response),
        "response": response,
        "error": error,
        "retryable": False,
        **extra,
    }


def result_from_logs(records, error="", status=""):
    groups = [dict(r["extra"]["result"]) for r in records
              if r.get("event") == "group_result"
              and isinstance((r.get("extra") or {}).get("result"), dict)]
    if not status:
        states = {g["status"] for g in groups}
        status = "failed" if error else (
            next(iter(states)) if len(states) == 1 else "partial" if states else "no_data"
        )
    result = execution_result(
        status, sent=any(g.get("sent") for g in groups),
        acknowledged=bool(groups) and all(g.get("acknowledged") for g in groups),
        response=next((g.get("response", "") for g in reversed(groups) if g.get("response")), ""),
        error=error, groups=groups,
    )
    result["metrics"] = {
        "groups": len(groups),
        "sent_confirmed": sum(bool(g.get("acknowledged")) for g in groups),
        "replied": sum(bool(g.get("reply_received")) for g in groups),
        "reply_timeout": sum(g.get("status") == "reply_timeout" for g in groups),
        "sent_unknown": sum(g.get("status") == "sent_unknown" for g in groups),
    }
    return result


def task_execution_counts(value):
    """Walk existing single/mixed/loop return shapes without double counting groups."""
    counts = {}
    def visit(node):
        if isinstance(node, (list, tuple)):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            if isinstance(node.get("execution_counts"), dict):
                for key, count in node["execution_counts"].items():
                    counts[key] = counts.get(key, 0) + count
                return
            status = node.get("execution_status")
            if status:
                counts[status] = counts.get(status, 0) + 1
                return
            if node.get("error") or node.get("exit_reason"):
                counts["failed"] = counts.get("failed", 0) + 1
            for key in ("result", "results", "rounds"):
                if key in node:
                    visit(node[key])
    visit(value)
    return counts
