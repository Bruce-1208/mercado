"""Authenticated product diagnosis UI and central-server transport."""
from flask import Blueprint, jsonify, render_template, request

from bit import bit_db_api


def validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求格式无效")
    if set(payload) - {"token_ids", "site_id", "search", "risk", "category", "page", "page_size", "link_id"}:
        raise ValueError("商品诊断参数无效")
    result = dict(payload)
    for field, default, maximum in (("page", 1, 1000000), ("page_size", 50, 200), ("link_id", None, 2147483647)):
        if field not in result and default is None:
            continue
        value = result.get(field, default)
        if isinstance(value, bool):
            raise ValueError("分页或商品编号无效")
        try:
            value = int(value)
        except (ValueError, TypeError):
            raise ValueError("分页或商品编号无效") from None
        if not 1 <= value <= maximum:
            raise ValueError("分页或商品编号无效")
        result[field] = value
    site = str(result.get("site_id") or "").strip().upper()
    if site and site not in {"MLM", "MLB", "MLC", "MCO", "MLA", "MLU", "MPE", "MEC"}:
        raise ValueError("站点无效")
    result["site_id"] = site
    if result.get("risk", "") not in {"", "high", "medium", "low", "pending", "healthy"}:
        raise ValueError("风险筛选无效")
    if result.get("category", "") not in {"", "quality", "experience", "inventory", "listing"}:
        raise ValueError("问题类型无效")
    result["search"] = str(result.get("search") or "").strip()[:200]
    ids = result.get("token_ids") or []
    if not isinstance(ids, list) or any(type(v) is not int or v <= 0 for v in ids):
        raise ValueError("店铺编号无效")
    result["token_ids"] = ids
    return result


def register_routes(app, *, login_required, internal_api_required, current_user,
                    authorized_token_ids, has_permission, reject_client_mode):
    blueprint = Blueprint("product_diagnosis", __name__)

    def reply(call):
        try:
            data = call()
        except PermissionError as exc:
            return jsonify(status="error", message=str(exc)), 403
        except KeyError:
            return jsonify(status="error", message="商品不存在或不在当前店铺权限内，请先诊断"), 404
        except (ValueError, TypeError) as exc:
            return jsonify(status="error", message=str(exc)), 400
        except Exception:
            app.logger.exception("商品经营诊断操作失败")
            return jsonify(status="error", message="商品诊断暂时不可用，请稍后重试"), 502
        response = jsonify(status="success", data=data)
        response.headers["Cache-Control"] = "no-store"
        return response

    def allowed():
        ids = authorized_token_ids()
        return None if ids is None else sorted(ids)

    def call(operation, payload):
        user = current_user() or {}
        permission = "product_diagnosis.execute" if operation in {"start", "today"} else "product_diagnosis.view"
        if not has_permission(user, permission):
            raise PermissionError("当前账号没有商品经营诊断操作权限")
        if operation == "today" and not has_permission(user, "tasks.execute"):
            raise PermissionError("当前账号没有今日必办处理权限")
        return bit_db_api.product_diagnosis(operation, validate_payload(payload), allowed_token_ids=allowed(),
            actor=str(user.get("username") or user.get("display_name") or user.get("id") or "unknown"))

    @blueprint.route("/product-diagnosis")
    @login_required
    def page():
        user = current_user() or {}
        if not has_permission(user, "product_diagnosis.view"):
            return "当前账号没有商品经营诊断查看权限", 403
        return render_template("product_diagnosis.html", current_user=user,
            embedded=request.args.get("embedded") == "1",
            can_manage=has_permission(user, "product_diagnosis.execute"),
            can_task=has_permission(user, "product_diagnosis.execute") and has_permission(user, "tasks.execute"))

    @blueprint.route("/api/product-diagnosis", methods=["GET"])
    @login_required
    def listing():
        def execute():
            ids = request.args.getlist("token_id")
            if any(not v.isdigit() or int(v) <= 0 for v in ids):
                raise ValueError("店铺编号无效")
            return call("list", {**{key: request.args[key] for key in ("site_id", "search", "risk", "category", "page", "page_size") if key in request.args},
                                 "token_ids": [int(v) for v in ids]})
        return reply(execute)

    @blueprint.route("/api/product-diagnosis/start", methods=["POST"])
    @login_required
    def start():
        return reply(lambda: call("start", request.get_json(silent=True) or {}))

    @blueprint.route("/api/product-diagnosis/<int:link_id>", methods=["GET"])
    @login_required
    def detail(link_id):
        return reply(lambda: call("detail", {"link_id": link_id}))

    @blueprint.route("/api/product-diagnosis/<int:link_id>/today", methods=["POST"])
    @login_required
    def today(link_id):
        return reply(lambda: call("today", {"link_id": link_id}))

    @blueprint.route("/api/db/product-diagnosis", methods=["POST"])
    @internal_api_required
    def internal():
        blocked = reject_client_mode()
        if blocked:
            return blocked

        def execute():
            from bit.product_diagnosis import dispatch
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict) or data.get("operation") not in {"list", "start", "detail", "today"}:
                raise ValueError("商品诊断操作无效")
            return dispatch(data["operation"], validate_payload(data.get("payload") or {}),
                            allowed_token_ids=data.get("allowed_token_ids"), actor=str(data.get("actor") or "unknown"))
        return reply(execute)

    app.register_blueprint(blueprint)
