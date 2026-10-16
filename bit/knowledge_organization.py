"""Enterprise boundary for knowledge reads, imports and background workers."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import re

_current = ContextVar("knowledge_organization", default=None)
LEGACY_ORGANIZATION = "wuhan-zeshun"
HEADER = "X-Knowledge-Organization"


def normalize(value):
    key = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{2,64}", key):
        raise ValueError("侵权知识库缺少有效企业标识")
    return LEGACY_ORGANIZATION if key == "default" else key


def organization():
    scoped = _current.get()
    if scoped is not None:
        return scoped
    from flask import has_request_context, g, request
    if has_request_context():
        from bit import bit_interface as app
        agent = (getattr(g, "internal_agent_user", None)
                 or getattr(g, "browser_extension_user", None)
                 or getattr(g, "local_executor_user", None))
        if agent:
            return normalize(agent.get("organization_key"))
        # This header is accepted only behind the internal API authentication.
        if request.path.startswith("/api/db/"):
            return normalize(request.headers.get(HEADER) or LEGACY_ORGANIZATION)
        user = app.get_current_workbench_user() or {}
        return normalize(app.workbench_selected_organization(user) or user.get("organization_key"))
    # Background workers retain the initiating enterprise through the existing
    # task identity context; only unowned legacy CLI jobs use the default.
    from erp.ai_usage_billing import current_usage_context
    return normalize(current_usage_context().get("organization_key"))


@contextmanager
def scope(key):
    token = _current.set(normalize(key))
    try:
        yield
    finally:
        _current.reset(token)


def bind(function):
    """Capture only the enterprise, never a Flask request or a user's session."""
    key = organization()
    @wraps(function)
    def run(*args, **kwargs):
        with scope(key):
            return function(*args, **kwargs)
    return run
