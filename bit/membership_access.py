"""Membership capability ceilings, independent of editable role grants.

Legacy internal roles remain compatible. Public registration uses basic_member;
upgrades are explicit administrator actions, never inferred from a client payload.
"""
MEMBER_ROLES = frozenset({'basic_member', 'premium_member'})
OWN_STORE_ROLES = MEMBER_ROLES | {'member'}
BASIC_PERMISSIONS = frozenset({'order_print.view', 'order_print.execute'})
PUBLIC_PATHS = frozenset({'/login', '/logout', '/register', '/api/login', '/api/register',
                          '/api/register/send-code'})
BASIC_PAGES = frozenset({'/', '/settings/profile', '/billing', '/print-wallet'})
BASIC_ACCOUNT_PATHS = frozenset({'/api/account/profile', '/api/account/password',
                                 '/api/mercado-tokens', '/api/mercado-tokens/authorization'})
CATALOG_PREFIXES = ('/api/mercado-products', '/api/mercado-collection', '/api/1688-products',
                    '/api/mercado-publish-records', '/mercado-collection/items',
                    '/api/browser-extension/1688', '/api/browser-extension/zying',
                    '/api/browser-extension/collect', '/api/browser-extension/management-categories',
                    '/api/mercado-management-categories', '/api/catalog')
AI_PREFIXES = ('/api/ai-original-products', '/api/ai-original', '/api/ai-videos', '/ai-videos',
               '/api/browser-extension/ai-original-products')


def permits(role, permission):
    if role == 'basic_member':
        return permission in BASIC_PERMISSIONS
    if role == 'premium_member':
        return permission != '*' and not permission.startswith(('access.', 'shop_status.'))
    return True


def limit_permissions(role, permissions):
    # Never let an old or manually edited wildcard expand a membership role.
    return [key for key in permissions if permits(role, key)]


def under(path, prefix):
    return path == prefix or path.startswith(prefix + '/')


def required_permission(path, method):
    suffix = 'view' if method in {'GET', 'HEAD', 'OPTIONS'} else 'execute'
    if any(under(path, prefix) for prefix in AI_PREFIXES):
        return 'ai_content.' + suffix
    if any(under(path, prefix) for prefix in CATALOG_PREFIXES):
        return 'catalog.' + suffix
    return None


def basic_path_allowed(path, method):
    if path in PUBLIC_PATHS or path in BASIC_PAGES:
        return True
    if path.startswith('/static/'):
        return True
    if path in BASIC_ACCOUNT_PATHS:
        return method in {'GET', 'HEAD'} or path.startswith('/api/account/')
    # Base members may see their assigned stores but cannot reassign ownership.
    if path.startswith(('/api/billing/', '/api/print-wallet/')):
        return True  # These handlers separately enforce self-only billing access.
    if path == '/api/orders' or path.startswith('/api/orders/'):
        return method in {'GET', 'HEAD'} or path in {'/api/orders/print', '/api/orders/outbound/confirm'}
    return False  # Newly added modules are unavailable until explicitly included.
