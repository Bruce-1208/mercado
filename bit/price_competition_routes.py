"""Authenticated public and central-server routes for the price competition center."""
from flask import jsonify, render_template, request

from bit import bit_db_api


ACTIONS = {"list", "stores", "refresh", "costs", "preview", "confirm"}


def register_routes(app, *, login_required, internal_api_required, authorized_token_ids,
                    current_user, has_permission, reject_client_mode):
    def execute(action, internal=False):
        try:
            if action not in ACTIONS:
                raise ValueError("不支持的价格竞争操作")
            payload = dict(request.args) if request.method == "GET" else request.get_json(silent=True)
            if not isinstance(payload, dict):
                raise ValueError("请求须为 JSON 对象")
            if internal:
                values = payload.get("allowed_token_ids")
                if values is not None and (not isinstance(values, list) or
                        any(type(value) is not int or value <= 0 for value in values)):
                    raise ValueError("店铺权限范围须为正整数数组")
                scope = set(values) if values is not None else None
                actor = str(payload.get("actor") or "")
                payload = payload.get("payload")
                if not isinstance(payload, dict):
                    raise ValueError("操作数据须为 JSON 对象")
            else:
                user = current_user() or {}
                permission = "price_competition.view" if action in {"list", "stores"} else "price_competition.execute"
                if not has_permission(user, permission):
                    raise PermissionError("当前账号没有价格竞争中心操作权限")
                scope = authorized_token_ids()
                actor = str(user.get("id") or user.get("username") or "unknown")
            result = bit_db_api.price_competition_call(action, payload, allowed_token_ids=scope, actor=actor)
        except PermissionError as exc:
            return jsonify(status="error", message=str(exc)), 403
        except KeyError:
            return jsonify(status="error", message="商品或建议预览不存在"), 404
        except (ValueError, TypeError) as exc:
            return jsonify(status="error", message=str(exc)), 400
        except Exception:
            app.logger.exception("价格竞争中心操作失败")
            return jsonify(status="error", message="价格竞争服务暂时不可用，请刷新后重试"), 502
        response = jsonify(status="success", data=result)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/price-competition")
    @login_required
    def price_competition_page():
        user = current_user() or {}
        if not has_permission(user, "price_competition.view"):
            return "当前账号没有价格竞争中心查看权限", 403
        return render_template("price_competition.html", current_user=user,
            can_manage=has_permission(user, "price_competition.execute"),
            embedded=request.args.get("embedded") == "1")

    @app.route("/api/price-competition", methods=["GET"])
    @login_required
    def price_competition_list():
        return execute("list")

    @app.route("/api/price-competition/stores", methods=["GET"])
    @login_required
    def price_competition_stores():
        return execute("stores")

    @app.route("/api/price-competition/<action>", methods=["POST"])
    @login_required
    def price_competition_action(action):
        return execute(action)

    @app.route("/api/db/price-competition/<action>", methods=["POST"])
    @internal_api_required
    def price_competition_internal(action):
        blocked = reject_client_mode()
        return blocked if blocked is not None else execute(action, internal=True)
