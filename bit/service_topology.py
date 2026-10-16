"""Ownership and bounded resources for independently supervised task services."""
import os

GROUPS = ('worker', 'orders', 'stores', 'maintenance')
DEFAULT_CONNECTIONS = {'web': 6, 'worker': 4, 'orders': 4, 'stores': 4, 'maintenance': 2}


def isolation_enabled():
    return os.environ.get('BIT_WORKER_ISOLATION', '').lower() in {'1', 'true', 'yes', 'on'}


def worker_group():
    group = os.environ.get('BIT_WORKER_GROUP', 'worker')
    if group not in GROUPS:
        raise ValueError('Unknown worker group')
    return group


def owns_background(group):
    mode = os.environ.get('BIT_SERVICE_MODE', 'combined')
    return mode != 'web' and (not isolation_enabled() or worker_group() == group)


def group_port(group):
    base = int(os.environ.get('BIT_WORKER_PORT', '5001'))
    port = int(os.environ.get(f'BIT_{group.upper()}_PORT', str(base + GROUPS.index(group))))
    if not 1024 <= port <= 65535 or port == 5000:
        raise ValueError(f'Invalid port for {group}')
    return port


def request_group(path):
    if path.startswith(('/api/order-sync/', '/api/db/order-sync/',
                        '/api/orders/zying-sync/', '/api/orders/purchase-tracking/',
                        '/api/local-agents/zying-sync/',
                        '/api/browser-extension/zying-order-sync/',
                        '/api/browser-extension/purchase-tracking/')) or path in {
        '/api/orders/print', '/api/db/orders/labels',
    }:
        return 'orders'
    if path.startswith(('/api/store-links/sync/', '/api/db/store-links/sync/',
                        '/api/product-diagnosis', '/api/db/product-diagnosis',
                        '/api/store-links/bulk-update', '/api/db/store-links/bulk-update',
                        '/api/prohibited-listings/sync/', '/api/db/prohibited-listings/sync/',
                        '/api/official-infractions/sync', '/api/db/official-infractions/sync', '/api/db/official-infractions/live',
                        '/api/mercado-reputation/')) or path in {
        '/api/store-links/delete', '/api/db/store-links/delete',
        '/api/store-links/metrics', '/api/db/store-links/metrics',
        '/api/overview-auto-sync', '/api/db/overview-auto-sync', '/api/mercado-tokens/exchange',
    }:
        return 'stores'
    if path in {'/api/mercado-shipping-rates', '/api/mercado-shipping-rates/refresh'}:
        return 'maintenance'
    return 'worker'


def validate_topology(environments):
    """Reject overcommitted or colliding configurations before starting children."""
    ports = [5000] + [int(env['BIT_LISTEN_PORT']) for name, env in environments.items() if name != 'web']
    if len(set(ports)) != len(ports):
        raise ValueError('后台服务端口重复')
    total = sum(int(env['MYSQL_POOL_MAX_CONNECTIONS']) for env in environments.values())
    budget = int(os.environ.get('BIT_TOTAL_DB_CONNECTIONS', '20'))
    if any(not 1 <= int(env['MYSQL_POOL_MAX_CONNECTIONS']) <= 100 for env in environments.values()):
        raise ValueError('后台连接额度必须在 1..100 之间')
    if total > budget:
        raise ValueError(f'各服务数据库连接额度合计 {total} 超过总预算 {budget}')
    return total
