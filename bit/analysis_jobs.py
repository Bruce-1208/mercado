"""Bounded background analysis with permission-scoped polling and result reuse."""
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import hashlib
import json
import threading
import time
import uuid

from flask import copy_current_request_context, current_app, jsonify, make_response, request, session

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='analysis')
_LOCK = threading.Lock()
_JOBS = {}


def background_analysis(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if request.headers.get('X-Analysis-Async') != '1':
            return view(*args, **kwargs)
        scope = json.dumps([session.get('workbench_user'), request.path, sorted(request.args.items(multi=True))], sort_keys=True, default=str)
        key = hashlib.sha256(scope.encode()).hexdigest()
        app = current_app._get_current_object()
        with _LOCK:
            now = time.monotonic()
            for old_key, (created, future) in list(_JOBS.items()):
                if future.done() and now - getattr(future, 'completed_at', now) > 60:
                    del _JOBS[old_key]
            job = _JOBS.get(key)
            if job is None:
                if len(_JOBS) >= 64:
                    return jsonify(status='error', message='分析任务较多，请稍后重试'), 503
                @copy_current_request_context
                def compute():
                    started = time.monotonic()
                    with app.app_context():
                        try:
                            response = make_response(view(*args, **kwargs))
                            return response.get_json(), response.status_code
                        finally:
                            app.logger.info('analysis_request id=%s path=%s queue_ms=%.1f compute_ms=%.1f',
                                request_id, request.path, (started - now) * 1000, (time.monotonic() - started) * 1000)
                request_id = uuid.uuid4().hex
                job = (now, _POOL.submit(compute))
                job[1].request_id = request_id
                job[1].add_done_callback(lambda completed: setattr(completed, 'completed_at', time.monotonic()))
                _JOBS[key] = job
            future = job[1]
        if not future.done():
            response = jsonify(status='pending', message='正在后台计算，请稍候…')
            response.status_code = 202
            response.headers['Retry-After'] = '2'
        else:
            try:
                payload, status = future.result()
                response = make_response(jsonify(payload), status)
                if status >= 400:
                    with _LOCK:
                        _JOBS.pop(key, None)
            except Exception:
                app.logger.exception('后台分析失败')
                with _LOCK:
                    _JOBS.pop(key, None)
                response = make_response(jsonify(status='error', message='分析失败，请重新查询'), 500)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Request-ID'] = getattr(future, 'request_id', key[:16])
        response.headers['Server-Timing'] = f'analysis_age;dur={(time.monotonic() - job[0]) * 1000:.1f}'
        return response
    return wrapper
