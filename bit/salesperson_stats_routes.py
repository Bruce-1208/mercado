"""Workbench and central database endpoints for salesperson activity."""
from flask import jsonify, request
from bit import bit_db_api


def register_routes(app, *, login_required, internal_api_required, authorized_token_ids,
                    current_user, has_permission, reject_client_mode):
    def respond(scope):
        try:
            data = bit_db_api.list_salesperson_stats(
                request.args.get('start_date', ''), request.args.get('end_date', ''),
                allowed_token_ids=scope)
        except (ValueError, TypeError, OverflowError) as exc:
            return jsonify(status='error', message=str(exc)), 400
        except Exception:
            app.logger.exception('业务员量统计加载失败')
            return jsonify(status='error', message='业务员量统计加载失败，请稍后重试'), 502
        response = jsonify(status='success', data=data)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/salesperson-stats')
    @login_required
    def salesperson_stats():
        if not has_permission(current_user() or {}, 'salesperson_stats.view'):
            return jsonify(status='error', message='无权查看业务员量统计'), 403
        return respond(authorized_token_ids())

    @app.get('/api/db/salesperson-stats')
    @internal_api_required
    def db_salesperson_stats():
        blocked = reject_client_mode()
        if blocked:
            return blocked
        try:
            scope = ({int(v) for v in request.args.getlist('allowed_token_id')}
                     if request.args.get('scope_limited') == '1' else None)
        except ValueError:
            return jsonify(status='error', message='店铺范围无效'), 400
        return respond(scope)
