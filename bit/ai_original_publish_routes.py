"""Authenticated publication endpoints, separate from Mercado's legacy flow."""
import re
import threading

from flask import jsonify, request

from erp.ai_original_publish import PublicationService, PublicationStore, platform_id


def _public_error_message(error):
    message = str(error)
    if re.search(r"(?:api[-_ ]?key|client[-_ ]?secret|(?:access|refresh)[-_ ]?token|authorization)\s*[:=]|bearer\s+\S+", message, re.I):
        return "发布参数或授权信息无效，请检查配置后重试"
    return message


def register_routes(app, *, login_required, current_user, has_permission, load_rows,
                    own_store_only=lambda user: False, service=None):
    initialization_lock = threading.Lock()

    def backend():
        nonlocal service
        with initialization_lock:
            if service is None:
                service = PublicationService(PublicationStore(secret_key=app.secret_key), load_rows)
        return service

    def scope(write=False, admin=False):
        user = current_user() or {}
        if not user.get("id"):
            raise PermissionError("请重新登录后操作")
        is_admin = bool(user.get("is_platform_admin") or user.get("is_admin") or
                        user.get("role_key") in {"super_admin", "enterprise_admin", "admin"})
        if admin and not is_admin:
            raise PermissionError("只有管理员可以配置亚马逊店铺授权")
        if own_store_only(user):
            raise PermissionError("当前账号的店铺权限尚未分配到该平台，请管理员操作")
        if not is_admin and not has_permission(user, "tasks.execute" if write else "tasks.view"):
            raise PermissionError("当前账号没有发布任务操作权限")
        organization = str(user.get("organization_key") or "wuhan-zeshun")
        if organization == "default":
            organization = "wuhan-zeshun"
        if not re.fullmatch(r"[a-z0-9_.-]{2,64}", organization):
            raise PermissionError("当前账号企业标识无效")
        return str(user["id"]), organization

    def payload():
        if request.content_length and request.content_length > 2_000_000:
            raise ValueError("发布资料不能超过 2 MB")
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("请求必须为 JSON 对象")
        return value

    def execute(action, *, job_id=None):
        try:
            owner, organization = scope(write=request.method != "GET", admin=action == "amazon-store")
            manager = backend()
            if action == "stores":
                result = manager.stores(platform_id(request.args.get("target_platform")), organization)
            elif action == "amazon-store":
                result = manager.store.save_amazon_store(organization, payload())
            elif action == "schema":
                platform = platform_id(request.args.get("target_platform"))
                publisher, _ = manager.publisher_factory(platform, str(request.args.get("store_id") or ""), organization)
                result = publisher.get_schema(request.args.get("category_id") or "")
            elif action == "preview":
                result = manager.preview(payload(), owner, organization)
            elif action == "submit":
                result = manager.submit(str(payload().get("preview_id") or ""), owner, organization)
            elif action == "jobs":
                platform = platform_id(request.args.get("target_platform"))
                result = {"rows": [manager.get_job(job["id"], owner, organization)
                                   for job in manager.store.jobs(owner, organization, platform)]}
            elif action == "job":
                result = manager.get_job(job_id, owner, organization)
            elif action == "refresh":
                result = manager.refresh(job_id, owner, organization)
            elif action == "retry":
                result = manager.retry(job_id, owner, organization)
            else:
                raise ValueError("不支持的发布操作")
            response = jsonify(status="success", data=result)
            response.headers["Cache-Control"] = "no-store"
            return response, 202 if action in {"submit", "retry"} else 200
        except PermissionError as exc:
            return jsonify(status="error", message=_public_error_message(exc)), 403
        except KeyError:
            return jsonify(status="error", message="店铺、预览或任务不存在"), 404
        except (ValueError, TypeError) as exc:
            return jsonify(status="error", message=_public_error_message(exc)), 400
        except Exception as exc:
            # Exceptions from SDKs may contain request credentials. Do not log
            # their repr/traceback or forward it into an HTTP response.
            app.logger.warning("AI 原创发布服务失败 action=%s type=%s", action, type(exc).__name__)
            return jsonify(status="error", message="发布服务暂时不可用，请检查授权配置或回查发布记录"), 502

    @app.get("/api/ai-original-products/publish/stores")
    @login_required
    def api_original_publish_stores(): return execute("stores")

    @app.post("/api/ai-original-products/publish/amazon-store")
    @login_required
    def api_original_amazon_store(): return execute("amazon-store")

    @app.get("/api/ai-original-products/publish/schema")
    @login_required
    def api_original_publish_schema(): return execute("schema")

    @app.post("/api/ai-original-products/publish/preview")
    @login_required
    def api_original_publish_preview(): return execute("preview")

    @app.post("/api/ai-original-products/publish")
    @login_required
    def api_original_publish_submit(): return execute("submit")

    @app.get("/api/ai-original-products/publish/jobs")
    @login_required
    def api_original_publish_jobs(): return execute("jobs")

    @app.get("/api/ai-original-products/publish/jobs/<job_id>")
    @login_required
    def api_original_publish_job(job_id): return execute("job", job_id=job_id)

    @app.post("/api/ai-original-products/publish/jobs/<job_id>/refresh")
    @login_required
    def api_original_publish_refresh(job_id): return execute("refresh", job_id=job_id)

    @app.post("/api/ai-original-products/publish/jobs/<job_id>/retry")
    @login_required
    def api_original_publish_retry(job_id): return execute("retry", job_id=job_id)
