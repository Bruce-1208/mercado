"""Read-only AI usage bills with server-resolved account and enterprise scopes."""
import logging
import inspect
import json
import re
from functools import wraps

from flask import Blueprint, abort, g, has_request_context, jsonify, render_template, request


def can_view_ai_costs(user):
    return bool(user and user.get("role_key") == "super_admin")


_PRIVATE_COST_FIELDS = frozenset({
    "generation_budget", "generation_cost_summary", "cost_cap_yuan",
    "spent_yuan", "reserved_yuan", "remaining_yuan", "cap_yuan", "cost_yuan",
    "cost_nano", "reserved_nano", "cap_nano", "budget_limit_enabled",
    "estimated_cost_cny", "cost_known", "cost_status", "rate_metadata",
    "pricing_source", "pricing_verified_on",
    "total_tokens", "prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "cached_tokens",
})
_COST_DIAGNOSTICS = (
    "费用记录", "费用待确认", "调用费用", "剩余预算", "本次预算",
    "历史费用", "待确认费用", "已记录费用", "预留费用", "模型计费估算",
)


def hide_cost_diagnostic(text):
    # Older task logs already contain formatted budget summaries. Keep the
    # actionable product error while removing each appended accounting field.
    if not (any(marker in text for marker in (*_COST_DIAGNOSTICS, '金额不限额', '待核对预留', '复用结果'))
            or re.search(r'\b[\d,]+\s*Tokens?\b', text, re.I)):
        return text
    lines = []
    for line in text.split('\n'):
        parts = [part for part in re.split(r'\s*·\s*', line) if not (
            any(marker in part for marker in (*_COST_DIAGNOSTICS, '金额不限额', '待核对预留', '复用结果'))
            or re.search(r'\b[\d,]+\s*Tokens?\b', part, re.I))]
        lines.append(' · '.join(parts))
    return '\n'.join(lines).strip() or '生成任务需要重试或人工处理，已保留生成结果。'


def hide_ai_costs(value, *, diagnostic=False):
    """Copy public payloads; never modify durable ledgers or worker state."""
    if isinstance(value, dict):
        return {key: hide_ai_costs(item, diagnostic=diagnostic or key in {
            "message", "error", "ai_error", "errors", "logs", "log", "blocked_reason", "last_error",
        }) for key, item in value.items() if key not in _PRIVATE_COST_FIELDS}
    if isinstance(value, list):
        return [hide_ai_costs(item, diagnostic=diagnostic) for item in value]
    if isinstance(value, str):
        # Product snapshots and persisted task results can be JSON strings.
        if value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                pass
            else:
                cleaned = hide_ai_costs(decoded, diagnostic=diagnostic)
                return json.dumps(cleaned, ensure_ascii=False) if cleaned != decoded else value
        if diagnostic:
            return hide_cost_diagnostic(value)
    return value


def install_cost_visibility(app, current_user):
    @app.after_request
    def filter_public_ai_costs(response):
        # DB transport is authenticated separately and must retain worker ledgers.
        if (not request.path.startswith("/api/") or request.path.startswith("/api/db/")
                or not response.is_json or response.is_streamed):
            return response
        payload = response.get_json(silent=True)
        if payload is None:
            return response
        cleaned = hide_ai_costs(payload)
        if cleaned == payload:
            return response
        user = (getattr(g, "internal_agent_user", None)
                or getattr(g, "browser_extension_user", None)
                or getattr(g, "local_executor_user", None) or current_user())
        response.headers["Cache-Control"] = "no-store"
        if not can_view_ai_costs(user):
            response.set_data(app.json.dumps(cleaned))
        return response


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
        if not can_view_ai_costs(current_user()):
            abort(403)
        return render_template("ai_billing.html", user=current_user())

    @blueprint.get("/api/ai-billing")
    @login_required
    def records():
        user = current_user() or {}
        if not user.get("id"):
            return jsonify(status="error", message="请先登录"), 401
        if not can_view_ai_costs(user):
            return jsonify(status="error", message="仅超级管理员可查看 AI 账单"), 403
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
