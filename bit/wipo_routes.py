"""WIPO sync endpoints, with workbench and internal server authorization."""
from flask import jsonify, request

from bit import bit_db_api
from bit.wipo_sync import get_service


def register_routes(app, *, login_required, internal_api_required, current_user,
                    has_permission, is_client, reject_client_mode):
    def response(action, *, internal=False):
        try:
            if internal:
                blocked = reject_client_mode()
                if blocked is not None:
                    return blocked
                manage = request.args.get("manage", "1") != "0"
            else:
                user = current_user() or {}
                permission = ("infringement_knowledge.view" if request.method == "GET"
                              else "infringement_knowledge.manage")
                if not has_permission(user, permission):
                    raise PermissionError("当前账号没有 WIPO 同步操作权限")
                manage = has_permission(user, "infringement_knowledge.manage")
            payload = None
            if request.method != "GET":
                payload = request.get_json(silent=True)
                if not isinstance(payload, dict):
                    raise ValueError("请求须为 JSON 对象")
            if is_client() and not internal:
                result = bit_db_api._request(request.method,
                    "/api/db/infringement-knowledge/wipo/" + action,
                    **({"params": {"manage": "1" if manage else "0"}} if request.method == "GET"
                       else {"json": payload}))
            else:
                service = get_service(app.secret_key)
                if action == "settings":
                    result = service.settings(manage=manage) if request.method == "GET" else service.save_settings(payload)
                elif action == "status":
                    result = service.status()
                else:
                    result = service.start_sync()
            reply = jsonify(status="success", data=result)
            reply.headers["Cache-Control"] = "no-store"
            return reply
        except PermissionError as exc:
            return jsonify(status="error", message=str(exc)), 403
        except ValueError as exc:
            return jsonify(status="error", message=str(exc)), 400
        except Exception:
            app.logger.error("WIPO 同步服务暂时不可用")
            return jsonify(status="error", message="WIPO 同步服务暂时不可用，请稍后重试"), 502

    @app.route("/api/infringement-knowledge/wipo/settings", methods=["GET", "PUT"])
    @login_required
    def api_wipo_settings():
        return response("settings")

    @app.route("/api/infringement-knowledge/wipo/status", methods=["GET"])
    @login_required
    def api_wipo_status():
        return response("status")

    @app.route("/api/infringement-knowledge/wipo/sync", methods=["POST"])
    @login_required
    def api_wipo_sync():
        return response("sync")

    @app.route("/api/db/infringement-knowledge/wipo/settings", methods=["GET", "PUT"])
    @internal_api_required
    def api_db_wipo_settings():
        return response("settings", internal=True)

    @app.route("/api/db/infringement-knowledge/wipo/status", methods=["GET"])
    @internal_api_required
    def api_db_wipo_status():
        return response("status", internal=True)

    @app.route("/api/db/infringement-knowledge/wipo/sync", methods=["POST"])
    @internal_api_required
    def api_db_wipo_sync():
        return response("sync", internal=True)
