"""Flask routes for the store-group promotion workbench."""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from bit.mercado_promotions import (
    create_preview,
    enrich_item_products,
    execute_preview,
    recover_stale_running_jobs,
    start_enrollment,
    sync_promotions,
    stop_campaign,
    visible_store_options,
    withdraw_listing_promotions,
)
from erp.mercadolibre_promotion_store import PromotionStore


def create_promotions_blueprint(
    *, login_required, current_user, authorized_token_ids, has_permission=None
):
    blueprint = Blueprint("promotions", __name__)

    def actor() -> str:
        user = current_user() or {}
        return str(user.get("username") or user.get("display_name") or user.get("id") or "unknown")

    def allowed_ids():
        allowed = authorized_token_ids(current_user())
        return None if allowed is None else {int(value) for value in allowed}

    def current_scope_pairs(token_ids, *, salesperson="", group_name="", site_id=""):
        """Resolve filters against current store settings, not old sync snapshots."""
        salesperson = str(salesperson or "").strip()
        group_name = str(group_name or "").strip()
        site_id = str(site_id or "").strip().upper()
        if not any((salesperson, group_name, site_id)):
            return None
        # An empty token list means "all stores" for the unrestricted view;
        # keep that behavior while resolving the current settings.
        selected = {int(value) for value in token_ids or ()} or None
        pairs = []
        for store in visible_store_options(selected):
            token_id = int(store.get("id") or 0)
            if selected is not None and token_id not in selected:
                continue
            for setting in store.get("sites") or ():
                current_site_id = str(setting.get("site_id") or "").strip().upper()
                if not current_site_id:
                    continue
                if site_id and current_site_id != site_id:
                    continue
                if salesperson and str(setting.get("salesperson") or "").strip() != salesperson:
                    continue
                if group_name and str(setting.get("group_name") or "").strip() != group_name:
                    continue
                pairs.append((token_id, current_site_id))
        return pairs

    def ok(data=None, message=""):
        return jsonify({"status": "success", "message": message, "data": data})

    def fail(exc, status=400):
        return jsonify({"status": "error", "message": str(exc)}), status

    def positive_body_id(value, label="活动编号"):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{label}无效")
        return value

    @blueprint.route("/promotions")
    @login_required
    def page():
        if has_permission and not has_permission(current_user(), "promotions.view"):
            return "当前账号没有活动管理查看权限", 403
        return render_template(
            "mercado_promotions.html",
            current_user=current_user() or {},
            embedded=str(request.args.get("embedded") or "").lower() in {"1", "true", "yes"},
        )

    @blueprint.route("/api/promotions/stores", methods=["GET"])
    @login_required
    def stores():
        try:
            return ok(visible_store_options(allowed_ids()))
        except Exception as exc:
            return fail(exc, 500)

    @blueprint.route("/api/promotions", methods=["GET"])
    @login_required
    def list_promotions():
        try:
            allowed = allowed_ids()
            if allowed is not None and not allowed:
                return ok({"rows": [], "summary": {"total": 0, "candidate_items": 0, "active_items": 0, "ending_soon": 0}})
            requested = [int(value) for value in request.args.getlist("token_id") if str(value).isdigit()]
            token_ids = requested or (sorted(allowed) if allowed is not None else [])
            if allowed is not None and not set(token_ids).issubset(allowed):
                return fail("当前账号不能查看所选店铺", 403)
            salesperson = str(request.args.get("salesperson") or "").strip()
            group_name = str(request.args.get("group_name") or "").strip()
            site_id = str(request.args.get("site_id") or "").strip()
            scope_pairs = current_scope_pairs(
                token_ids,
                salesperson=salesperson,
                group_name=group_name,
                site_id=site_id,
            )
            rows = PromotionStore().list_promotions(
                token_ids=token_ids,
                search=str(request.args.get("search") or "").strip(),
                status=str(request.args.get("status") or "").strip(),
                promotion_type=str(request.args.get("type") or "").strip(),
                participated=str(request.args.get("participated") or "").strip(),
                scope_pairs=scope_pairs,
                # When current_scope_pairs is active, the local snapshot's
                # salesperson/group/site values are deliberately ignored.
                salesperson="" if scope_pairs is not None else salesperson,
                group_name="" if scope_pairs is not None else group_name,
                site_id="" if scope_pairs is not None else site_id,
                current_participation=True,
                include_operation_summary=False,
            )
            summary = {
                "total": len(rows),
                "candidate_items": sum(int(row.get("candidate_count") or 0) for row in rows),
                "active_items": sum(int(row.get("active_item_count") or 0) for row in rows),
                "ending_soon": sum(1 for row in rows if row.get("deadline_date") and str(row.get("status_raw")) != "finished"),
            }
            return ok({"rows": rows, "summary": summary})
        except Exception as exc:
            return fail(exc, 500)

    @blueprint.route("/api/promotions/sync", methods=["POST"])
    @login_required
    def sync():
        try:
            body = request.get_json(silent=True) or {}
            token_ids = [int(value) for value in body.get("token_ids") or []]
            allowed = allowed_ids()
            if allowed is not None and not set(token_ids).issubset(allowed):
                return fail("当前账号不能同步所选店铺", 403)
            return ok(sync_promotions(token_ids), "活动同步完成")
        except ValueError as exc:
            return fail(exc)
        except Exception as exc:
            return fail(exc, 500)

    @blueprint.route("/api/promotions/<int:promotion_id>/items", methods=["GET"])
    @login_required
    def items(promotion_id):
        try:
            store = PromotionStore()
            selection_only = request.args.get("selection") == "1"
            promotion = store.get_promotion(promotion_id, include_summary=False)
            if not promotion:
                return fail("活动不存在", 404)
            allowed = allowed_ids()
            if allowed is not None and int(promotion.get("token_id") or 0) not in allowed:
                return fail("当前账号不能查看该店铺", 403)
            if not selection_only:
                # Deep links can load item details before the page-level running
                # job restore. Heal orphaned jobs here so latest_job cannot keep
                # the campaign disabled after its worker has disappeared.
                recover_stale_running_jobs(
                    store=store, token_ids=allowed, promotion_fks=[promotion_id],
                    include_terminal=False,
                )
                promotion = store.get_promotion(promotion_id)
            page_data = store.list_items_page(
                promotion_id,
                selection_only=selection_only,
                candidate_only=selection_only and request.args.get("candidate_only") == "1",
                page=request.args.get("page", 1),
                page_size=request.args.get("page_size", 50),
                target_item_id=str(request.args.get("target_item_id") or "").strip(),
                enrolled=str(request.args.get("enrolled") or "").strip(),
                product=str(request.args.get("product") or "").strip(),
                item_id=str(request.args.get("item_id") or "").strip(),
            )
            if selection_only:
                # Full selection does not display media, net quotes or job history.
                fields = {"id", "item_id", "offer_id", "status_raw", "price", "original_price", "min_price", "max_price", "currency_id"}
                return ok({**page_data, "rows": [{k: v for k, v in row.items() if k in fields} for row in page_data["rows"]]})
            results = store.enrollment_results(promotion_id, limit=50)
            results_total = int((promotion.get("enrollment_summary") or {}).get("total") or 0)
            return ok({
                **page_data,
                "promotion": promotion,
                "rows": enrich_item_products(promotion, page_data["rows"]),
                "enrollment_results": results,
                "enrollment_results_total": results_total,
                "enrollment_results_truncated": results_total > len(results),
                "latest_job": store.latest_job(promotion_id, result_limit=50),
            })
        except Exception as exc:
            return fail(exc, 500)

    @blueprint.route("/api/promotions/<int:promotion_id>/withdraw", methods=["POST"])
    @login_required
    def withdraw_listing(promotion_id):
        try:
            if has_permission and not has_permission(current_user(), "promotions.execute"):
                return fail("当前账号没有取消活动权限", 403)
            store = PromotionStore()
            promotion = store.get_promotion(promotion_id)
            if not promotion:
                return fail("活动不存在", 404)
            allowed = allowed_ids()
            if allowed is not None and int(promotion["token_id"]) not in allowed:
                return fail("当前账号不能操作该店铺", 403)
            body = request.get_json(silent=True) or {}
            result = withdraw_listing_promotions(token_id=promotion["token_id"], site_id=promotion["site_id"],
                item_id=str(body.get("item_id") or "").strip(), promotion_fk=promotion_id, actor=actor(), store=store)
            return ok(result, "活动已取消")
        except ValueError as exc:
            return fail(exc)
        except Exception as exc:
            return fail(exc, 500)

    @blueprint.route("/api/promotions/stop", methods=["POST"])
    @login_required
    def stop_promotions():
        try:
            if has_permission and not has_permission(current_user(), "promotions.execute"):
                return fail("当前账号没有停止活动权限", 403)
            body = request.get_json(silent=True) or {}
            ids = body.get("promotion_ids")
            if not isinstance(ids, list) or not ids or len(ids) > 50:
                return fail("请选择 1 至 50 个活动")
            if any(type(value) is not int or value <= 0 for value in ids):
                return fail("活动编号无效")
            if body.get("confirmed") is not True:
                return fail("请确认停止所选活动下所有已报名商品")
            store, allowed = PromotionStore(), allowed_ids()
            promotions = []
            # Validate the entire authorization scope before starting any mutation.
            for pk in dict.fromkeys(ids):
                promotion = store.get_promotion(pk)
                if not promotion:
                    return fail("活动不存在", 404)
                if allowed is not None and int(promotion["token_id"]) not in allowed:
                    return fail("当前账号不能操作所选店铺", 403)
                promotions.append(promotion)
            results = []
            for promotion in promotions:
                try:
                    result = stop_campaign(promotion_fk=promotion["id"], actor=actor(), store=store)
                except Exception as exc:
                    result = {"status": "failed", "message": str(exc)}
                results.append({**result, "promotion_id": promotion["id"], "name": promotion["name"]})
            return ok({"results": results})
        except (TypeError, ValueError) as exc:
            return fail(exc)
        except Exception as exc:
            return fail(exc, 500)

    @blueprint.route("/api/promotions/preview", methods=["POST"])
    @login_required
    def preview():
        try:
            if has_permission and not has_permission(current_user(), "promotions.execute"):
                return fail("当前账号没有活动操作权限", 403)
            body = request.get_json(silent=True) or {}
            promotion_id = positive_body_id(body.get("promotion_id"))
            store = PromotionStore()
            promotion = store.get_promotion(promotion_id, include_summary=False)
            if not promotion:
                return fail("活动不存在", 404)
            allowed = allowed_ids()
            if allowed is not None and int(promotion.get("token_id") or 0) not in allowed:
                return fail("当前账号不能操作该店铺", 403)
            result = create_preview(
                promotion_fk=promotion_id,
                action=str(body.get("action") or "enroll"),
                rows=list(body.get("rows") or []),
                actor=actor(),
                store=store,
                promotion=promotion,
            )
            return ok(result)
        except (TypeError, ValueError) as exc:
            return fail(exc)
        except Exception as exc:
            return fail(exc, 500)

    @blueprint.route("/api/promotions/enroll", methods=["POST"])
    @login_required
    def enroll():
        try:
            if has_permission and not has_permission(current_user(), "promotions.execute"):
                return fail("当前账号没有活动报名权限", 403)
            body = request.get_json(silent=True) or {}
            promotion_id = positive_body_id(body.get("promotion_id"))
            rows = body.get("rows") or []
            if not isinstance(rows, list):
                return fail("报名商品格式无效")
            store = PromotionStore()
            promotion = store.get_promotion(promotion_id, include_summary=False)
            if not promotion:
                return fail("活动不存在", 404)
            allowed = allowed_ids()
            if allowed is not None and int(promotion.get("token_id") or 0) not in allowed:
                return fail("当前账号不能操作该店铺", 403)
            return ok(start_enrollment(
                promotion_fk=promotion_id,
                rows=rows,
                actor=actor(),
                store=store,
                promotion=promotion,
            ))
        except (TypeError, ValueError) as exc:
            return fail(exc)
        except Exception as exc:
            return fail(exc, 500)

    @blueprint.route("/api/promotions/jobs", methods=["GET", "POST"])
    @login_required
    def jobs():
        try:
            store = PromotionStore()
            if request.method == "GET":
                job_ids = request.args.getlist("job_id") or None
                if job_ids and (len(job_ids) > 50 or any(len(value) > 64 for value in job_ids)):
                    return fail("查询任务数量或编号无效")
                allowed = allowed_ids()
                action = str(request.args.get("action") or "")
                recover_stale_running_jobs(
                    store=store, token_ids=allowed, job_ids=job_ids, action=action,
                    include_terminal=str(request.args.get("status") or "") != "running",
                )
                response = ok({"rows": store.list_jobs(token_ids=allowed,
                    job_ids=job_ids, summary_only=request.args.get("summary") == "1",
                    result_limit=request.args.get("result_limit"),
                    action=action, status=str(request.args.get("status") or ""))})
                response.headers["Cache-Control"] = "no-store"
                return response
            if has_permission and not has_permission(current_user(), "promotions.execute"):
                return fail("当前账号没有活动操作权限", 403)
            body = request.get_json(silent=True) or {}
            if body.get("confirmed") is not True:
                return fail("请确认已核对活动、商品和价格后再提交")
            preview = store.get_preview(str(body.get("preview_id") or ""))
            if not preview:
                return fail("报名预览不存在", 404)
            promotion = store.get_promotion(int(preview.get("promotion_fk") or 0))
            if not promotion:
                return fail("活动不存在", 404)
            allowed = allowed_ids()
            if allowed is not None and int(promotion.get("token_id") or 0) not in allowed:
                return fail("当前账号不能操作该店铺", 403)
            return ok(execute_preview(str(body.get("preview_id") or ""), actor=actor(), store=store, background=True))
        except ValueError as exc:
            return fail(exc)
        except Exception as exc:
            return fail(exc, 500)

    return blueprint
