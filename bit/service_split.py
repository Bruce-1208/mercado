"""Optional single-owner scheduler service behind the public Web process.

Stateful task endpoints stay on one worker, including manual starts and status
reads. The private listener binds loopback and requires an authenticated hop.
No POST is retried: a lost response must not duplicate a business operation.
"""
import hashlib
import hmac
import os
import time

import requests
from flask import Response, g, jsonify, request, stream_with_context

MODES = {"combined", "web", "worker"}
HOP_HEADER = "X-Workbench-Worker-Auth"
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
STATEFUL_PREFIXES = (
    "/api/order-sync/", "/api/db/order-sync/", "/api/store-links/sync/",
    "/api/db/store-links/sync/", "/api/prohibited-listings/sync/",
    "/api/db/prohibited-listings/sync/", "/api/official-infractions/sync",
    "/api/db/official-infractions/sync", "/api/mercado-reputation/",
    "/api/orders/purchase-tracking/", "/api/zying-collection/",
    "/api/exports/", "/api/tasks/daily/", "/api/run_shensu",
    "/api/mercado-collection/", "/api/db/mercado-collection/",
    "/api/mercado-products/", "/api/db/mercado-products/",
    "/api/browser-extension/zying/", "/api/local-executor/tasks/daily/",
    "/api/local-executor/run_shensu",
    "/api/browser-extension/ai-weight-price/client/",
    "/api/db/ai-weight-price/store",
)
STATEFUL_PATHS = {
    "/api/overview-auto-sync", "/api/db/overview-auto-sync",
    "/api/mercado-collection/start", "/api/mercado-collection/stop",
    "/api/mercado-collection/status", "/api/mercado-products/publish",
    "/api/mercado-products/publish/status", "/api/mercado-publish-records/retry",
    "/api/mercado-shipping-rates", "/api/mercado-shipping-rates/refresh",
    "/api/exports", "/api/mercado-tokens/exchange",
    "/api/store-links/bulk-update", "/api/store-links/bulk-update/status",
    "/api/db/store-links/bulk-update", "/api/db/store-links/bulk-update/status",
}


def service_mode():
    mode = os.environ.get("BIT_SERVICE_MODE", "combined").strip().lower()
    if mode not in MODES:
        raise ValueError("BIT_SERVICE_MODE must be combined, web or worker")
    return mode


def worker_port():
    port = int(os.environ.get("BIT_WORKER_PORT", "5001"))
    if not 1024 <= port <= 65535 or port == 5000:
        raise ValueError("BIT_WORKER_PORT must be 1024..65535 and different from 5000")
    return port


def is_worker_path(path):
    return path in STATEFUL_PATHS or any(path.startswith(p) for p in STATEFUL_PREFIXES)


def signature(secret, method, path, body, timestamp, original_ip=""):
    path = requests.utils.requote_uri(path)
    digest = hashlib.sha256(body).hexdigest()
    message = f"{timestamp}\n{method}\n{path}\n{digest}\n{original_ip}".encode()
    return hmac.new(str(secret).encode(), message, hashlib.sha256).hexdigest()


def install_service_routing(app):
    def route_request():
        mode = service_mode()
        if mode == "combined":
            return None
        if mode == "web" and not is_worker_path(request.path):
            return None
        path = request.path + ("?" + request.query_string.decode("latin1") if request.query_string else "")
        body = request.get_data()
        if mode == "worker":
            supplied = request.headers.get(HOP_HEADER, "")
            timestamp, _, digest = supplied.partition(":")
            original_ip = request.headers.get("X-Workbench-Original-IP", "")
            try:
                fresh = abs(time.time() - int(timestamp)) <= 60
            except ValueError:
                fresh = False
            if (request.remote_addr not in {"127.0.0.1", "::1"} or not fresh or
                    not hmac.compare_digest(digest, signature(app.secret_key, request.method, path, body, timestamp, original_ip))):
                return jsonify(status="error", message="Private worker request required"), 403
            g.worker_original_remote_addr = original_ip
            return None
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP_BY_HOP and not k.lower().startswith(("x-workbench-", "x-forwarded-"))}
        timestamp = str(int(time.time()))
        headers["X-Workbench-Original-IP"] = request.remote_addr or ""
        headers[HOP_HEADER] = timestamp + ":" + signature(app.secret_key, request.method, path, body, timestamp, request.remote_addr or "")
        # Use a literal loopback address; proxy environment variables must never
        # send sessions or internal credentials to a public HTTP proxy.
        transport = requests.Session()
        transport.trust_env = False
        try:
            upstream = transport.request(request.method,
                f"http://127.0.0.1:{worker_port()}{path}", data=body, headers=headers,
                timeout=(2, 120), allow_redirects=False, stream=True)
        except requests.RequestException:
            transport.close()
            return jsonify(status="error", message="后台服务暂时不可用，请稍后检查任务状态；请求未自动重试"), 503
        excluded = HOP_BY_HOP | {"content-encoding"}
        response_headers = [(k, v) for k, v in upstream.headers.items()
                            if k.lower() not in excluded and k.lower() != "set-cookie"]
        for value in upstream.raw.headers.getlist("Set-Cookie"):
            response_headers.append(("Set-Cookie", value))
        def chunks():
            try:
                yield from upstream.iter_content(chunk_size=65536)
            finally:
                upstream.close()
                transport.close()
        response = Response(stream_with_context(chunks()), status=upstream.status_code,
                            headers=response_headers)
        response.call_on_close(upstream.close)
        response.call_on_close(transport.close)
        return response
    # Run before public auth hooks; the worker executes the original permission
    # checks against the same signed session and current database permissions.
    app.before_request_funcs.setdefault(None, []).insert(0, route_request)
