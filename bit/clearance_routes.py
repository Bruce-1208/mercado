"""HTTP integration without importing the central collector on API clients."""
from flask import jsonify, render_template, request

from bit import bit_db_api


def register_routes(app, *, login_required, internal_api_required, authorized_token_ids,
                    current_user, has_permission, reject_client_mode):
    def response(call):
        try:
            result = call()
        except PermissionError as exc:
            return jsonify(status="error", message=str(exc)), 403
        except KeyError:
            return jsonify(status="error", message="SKU 不存在或不在当前半托管店铺权限内，请先检测"), 404
        except (ValueError, TypeError) as exc:
            return jsonify(status="error", message=str(exc)), 400
        except Exception:
            app.logger.exception("半托管清退告警操作失败")
            return jsonify(status="error", message="清退告警数据暂时不可用，请稍后重试"), 502
        reply = jsonify(status="success", data=result)
        reply.headers["Cache-Control"] = "no-store"
        return reply

    def args(internal=False):
        token = request.args.get("token_id")
        if token and (not token.isdigit() or int(token) <= 0):
            raise ValueError("店铺编号无效")
        result = {"token_id": int(token) if token else None,
                  "site_id": request.args.get("site_id", "")}
        result["allowed_token_ids"] = (
            {int(v) for v in request.args.getlist("allowed_token_ids") if v.isdigit() and int(v) > 0}
            if internal and "allowed_token_ids" in request.args
            else None if internal else authorized_token_ids()
        )
        return result

    def list_response(internal=False):
        def execute():
            options = args(internal)
            page = int(request.args.get("page", 1))
            page_size = int(request.args.get("page_size", 100))
            if not 1 <= page <= 1000000 or not 1 <= page_size <= 200:
                raise ValueError("分页参数无效")
            return bit_db_api.list_clearance_alerts(**options, search=request.args.get("search", ""),
                alert_status=request.args.get("alert_status", ""), page=page, page_size=page_size)
        return response(execute)

    def write_scope(internal):
        if not internal:
            return authorized_token_ids()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError("请求须为 JSON 对象")
        values = payload.get("allowed_token_ids")
        if values is None:
            return None
        if not isinstance(values, list) or any(type(v) is not int or v <= 0 for v in values):
            raise ValueError("店铺权限范围无效")
        return set(values)

    def refresh_response(internal=False):
        def execute():
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                raise ValueError("请求须为 JSON 对象")
            token = payload.get("token_id")
            if token is not None and (type(token) is not int or token <= 0):
                raise ValueError("店铺编号无效")
            return bit_db_api.refresh_clearance_alerts(allowed_token_ids=write_scope(internal),
                    token_id=token, site_id=payload.get("site_id", ""))
        return response(execute)

    def facts_response(link_id, internal=False):
        def execute():
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                raise ValueError("核实信息须为 JSON 对象")
            allowed = write_scope(internal)
            facts = payload.get("facts") if internal else payload
            return bit_db_api.save_clearance_facts(link_id, facts, allowed_token_ids=allowed)
        return response(execute)

    @app.route("/clearance-alerts")
    @login_required
    def clearance_alerts_page():
        user = current_user() or {}
        if not has_permission(user, "order_analysis.view"):
            return jsonify(status="error", message="无权查看清退告警"), 403
        return render_template("clearance_alerts.html", current_user=user,
                               can_manage=has_permission(user, "order_analysis.execute"),
                               embedded=request.args.get("embedded") == "1")

    @app.route("/api/clearance-alerts", methods=["GET"])
    @login_required
    def api_clearance_alerts():
        return list_response()

    @app.route("/api/clearance-alerts/refresh", methods=["POST"])
    @login_required
    def api_clearance_alerts_refresh():
        return refresh_response()

    @app.route("/api/clearance-alerts/<int:link_id>/facts", methods=["PATCH"])
    @login_required
    def api_clearance_alerts_facts(link_id):
        return facts_response(link_id)

    @app.route("/api/db/clearance-alerts", methods=["GET"])
    @internal_api_required
    def api_db_clearance_alerts():
        blocked = reject_client_mode()
        return blocked if blocked else list_response(True)

    @app.route("/api/db/clearance-alerts/refresh", methods=["POST"])
    @internal_api_required
    def api_db_clearance_alerts_refresh():
        blocked = reject_client_mode()
        return blocked if blocked else refresh_response(True)

    @app.route("/api/db/clearance-alerts/<int:link_id>/facts", methods=["PATCH"])
    @internal_api_required
    def api_db_clearance_alerts_facts(link_id):
        blocked = reject_client_mode()
        return blocked if blocked else facts_response(link_id, True)
