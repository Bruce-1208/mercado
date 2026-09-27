"""Collection-check coordination shared by Agent, server Edge and legacy plugin."""
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


def prepare(service, ids, target):
    """Called under queue_lock before dispatch, so two clicks cannot start twice."""
    from bit.ai_weight_price_client import start
    if not isinstance(ids, list):
        raise ValueError('collection_item_ids 必须是数组')
    if service.store.state('collection_pending', None):
        raise ValueError('已有等待插件领取的任务，请先停止旧任务')
    rows = repository.products(ids)
    start(service, {'max_items': len(rows)}, collection_products=rows)
    run = service.store.state('run', {})
    run.update(source_type='mercado_collection', execution_target=target,
               message='等待 Agent 执行' if target == 'agent' else '正在连接服务器 Edge')
    service.store.set_state('run', run)
    service.store.save_run(run)
    for row in rows:
        repository.save_result(row, 'queued', run['message'])
    return run


def step(service, run_id, action, payload, runtime_api_key=''):
    with queue_lock(service):
        return _step(service, run_id, action, payload, runtime_api_key)


def _step(service, run_id, action, payload, runtime_api_key=''):
    from bit import ai_weight_price_client as client
    run = service.store.state('run', {}) or {}
    if run.get('run_id') != run_id or run.get('source_type') != 'mercado_collection':
        raise ValueError('采集核查批次已变更，请重新启动')
    if run.get('outcome') != 'running' or service.store.state('stop_requested', False):
        return {'action': 'done'}
    if action == 'next':
        result = client._next_action(service)
    elif action == 'adapt':
        from erp.ai_weight_price.models import Models
        return Models(client._config(service, runtime_api_key), service.store.log).supplier_dom(payload['snapshot'])
    elif action in {'search', 'detail', 'fail', 'stop'}:
        result = client.dispatch(service, action, {**payload, 'runtime_api_key': runtime_api_key})
        if action == 'fail':
            for key in run.get('task_ids', [])[int(run.get('cursor') or 0):]:
                repository.save_result(service.store.get(key), 'failed', payload.get('error') or '浏览器执行失败')
    else:
        raise ValueError('采集核查操作无效')
    # The browser only needs the next action, never account/model configuration.
    return {key: value for key, value in result.items() if key != 'data'}


def start_server(service, run_id, runtime_api_key):
    import logging
    import threading
    from erp.ai_weight_price.collection_runner import run
    actor = service.store.actor()
    service.stop_event.clear()

    def worker():
        with service.store.actor_scope(actor, view_all=False):
            try:
                run(service, lambda action, payload: step(service, run_id, action, payload, runtime_api_key),
                    service.stop_event)
            except Exception:
                logging.exception('服务器 Edge 采集核重核价失败')

    service.thread = threading.Thread(target=worker, name='collection-ai-edge', daemon=True)
    service.thread.start()
