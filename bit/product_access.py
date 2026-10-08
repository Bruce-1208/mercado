"""Authenticated enterprise and collector scope for the unified product catalog."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json

HEADER = 'X-Product-Actor'
_current = ContextVar('product_actor', default=None)
LEGACY_ORGANIZATION = 'wuhan-zeshun'


def install(app, user_resolver, selected_organization_resolver):
    # Works for both imported WSGI apps and bit_interface.py run as __main__.
    app.extensions['product_access'] = {
        'user': user_resolver, 'selected_organization': selected_organization_resolver,
    }


def actor():
    scoped = _current.get()
    if scoped is not None:
        return scoped
    from flask import has_request_context, current_app, g, request
    if not has_request_context():
        return None
    resolver = current_app.extensions.get("product_access")
    if not resolver:
        return None
    if request.path.startswith('/api/db/'):
        # Only authenticated internal routes consume this header.
        user = getattr(g, 'internal_agent_user', None)
        if not user:
            raw = request.headers.get(HEADER)
            user = json.loads(raw) if raw else None
    else:
        user = getattr(g, 'browser_extension_user', None) or resolver['user']()
    if not user or user.get('access_version') != 1:
        return None
    user = dict(user)
    if not request.path.startswith('/api/db/'):
        user['organization_key'] = resolver['selected_organization'](user) or user.get('organization_key') or LEGACY_ORGANIZATION
    return user


def organization(user=None):
    value = str((user or actor() or {}).get('organization_key') or LEGACY_ORGANIZATION).strip()
    return LEGACY_ORGANIZATION if value == 'default' else value


def aliases(user):
    return {str(user.get(key) or '').strip().casefold() for key in ('display_name', 'username')} - {''}


def is_admin(user):
    return user.get('role_key') in {'enterprise_admin', 'super_admin'}


def owns(row, user):
    owner_id = row.get('owner_user_id')
    if owner_id:
        return str(owner_id) == str(user.get('id'))
    return str(row.get('owner_name') or '').strip().casefold() in aliases(user)


def can_view(row, user):
    return (organization(row) == organization(user)
            and (row.get('visibility') != 'private' or is_admin(user) or owns(row, user)))


def predicate(table, user=None):
    user = user or actor()
    if not user:
        return '', []
    prefix = f'`{table}`.' if table else ''
    sql = f'{prefix}`organization_key` = %s'
    params = [organization(user)]
    if not is_admin(user):
        names = sorted(aliases(user))
        owner = f'{prefix}`owner_user_id` = %s'
        params.append(int(user.get('id') or 0))
        if names:
            owner += f" OR ({prefix}`owner_user_id` IS NULL AND LOWER(TRIM({prefix}`owner_name`)) IN ({', '.join(['%s'] * len(names))}))"
            params.extend(names)
        sql += f" AND ({prefix}`visibility` = 'enterprise' OR ({owner}))"
    return sql, params


@contextmanager
def scope(user):
    token = _current.set(dict(user) if user else None)
    try:
        yield
    finally:
        _current.reset(token)


def bind(function):
    user = actor()
    @wraps(function)
    def run(*args, **kwargs):
        with scope(user):
            return function(*args, **kwargs)
    return run


def forwarding_headers():
    user = actor()
    return {HEADER: json.dumps(user, ensure_ascii=True)} if user else {}
