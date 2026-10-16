"""Read saved catalog facts, fetch category schemas, then explicitly edit attributes."""
import re
from flask import request, jsonify
from bit import bit_db_api, product_access
from erp.catalog_attributes import KINDS, facts, inspect


def register_routes(app, *, login_required, internal_required, reject_client_mode,
                    current_user, has_permission, filtered_tokens, publishing):
    def selection(data):
        kind = data.get('kind')
        if kind not in KINDS:
            raise ValueError('不支持的产品列表')
        raw = data.get('ids')
        if not isinstance(raw, list) or not 1 <= len(raw) <= 100 or any(isinstance(v, bool) or not str(v).isdigit() or int(v) <= 0 for v in raw):
            raise ValueError('每批请选择 1 至 100 件有效产品')
        return kind, list(dict.fromkeys(int(v) for v in raw))

    def load(kind, ids, write=False):
        user = current_user()
        permission = ('ai_content.' if kind == 'original' else 'catalog.') + ('execute' if write else 'view')
        if not has_permission(user, permission):
            raise PermissionError('当前账号没有此项产品操作权限')
        bit_db_api.check_catalog_access('collection' if kind == 'collection' else 'products', ids, operate=write)
        rows = (bit_db_api.get_mercado_collection_items_by_ids(ids) if kind == 'collection'
                else bit_db_api.get_mercado_product_items_by_ids(ids))
        if len(rows) != len(ids) or any(not product_access.can_view(row, user) for row in rows):
            raise PermissionError('当前账号无权查看所选产品')
        if kind in {'supplier', 'original'} and any(row.get('source_type') != 'ai_original' for row in rows):
            raise ValueError('所选产品不属于当前列表')
        for row in rows:
            row['can_operate'] = product_access.can_operate(row, user)
        return rows

    def schemas_for(rows, kind):
        from erp.mercadolibre_batch_publish import DatabaseMercadoLibreClient
        from erp.mercadolibre_follow_sell import _category_attribute_schema
        from erp.ai_original_attribute_labels import localize_attribute_labels
        schemas, clients = {}, {}
        tokens = None
        for row in rows:
            category = facts(row, kind)['category_id']
            if category in schemas:
                continue
            schemas[category] = None
            if not re.fullmatch(r'(?:ML[A-Z]|MCO|CBT)\d+', category):
                continue
            try:
                if tokens is None:
                    tokens = [t for t in filtered_tokens().get('rows', []) if t.get('enabled', True)]
                candidates = sorted(tokens, key=lambda t: str(t.get('site_id') or '').upper() != category[:3])
                if not candidates:
                    continue
                token_id = int(candidates[0]['id'])
                if token_id not in clients:
                    clients[token_id] = DatabaseMercadoLibreClient(token_id, timeout=8)
                schema = _category_attribute_schema(clients[token_id], category)
                if isinstance(schema, list):
                    from copy import deepcopy
                    schema = deepcopy(schema)
                    localize_attribute_labels(schema)
                    schemas[category] = schema
            except Exception:
                # A failed rules lookup must never mark a product complete or invent defaults.
                continue
        return schemas

    def error(exc):
        code = 403 if isinstance(exc, (PermissionError, bit_db_api.DatabaseAPIAuthError)) else 400
        return jsonify(status='error', message=str(exc)), code

    @app.route('/api/catalog/attributes/check', methods=['GET'])
    @login_required
    def api_catalog_attributes_check():
        try:
            kind, ids = selection({'kind': request.args.get('kind'), 'ids': (request.args.get('ids') or '').split(',')})
            rows = load(kind, ids)
            schemas = schemas_for(rows, kind)
            response = jsonify(status='success', data={'results': [inspect(row, kind, schemas.get(facts(row, kind)['category_id'])) for row in rows]})
            response.headers['Cache-Control'] = 'no-store'
            return response
        except (ValueError, PermissionError, bit_db_api.DatabaseAPIAuthError) as exc:
            return error(exc)

    @app.route('/api/catalog/attributes', methods=['PATCH'])
    @login_required
    def api_catalog_attributes_update():
        try:
            if publishing():
                return jsonify(status='error', message='批量上架正在运行，完成后再修改属性'), 409
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                raise ValueError('属性参数必须是对象')
            kind, ids = selection(data)
            operation = {key: data[key] for key in ('mode', 'category_id', 'attribute_id', 'value', 'only_missing') if key in data}
            if operation.get('mode') not in {'generic', 'set'}:
                raise ValueError('不支持的属性操作')
            if 'only_missing' in operation and not isinstance(operation['only_missing'], bool):
                raise ValueError('仅补空值参数必须为布尔值')
            rows = load(kind, ids, True)
            schemas = schemas_for(rows, kind)
            result = bit_db_api.update_catalog_attributes(kind, ids, schemas, operation)
            return jsonify(status='success', data=result)
        except (ValueError, PermissionError, bit_db_api.DatabaseAPIAuthError) as exc:
            return error(exc)

    @app.route('/api/db/catalog/attributes', methods=['POST'])
    @internal_required
    def api_db_catalog_attributes_update():
        blocked = reject_client_mode()
        if blocked:
            return blocked
        try:
            from erp.mercadolibre_collection_store import update_catalog_attributes
            data = request.get_json(silent=True) or {}
            kind, ids = selection({'kind': data.get('kind'), 'ids': data.get('item_ids')})
            return jsonify(status='success', data=update_catalog_attributes(kind, ids, data.get('schemas'), data.get('operation')))
        except (ValueError, PermissionError) as exc:
            return error(exc)
