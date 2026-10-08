"""Project shared synchronization state onto the viewer's authorized stores."""

from collections import defaultdict


PUBLIC_SYNC_PATHS = frozenset({
    "/api/order-sync/start", "/api/order-sync/status",
    "/api/store-links/sync/start", "/api/store-links/sync/status",
    "/api/prohibited-listings/sync/start", "/api/prohibited-listings/sync/status",
    "/api/official-infractions/sync", "/api/official-infractions/sync/status",
})

SCHEDULE_FIELDS = frozenset({
    "auto_sync_enabled", "auto_sync_days", "auto_sync_hours",
    "scheduler_enabled", "sync_interval_seconds", "next_run_at",
    "recent_window_hours", "next_daily_status_at", "daily_status_hour",
    "store_workers", "detail_workers_per_store", "source", "target_reason",
})
COUNTERS = {
    "fetched_count": "fetched", "discovered_count": "discovered",
    "inserted_count": "inserted", "updated_count": "updated",
    "detail_count": "details", "detail_failed_count": "failed",
    "product_count": "products", "scanned_count": "scanned",
    "reason_matched_count": "reason_matched", "prohibited_count": "prohibited",
    "detection_scanned_count": "detection_scanned",
    "detection_matched_count": "detection_matched", "rights_holder_count": "rights_holder",
}
RESULT_FIELDS = frozenset(COUNTERS.values()) | {"detail_failed", "status", "yielded"}


def _id(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def scope_sync_state(state, tokens, allowed_ids):
    """Never pass through global logs, messages, totals or nested task state.

    Older workers identify active/failed stores by name. Only unique names in
    the complete token catalog can be resolved safely across member/tenant
    boundaries. New workers publish explicit target and active token ids.
    """
    state = state or {}
    allowed_ids = set(allowed_ids)
    by_id = {_id(row.get("id")): row for row in tokens}
    names = defaultdict(set)
    for token_id, row in by_id.items():
        for name in (row.get("display_name"), row.get("nickname"), str(token_id)):
            if name:
                names[str(name).strip().casefold()].add(token_id)

    def name_id(name):
        matches = names.get(str(name or "").strip().casefold(), set())
        return next(iter(matches)) if len(matches) == 1 else 0

    def store_name(token_id):
        row = by_id[token_id]
        return str(row.get("display_name") or row.get("nickname") or token_id)

    results = []
    for row in state.get("results") or []:
        if not isinstance(row, dict):
            continue
        token_id = _id(row.get("token_id")) if "token_id" in row else name_id(row.get("store"))
        if token_id not in allowed_ids or token_id not in by_id:
            continue
        results.append({
            **{key: row[key] for key in RESULT_FIELDS if key in row},
            "token_id": token_id, "store": store_name(token_id),
        })

    if "active_token_ids" in state:
        active_ids = {_id(value) for value in state.get("active_token_ids") or []}
    else:
        active_ids = {name_id(name) for name in state.get("active_stores") or []}
        active_ids.add(name_id(state.get("current_store")))
    active_ids &= allowed_ids & by_id.keys()
    active = [store_name(token_id) for token_id in sorted(active_ids)]
    if "target_token_ids" in state:
        target_ids = {_id(value) for value in state.get("target_token_ids") or []} & allowed_ids
    else:
        target_ids = active_ids | {row["token_id"] for row in results}
    completed_ids = {row["token_id"] for row in results if not row.get("yielded")}
    running = bool(state.get("running") and (active_ids or target_ids - completed_ids))
    failed = sum(row.get("status") in {"error", "partial"} for row in results)
    if running:
        status = "starting" if state.get("status") == "starting" else "running"
        message = "正在同步可见店铺" + (f"：{'、'.join(active)}" if active else "")
    elif results:
        status = "paused" if any(row.get("yielded") for row in results) else "partial" if failed else "completed"
        message = "可见店铺同步已暂停" if status == "paused" else "可见店铺同步完成"
        if failed:
            message += f"，{failed} 家店铺需重试"
    elif target_ids and state.get("status") in {"error", "busy", "queued", "paused"}:
        status = state["status"]
        message = {
            "error": "可见店铺同步失败，请重试", "busy": "同步服务忙，请稍后重试",
            "queued": "可见店铺同步已排队", "paused": "可见店铺同步已暂停",
        }[status]
    else:
        status, message = "idle", "等待可见店铺同步"

    visible = {key: state[key] for key in SCHEDULE_FIELDS if key in state}
    visible.update(
        running=running, status=status, message=message,
        total_stores=len(target_ids), processed_stores=len(completed_ids),
        active_token_ids=sorted(active_ids), active_stores=active,
        current_store="、".join(active), target_token_ids=sorted(target_ids),
        results=results, failed_count=failed, failed_stores=failed,
        logs=[f"{row['store']}：" + (
            "同步需重试" if row.get("status") in {"error", "partial"} else
            "同步已暂停" if row.get("yielded") else "同步完成"
        ) for row in results] + [f"正在同步 {name}" for name in active],
    )
    # Counters become available as each visible store completes. In-flight
    # global counters cannot be assigned to a store and must not be exposed.
    for counter, field in COUNTERS.items():
        if counter in state:
            if counter == "detail_failed_count" and any("detail_failed" in row for row in results):
                field = "detail_failed"
            visible[counter] = sum(_id(row.get(field)) for row in results)
    if target_ids or results:
        for key in ("task_id", "mode", "start_date", "end_date", "started_at", "finished_at"):
            if key in state:
                visible[key] = state[key]
    if isinstance(state.get("rights_holder_sync"), dict):
        rights = scope_sync_state(state["rights_holder_sync"], tokens, allowed_ids)
        visible["rights_holder_sync"] = rights
        visible["prohibited_running"] = running
        visible["running"] = running or rights["running"]
    return visible
