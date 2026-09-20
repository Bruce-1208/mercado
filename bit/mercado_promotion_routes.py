"""Flask routes for the store-group promotion workbench."""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from bit.mercado_promotions import (
    create_preview,
    execute_preview,
    sync_promotions,
    visible_store_options,
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
                scope_pairs=scope_pairs,
                # When current_scope_pairs is active, the local snapshot's
                # salesperson/group/site values are deliberately ignored.
                salesperson="" if scope_pairs is not None else salesperson,
                group_name="" if scope_pairs is not None else group_name,
                site_id="" if scope_pairs is not None else site_id,
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
            promotion = store.get_promotion(promotion_id)
            if not promotion:
                return fail("活动不存在", 404)
            allowed = allowed_ids()
            if allowed is not None and int(promotion.get("token_id") or 0) not in allowed:
                return fail("当前账号不能查看该店铺", 403)
            return ok({"promotion": promotion, "rows": store.list_items(promotion_id)})
        except Exception as exc:
            return fail(exc, 500)

    @blueprint.route("/api/promotions/preview", methods=["POST"])
    @login_required
    def preview():
        try:
            body = request.get_json(silent=True) or {}
            promotion_id = int(body.get("promotion_id") or 0)
            promotion = PromotionStore().get_promotion(promotion_id)
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
            )
            return ok(result)
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
                return ok({"rows": store.list_jobs()})
            body = request.get_json(silent=True) or {}
            if body.get("confirmed") is not True:
                return fail("请确认已核对活动、商品和价格后再提交")
            return ok(execute_preview(str(body.get("preview_id") or ""), actor=actor(), store=store))
        except ValueError as exc:
            return fail(exc)
        except Exception as exc:
            return fail(exc, 500)

    return blueprint
