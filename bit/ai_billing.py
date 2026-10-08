"""Read-only AI usage bills with server-resolved account and enterprise scopes."""
import logging
import inspect
from functools import wraps

from flask import Blueprint, g, has_request_context, jsonify, render_template, request


def actor_context(user, selected_organization=""):
    user = user or {}
    organization = str(selected_organization or user.get("organization_key") or "wuhan-zeshun").strip()
    if organization == "default":
        organization = "wuhan-zeshun"
    name = str(user.get("display_name") or user.get("username") or "").strip()
    return {
        "actor_id": int(user.get("id") or 0), "actor_name": name,
        "salesperson": name, "organization_key": organization,
    }


def install_usage_context(current_user, selected_organization):
    """Resolve decorated browser/Agent identities only when a model is called."""
    from erp.ai_usage_billing import set_usage_context_resolver

    def resolve():
        if not has_request_context():
            return {}
        user = (getattr(g, "internal_agent_user", None)
                or getattr(g, "browser_extension_user", None)
                or getattr(g, "local_executor_user", None)
                or current_user())
        if not user:
            return {}
        return actor_context(user, selected_organization(user))

    set_usage_context_resolver(resolve)


def account_usage_context(user_id_argument, *, get_user):
    """Restore the creator of persisted schedules before their paid calls."""
    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        def run(*args, **kwargs):
            from erp.ai_usage_billing import current_usage_context, usage_context
            current = current_usage_context()
            values = signature.bind(*args, **kwargs).arguments
            owner_id = int(values.get(user_id_argument) or 0)
            if int(current.get("actor_id") or 0) or not owner_id:
                return function(*args, **kwargs)
            owner = get_user(owner_id)
            if not owner or not owner.get("is_active", True):
                raise ValueError("AI 任务所属账号不存在或已停用")
            with usage_context(**actor_context(owner)):
                return function(*args, **kwargs)

        return run
    return decorate


def create_blueprint(*, login_required, current_user, selected_organization, storage):
    blueprint = Blueprint("ai_billing", __name__)

    @blueprint.get("/ai-billing")
    @login_required
    def page():
        return render_template("ai_billing.html", user=current_user())

    @blueprint.get("/api/ai-billing")
    @login_required
    def records():
        user = current_user() or {}
        if not user.get("id"):
            return jsonify(status="error", message="请先登录"), 401
        scope = actor_context(user, selected_organization(user))
        # Names are display fields; ownership is always checked by immutable ID.
        manager = user.get("role_key") in {"super_admin", "enterprise_admin"}
        payload = {
            "action": "list", "organization_key": scope["organization_key"],
            "scope_user_ids": None if manager else [scope["actor_id"]],
            **{key: request.args.get(key, "") for key in (
                "start_date", "end_date", "salesperson", "task_type", "model",
                "keyword", "page", "page_size",
            )},
        }
        try:
            response = jsonify(status="success", data=storage(payload))
            response.headers["Cache-Control"] = "no-store"
            return response
        except (ValueError, TypeError) as exc:
            return jsonify(status="error", message=str(exc)), 400
        except PermissionError as exc:
            return jsonify(status="error", message=str(exc)), 403
        except Exception:
            logging.exception("读取 AI 账单失败")
            return jsonify(status="error", message="AI 账单暂时无法读取，请稍后重试"), 503

    return blueprint
