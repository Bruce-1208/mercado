"""Size-chart HTTP routes with explicit company, permission and store scopes."""

import re

from flask import jsonify, render_template, request

from bit import bit_db_api


READ_ACTIONS = frozenset({"list", "options", "requirements", "records", "batch-status"})
ACTIONS = READ_ACTIONS | {"create", "update", "copy", "delete", "preview", "upload", "reconcile", "batch-upload"}


def register_routes(app, *, login_required, internal_api_required, authorized_token_ids,
                    current_user, has_permission, organization_key, reject_client_mode):
    def json_object():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError("请求须为 JSON 对象")
        # Bound imports and large grids before forwarding them to the worker.
        if request.content_length and request.content_length > 2_000_000:
            raise ValueError("尺码表数据不得超过 2 MB")
        return payload

    def execute(action, payload=None, *, template_id=None, internal=False):
        try:
            if action not in ACTIONS:
                raise ValueError("不支持的尺码表操作")
            if not internal:
                user = current_user() or {}
                permission = "size_charts.view" if action in READ_ACTIONS else "size_charts.execute"
                if not has_permission(user, permission):
                    raise PermissionError("当前账号没有尺码表操作权限")
                scope = authorized_token_ids()
                company = organization_key()
            else:
                envelope = json_object()
                company = envelope.get("organization_key")
                values = envelope.get("allowed_token_ids")
                if values is not None and (not isinstance(values, list)
                        or any(type(value) is not int or value <= 0 for value in values)):
                    raise ValueError("店铺权限范围须为正整数数组")
                scope = set(values) if values is not None else None
                payload = envelope.get("payload")
                if not isinstance(payload, dict):
                    raise ValueError("操作数据须为 JSON 对象")
            if not isinstance(company, str) or not re.fullmatch(r"[a-z0-9_.-]{2,64}", company):
                raise ValueError("企业标识无效")
            if payload is None:
                payload = dict(request.args) if request.method == "GET" else json_object()
            if template_id is not None:
                payload = {**payload, "template_id": template_id}
            result = bit_db_api.size_chart_call(action, payload,
                organization_key=company, allowed_token_ids=scope)
        except PermissionError as exc:
            return jsonify(status="error", message=str(exc)), 403
        except KeyError:
            return jsonify(status="error", message="尺码表模板或记录不存在"), 404
        except (ValueError, TypeError) as exc:
            return jsonify(status="error", message=str(exc)), 400
        except Exception:
            app.logger.exception("尺码表操作失败")
            return jsonify(status="error", message="尺码表服务暂时不可用，请刷新上传记录核对结果后再操作"), 502
        response = jsonify(status="success", data=result)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/size-charts")
    @login_required
    def size_charts_page():
        user = current_user() or {}
        if not has_permission(user, "size_charts.view"):
            return jsonify(status="error", message="无权查看尺码表管理"), 403
        return render_template("size_charts.html", current_user=user,
            can_manage=has_permission(user, "size_charts.execute"),
            embedded=request.args.get("embedded") == "1")

    @app.route("/api/size-charts/templates", methods=["GET", "POST"])
    @login_required
    def api_size_chart_templates():
        return execute("list" if request.method == "GET" else "create")

    @app.route("/api/size-charts/templates/<int:template_id>", methods=["PATCH", "DELETE"])
    @login_required
    def api_size_chart_template(template_id):
        return execute("update" if request.method == "PATCH" else "delete", template_id=template_id)

    @app.route("/api/size-charts/templates/<int:template_id>/copy", methods=["POST"])
    @login_required
    def api_size_chart_copy(template_id):
        return execute("copy", template_id=template_id)

    @app.route("/api/size-charts/options", methods=["GET"])
    @login_required
    def api_size_chart_options():
        return execute("options")

    @app.route("/api/size-charts/requirements", methods=["POST"])
    @login_required
    def api_size_chart_requirements():
        return execute("requirements")

    @app.route("/api/size-charts/preview", methods=["POST"])
    @login_required
    def api_size_chart_preview():
        return execute("preview")

    @app.route("/api/size-charts/upload", methods=["POST"])
    @login_required
    def api_size_chart_upload():
        return execute("upload")

    @app.route("/api/size-charts/batch", methods=["GET", "POST"])
    @login_required
    def api_size_chart_batch():
        return execute("batch-status" if request.method == "GET" else "batch-upload")

    @app.route("/api/size-charts/records", methods=["GET"])
    @login_required
    def api_size_chart_records():
        return execute("records")

    @app.route("/api/size-charts/reconcile", methods=["POST"])
    @login_required
    def api_size_chart_reconcile():
        return execute("reconcile")

    @app.route("/api/db/size-charts/<action>", methods=["POST"])
    @internal_api_required
    def api_db_size_charts(action):
        blocked = reject_client_mode()
        return blocked if blocked is not None else execute(action, internal=True)
