"""Queue collection checks for the signed-in user's browser extension."""
from contextlib import contextmanager
from erp import collection_ai_check as repository


@contextmanager
def queue_lock(service):
    connection = repository.db._connect()
    name = 'collection-ai:' + str((service.store.actor() or {}).get('id') or '')
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT GET_LOCK(%s, 5) AS acquired', (name,))
            if not (cursor.fetchone() or {}).get('acquired'):
                raise ValueError('任务正在启动，请稍后重试')
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute('SELECT RELEASE_LOCK(%s)', (name,))
        connection.close()


def enqueue(service, ids):
    if not isinstance(ids, list):
        raise ValueError('collection_item_ids 必须是数组')
    with queue_lock(service):
        if service.store.state('collection_pending', None) or (service.store.state('run', {}) or {}).get('outcome') in {'running', 'blocked'}:
            raise ValueError('已有核重核价任务，请先完成或停止')
        rows = repository.products(ids)
        service.store.set_state('collection_pending', [r['collection_item_id'] for r in rows])
        for row in rows:
            repository.save_result(row, 'queued', '等待泽顺插件领取任务')
    return {'count': len(rows), 'message': '已排队；保持同账号泽顺插件在线，约一分钟内开始1688搜图'}


def claim(service, payload):
    from bit.ai_weight_price_client import start
    with queue_lock(service):
        ids = service.store.state('collection_pending', None)
        if not ids or (service.store.state('run', {}) or {}).get('outcome') in {'running', 'blocked'}:
            return {'action': 'done'}
        rows = repository.products(ids)
        result = start(service, {**payload, 'max_items': len(rows)}, collection_products=rows)
        service.store.set_state('collection_pending', None)
        return result
