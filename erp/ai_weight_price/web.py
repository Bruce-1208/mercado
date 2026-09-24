import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, Response, g, jsonify, render_template, request, send_file, session


def create_blueprint(service, authorize=None, agent_dispatch=None):
    bp = Blueprint("ai_weight_price", __name__, template_folder=str(Path(__file__).resolve().parents[2] / "bit" / "templates"))

    @bp.before_request
    def guard():
        if authorize:
            denied = authorize("ai_weight_price.view" if request.method == "GET" else "ai_weight_price.execute")
            if denied is not None:
                return denied
        user = session.get("workbench_user") if authorize else None
        is_admin = bool(user and (
            user.get("is_platform_admin") or user.get("role_key") == "super_admin"
        ))
        service.bind_actor(user, view_all=is_admin)
        g.awp_is_admin = is_admin
        # Read-only task data is shared through the server store and must remain
        # available from every authenticated workbench. Browser automation still
        # belongs to the workstation that owns Edge; remote mutations are only
        # accepted when explicitly routed to a selected Agent.
        host = urlsplit(request.host_url).hostname
        local_request = (
            host in ("127.0.0.1", "localhost", "::1")
            and request.remote_addr in ("127.0.0.1", "::1")
        )
        g.awp_local_request = local_request
        body = request.get_json(silent=True) if request.method != "GET" else {}
        body = body if isinstance(body, dict) else {}
        requested_target = str(
            body.get("execution_target") or request.args.get("execution_target") or ""
        ).strip().lower()
        g.awp_execution_target = requested_target if requested_target in {"agent", "local"} else ""
        g.awp_agent_request = bool(
            agent_dispatch and requested_target == "agent"
        )
        if not local_request and not authorize:
            if request.path == "/ai-weight-price":
                return render_template("ai_weight_price.html", local_only=True, can_execute=False)
            return jsonify(message="请打开本机 http://127.0.0.1:5000 控制台使用AI核重核价"), 403
        remote_config_write = bool(
            authorize
            and request.path == "/api/ai-weight-price/config"
            and request.method == "PUT"
        )
        if (
            not local_request
            and request.method != "GET"
            and not remote_config_write
            and not g.awp_agent_request
        ):
            return jsonify(message="请打开本机 http://127.0.0.1:5000 控制台使用AI核重核价"), 403
        if request.method != "GET":
            if not request.is_json or request.headers.get("X-AWP-Request") != "1":
                return jsonify(message="请求校验失败，请从本机控制台操作"), 403
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                return jsonify(message="不允许跨站请求"), 403
            g.awp_action = True
            if not isinstance(request.get_json(silent=True), dict):
                raise ValueError("请求体必须是 JSON 对象")

    def dispatch_agent(action, body=None):
        if not getattr(g, "awp_agent_request", False):
            return None
        if not agent_dispatch:
            return jsonify(message="当前服务未配置 Agent 执行入口"), 503
        return agent_dispatch(action, body or {})

    @bp.errorhandler(ValueError)
    def bad_request(exc):
        return jsonify(message=str(exc)), 400

    @bp.errorhandler(KeyError)
    def not_found(exc):
        return jsonify(message=str(exc)), 404

    @bp.after_request
    def record_result(response):
        response.headers["Cache-Control"] = "no-store"
        if getattr(g, "awp_action", False) and response.status_code >= 400:
            body = response.get_json(silent=True) or {}
            message = body.get("message") or f"服务请求失败（HTTP {response.status_code}），请检查本机服务日志"
            service.store.log(f"操作失败 [{request.path}]：{message}", level="ERROR")
            service.store.set_state("action_error", {"message": message, "at": time.time(), "path": request.path})
        elif getattr(g, "awp_action", False) and response.status_code < 400:
            service.store.set_state("action_error", None)
        return response

    @bp.get("/ai-weight-price")
    def page():
        local_request = bool(getattr(g, "awp_local_request", False))
        has_execute_permission = not authorize or authorize("ai_weight_price.execute") is None
        can_execute = has_execute_permission and (local_request or bool(agent_dispatch))
        return render_template(
            "ai_weight_price.html",
            local_only=False,
            can_execute=can_execute,
            can_configure=has_execute_permission,
            remote_read_only=not local_request,
            plugin_launch_only=bool(authorize),
            agent_launch_available=bool(agent_dispatch),
            is_admin=bool(getattr(g, "awp_is_admin", False)),
        )

    @bp.get("/api/ai-weight-price/status")
    def status():
        return jsonify(**service.status())

    @bp.post("/api/ai-weight-price/model/check")
    def check_model():
        return jsonify(service.check_model_connection())

    @bp.get("/api/ai-weight-price/visuals/<filename>")
    def visual_frame(filename):
        if not re.fullmatch(r"[0-9a-f]{32}\.jpg", filename):
            return jsonify(message="画面不存在"), 404
        path = service.store.root / "visuals" / filename
        if not path.is_file():
            return jsonify(message="画面不存在"), 404
        return send_file(path, mimetype="image/jpeg")

    @bp.post("/api/ai-weight-price/login/open")
    def open_login():
        dispatched = dispatch_agent("login/open", request.get_json(silent=True))
        if dispatched is not None:
            return dispatched
        service.open_login()
        return jsonify(message="已在 Edge 打开智赢和1688，请分别登录后回到控制台确认")

    @bp.post("/api/ai-weight-price/login/confirm")
    def confirm_login():
        if request.get_json().get("acknowledged") is not True:
            raise ValueError("请先人工完成登录，再点击“我已成功登录”")
        dispatched = dispatch_agent("login/confirm", request.get_json(silent=True))
        if dispatched is not None:
            return dispatched
        return jsonify(message="已确认智赢和1688登录，请选择分类（可留空）和页码范围", login=service.confirm_login())

    @bp.post("/api/ai-weight-price/supplier/login/open")
    def open_supplier_login():
        dispatched = dispatch_agent("login/supplier", request.get_json(silent=True))
        if dispatched is not None:
            return dispatched
        service.open_supplier_login()
        return jsonify(message="已在同一Edge窗口打开1688，请完成登录后继续；不影响智赢登录状态")

    @bp.post("/api/ai-weight-price/categories/refresh")
    def categories():
        dispatched = dispatch_agent("categories/refresh", request.get_json(silent=True))
        if dispatched is not None:
            return dispatched
        return jsonify(options=service.categories(), meta=service.store.state("categories_meta", {}))

    @bp.get("/api/ai-weight-price/categories")
    def cached_categories():
        response = jsonify(options=service.store.state("categories", []), meta=service.store.state("categories_meta", {}))
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.route("/api/ai-weight-price/config", methods=["GET", "PUT"])
    def config():
        if request.method == "GET":
            return jsonify(service.config.load())
        with service.idle():
            old = service.config.load()
            result = service.config.save(request.get_json())
            if any(old[key] != result[key] for key in ("cdp_url", "erp_list_url")):
                service.store.set_state("login", {"confirmed": False})
                service.store.set_state("categories", [])
            service.store.log("已保存核重核价配置")
        return jsonify(result)

    @bp.get("/api/ai-weight-price/tasks")
    def tasks():
        return jsonify(service.store.list(request.args.get("status", ""), request.args.get("search", ""),
                                          max(1, int(request.args.get("page", 1))), 50))

    @bp.get("/api/ai-weight-price/run-items")
    def run_items():
        current = service.store.state("run", {}) or {}
        run_id = current.get("run_id") or service.store.state("latest_run_id")
        if not run_id:
            return jsonify(run={}, total=0, rows=[])
        data = service.store.run_items(run_id, request.args.get("status", ""),
                                       request.args.get("search", ""),
                                       max(1, int(request.args.get("page", 1))), 50)
        if not service.status()["running"]:
            for row in data["rows"]:
                if row.get("execution_result") == "执行中" and row.get("status") == "pending":
                    row["execution_result"] = "待重试"
                    row["execution_reason"] = "上次运行已中断，商品进度已保留，可点击重试"
        return jsonify(data)

    @bp.route("/api/ai-weight-price/tasks/<key>", methods=["GET", "PATCH"])
    def task(key):
        if request.method == "PATCH":
            from flask import session
            actor = (session.get("workbench_user") or {}).get("username", "本机操作者")
            service.edit(key, request.get_json(), actor)
        return jsonify(service.store.get(key))

    @bp.post("/api/ai-weight-price/tasks/<key>/manual-execute")
    def manual_execute(key):
        body = request.get_json(silent=True) or {}
        if getattr(g, "awp_agent_request", False):
            return dispatch_agent("manual-execute", {**body, "task_id": key})
        from flask import session
        actor = (session.get("workbench_user") or {}).get("username", "本机操作者")
        result = service.manual_execute(key, request.get_json(), actor)
        return jsonify(message="人工核验已直接同步智赢，保存后回读确认通过", task=result)

    @bp.post("/api/ai-weight-price/tasks/<key>/retry")
    def retry(key):
        body = request.get_json(silent=True) or {}
        if getattr(g, "awp_agent_request", False):
            return dispatch_agent("retry", {**body, "task_id": key})
        run = body.get("run", True)
        pending = service.store.get(key)["status"] == "pending"
        if run:
            service.require_login(service.config.load())
            service.preflight(service.config.load(), "process", key)
        if not pending:
            service.retry(key)
        if run:
            service.start("process", key)
        return jsonify(message=("已启动此待处理商品" if pending else "已启动此任务重试；不会重复咨询商家")
                       if run else ("任务已是待处理状态" if pending else "已重新排队"))

    @bp.post("/api/ai-weight-price/start")
    def start():
        body = request.get_json()
        dispatched = dispatch_agent("start", body)
        if dispatched is not None:
            return dispatched
        mode = body.get("mode", "pipeline")
        if mode != "probe" and not body.get("task_id"):
            from .config import selection_params
            selection_params(body.get("selection"), service.config.load())
        service.start(mode, body.get("task_id"), body.get("selection"), body.get("max_items", 10),
                      resume=body.get("resume") is True)
        return jsonify(message="任务已启动")

    @bp.post("/api/ai-weight-price/stop")
    def stop():
        dispatched = dispatch_agent("stop", request.get_json(silent=True))
        if dispatched is not None:
            return dispatched
        service.stop()
        return jsonify(message="停止请求已记录，当前操作结束后保留进度退出")

    @bp.post("/api/ai-weight-price/terminate")
    def terminate():
        dispatched = dispatch_agent("terminate", request.get_json(silent=True))
        if dispatched is not None:
            return dispatched
        result = service.terminate_current()
        message = ("本次任务已终止并清空；历史商品与登录状态已保留" if result["cleared"]
                   else "终止请求已记录；当前操作退出后将自动清空本次任务数据")
        return jsonify(message=message, **result)

    @bp.post("/api/ai-weight-price/continue")
    def continue_after_human():
        if request.get_json().get("acknowledged") is not True:
            raise ValueError("请先在可见Edge完成1688登录或人机审核，并打开确认开关")
        dispatched = dispatch_agent("continue", request.get_json(silent=True))
        if dispatched is not None:
            return dispatched
        service.continue_after_human()
        return jsonify(message="已从暂停的当前商品继续执行")

    @bp.post("/api/ai-weight-price/skip-current")
    def skip_current():
        dispatched = dispatch_agent("skip-current", request.get_json(silent=True))
        if dispatched is not None:
            return dispatched
        service.skip_current_exception()
        return jsonify(message="已跳过当前异常商品，继续执行下一件")

    @bp.get("/api/ai-weight-price/logs")
    def logs():
        limit = min(10000, max(1, int(request.args.get("limit", 200))))
        return jsonify(service.store.logs(max(0, int(request.args.get("after", 0))), limit))

    @bp.get("/api/ai-weight-price/export")
    def export():
        status = request.args.get("status", "")
        if request.args.get("format") == "xlsx":
            from .reports import execution_xlsx
            run_id = request.args.get("run_id", "")
            if run_id == "latest":
                run_id = service.store.state("latest_run_id")
                if not run_id:
                    raise ValueError("暂无执行批次，请先启动一次任务")
            if run_id:
                batch, rows = service.store.run_report(run_id)
                rows = [row for row in rows if not status or row["status"] == status]
            else:
                batch = {}
                rows = service.store.list(status, page_size=1000000)["rows"]
            return Response(execution_xlsx(rows, batch), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": 'attachment; filename="ai-weight-price-execution.xlsx"'})
        return Response(service.store.csv(status), content_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="ai-weight-price-{status or "all"}.csv"'})

    return bp
