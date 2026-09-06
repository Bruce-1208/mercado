import socket
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, Response, jsonify, render_template, request


def create_blueprint(service, authorize=None):
    bp = Blueprint("ai_weight_price", __name__, template_folder=str(Path(__file__).resolve().parents[2] / "bit" / "templates"))

    @bp.before_request
    def guard():
        if authorize:
            denied = authorize("ai_weight_price.view" if request.method == "GET" else "ai_weight_price.execute")
            if denied is not None:
                return denied
        # Module operates on the computer running this service. Never silently run on the public console server.
        host = urlsplit(request.host_url).hostname
        if host not in ("127.0.0.1", "localhost", "::1") or request.remote_addr not in ("127.0.0.1", "::1"):
            if request.path == "/ai-weight-price":
                return render_template("ai_weight_price.html", local_only=True, can_execute=False)
            return jsonify(message="请打开本机 http://127.0.0.1:5000 控制台使用AI核重核价"), 403
        if request.method != "GET":
            if not request.is_json or request.headers.get("X-AWP-Request") != "1":
                return jsonify(message="请求校验失败，请从本机控制台操作"), 403
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                return jsonify(message="不允许跨站请求"), 403
            if not isinstance(request.get_json(silent=True), dict):
                raise ValueError("请求体必须是 JSON 对象")

    @bp.errorhandler(ValueError)
    def bad_request(exc):
        return jsonify(message=str(exc)), 400

    @bp.errorhandler(KeyError)
    def not_found(exc):
        return jsonify(message=str(exc)), 404

    @bp.get("/ai-weight-price")
    def page():
        can_execute = not authorize or authorize("ai_weight_price.execute") is None
        return render_template("ai_weight_price.html", local_only=False, can_execute=can_execute)

    @bp.get("/api/ai-weight-price/status")
    def status():
        return jsonify(**service.status(), computer=socket.gethostname())

    @bp.post("/api/ai-weight-price/login/open")
    def open_login():
        service.open_login()
        return jsonify(message="已打开 Edge，请人工登录后回到控制台点击“我已成功登录”")

    @bp.post("/api/ai-weight-price/login/confirm")
    def confirm_login():
        if request.get_json().get("acknowledged") is not True:
            raise ValueError("请先人工完成登录，再点击“我已成功登录”")
        return jsonify(message="登录已确认，请选择分类（可留空）和页码范围", login=service.confirm_login())

    @bp.post("/api/ai-weight-price/categories/refresh")
    def categories():
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
            service.store.log("已保存可视化配置")
        return jsonify(result)

    @bp.get("/api/ai-weight-price/tasks")
    def tasks():
        return jsonify(service.store.list(request.args.get("status", ""), request.args.get("search", ""),
                                          max(1, int(request.args.get("page", 1))), 50))

    @bp.route("/api/ai-weight-price/tasks/<key>", methods=["GET", "PATCH"])
    def task(key):
        if request.method == "PATCH":
            from flask import session
            actor = (session.get("workbench_user") or {}).get("username", "本机操作者")
            service.edit(key, request.get_json(), actor)
        return jsonify(service.store.get(key))

    @bp.post("/api/ai-weight-price/tasks/<key>/retry")
    def retry(key):
        run = request.get_json().get("run", True)
        if run:
            service.require_login(service.config.load())
            service.preflight(service.config.load(), "process", key)
        service.retry(key)
        if run:
            service.start("process", key)
        return jsonify(message="已启动此任务重试；不会重复咨询商家" if run else "已重新排队")

    @bp.post("/api/ai-weight-price/start")
    def start():
        body = request.get_json()
        mode = body.get("mode", "process")
        if mode != "probe" and not body.get("task_id"):
            from .config import selection_params
            selection_params(body.get("selection"), service.config.load())
        service.start(mode, body.get("task_id"), body.get("selection"))
        return jsonify(message="任务已启动")

    @bp.post("/api/ai-weight-price/stop")
    def stop():
        service.stop()
        return jsonify(message="停止请求已记录，当前操作结束后保留进度退出")

    @bp.post("/api/ai-weight-price/circuit/reset")
    def reset():
        if request.get_json().get("acknowledged") is not True:
            raise ValueError("请先处理浏览器验证码/限制，并勾选确认")
        with service.idle():
            service.store.set_state("circuit", None)
            service.store.log("人工确认已处理页面限制，解除聊天熔断；去重和限额继续保留", level="WARNING")
        return jsonify(message="已解除熔断，请手动开始处理")

    @bp.get("/api/ai-weight-price/logs")
    def logs():
        return jsonify(service.store.logs(max(0, int(request.args.get("after", 0)))))

    @bp.get("/api/ai-weight-price/export")
    def export():
        status = request.args.get("status", "")
        return Response(service.store.csv(status), content_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="ai-weight-price-{status or "all"}.csv"'})

    return bp
