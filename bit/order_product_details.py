"""Read the complete listing for a product belonging to an accessible order."""
import json
import re
from html.parser import HTMLParser

from erp.mercadolibre_translation import ListingTranslationError, translate_texts

from bit import bit_mysql
from bit.bit_order_labels import _refresh_store_token, _is_invalid_token_error
from mercado_api.client import MercadoLibreClient, MercadoAPIError


class _DescriptionText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def translate_product_detail(item, description):
    """Return display-text translations without changing platform identifiers."""
    description_text = description.get('plain_text') or ''
    if not description_text and description.get('text'):
        parser = _DescriptionText()
        parser.feed(description['text'])
        description_text = ''.join(parser.parts)
    texts = [item.get('title'), description_text]
    attributes = list(item.get('attributes') or [])
    for variant in item.get('variations') or []:
        attributes.extend(variant.get('attribute_combinations') or [])
        attributes.extend(variant.get('attributes') or [])
    for attribute in attributes:
        texts.append(attribute.get('name'))
        if str(attribute.get('id') or '').upper() in {'BRAND', 'MODEL', 'MPN', 'GTIN', 'SELLER_SKU', 'SKU'}:
            continue
        value = attribute.get('value_name')
        if value is None and isinstance(attribute.get('values'), list):
            value = ' / '.join(str(entry.get('name') or entry.get('id') or '') for entry in attribute['values'])
        if value is not None:
            texts.append(str(value))
    pending = list(dict.fromkeys(str(text).strip() for text in texts if text and re.search(r'[A-Za-zÀ-ÿ]', str(text))))
    site = str(item.get('site_id') or item.get('id') or '')[:3].upper()
    source = 'pt-BR' if site == 'MLB' else 'en' if site == 'CBT' else 'es'
    translations = {}
    for offset in range(0, len(pending), 100):
        batch = pending[offset:offset + 100]
        translations.update(zip(batch, translate_texts(batch, source, 'zh-CN')))
    return translations


def get_order_product_detail(order_ids, product_id, allowed_token_ids=None):
    product_id = str(product_id or '').strip().upper()
    if not re.fullmatch(r'[A-Z]{3}\d+', product_id):
        raise ValueError('商品编号无效')
    if not isinstance(order_ids, list):
        raise ValueError('订单编号必须是数组')
    ids = list(dict.fromkeys(str(value).strip() for value in order_ids or [] if str(value).strip()))
    if not ids or len(ids) > 100:
        raise ValueError('请选择 1 至 100 个订单')
    allowed = None if allowed_token_ids is None else {int(value) for value in allowed_token_ids}
    connection = bit_mysql.pymysql.connect(**bit_mysql.config)
    try:
        with connection.cursor() as cursor:
            placeholders = ','.join(['%s'] * len(ids))
            cursor.execute(
                f'SELECT synced.order_id, synced.token_id, synced.product_id, synced.raw_json, '
                f'stores.access_token, stores.refresh_token FROM mercado_synced_orders AS synced '
                f'INNER JOIN mercado_store_tokens AS stores ON stores.id = synced.token_id '
                f'WHERE synced.order_id IN ({placeholders})', ids,
            )
            contexts = list(cursor.fetchall() or [])
    finally:
        connection.close()
    context = None
    for candidate in contexts:
        if allowed is not None and int(candidate['token_id']) not in allowed:
            continue
        raw = candidate.get('raw_json') or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        products = {str(candidate.get('product_id') or '')}
        products.update(str((entry.get('item') or {}).get('id') or '') for entry in raw.get('order_items', []))
        if product_id in products:
            context = candidate
            break
    if context is None:
        raise ValueError('商品不属于所选订单或无权查看')
    client = MercadoLibreClient(context['access_token'])

    def read(path, **kwargs):
        nonlocal client
        try:
            return client.request('GET', path, **kwargs)
        except MercadoAPIError as exc:
            if not _is_invalid_token_error(exc) or not context.get('refresh_token'):
                raise
            token = _refresh_store_token(context['token_id'])
            client = MercadoLibreClient(token['access_token'])
            return client.request('GET', path, **kwargs)

    params = {'include_attributes': 'all', 'include_internal_attributes': 'true'}
    try:
        item = read(f'/marketplace/items/{product_id}', params=params)
    except MercadoAPIError:
        item = read(f'/items/{product_id}', params=params)
    if not isinstance(item, dict) or not item.get('id'):
        raise ValueError('平台未返回完整商品资料，请稍后重试')
    warnings = []
    try:
        description = read(f'/items/{product_id}/description') or {}
    except MercadoAPIError:
        description = {}
        warnings.append('商品描述暂时读取失败，请重试')
    try:
        translations = translate_product_detail(item, description)
    except ListingTranslationError as exc:
        translations = {}
        warnings.append(f'中文翻译暂时不可用，已保留原文。{exc}')
    except Exception:
        translations = {}
        warnings.append('中文翻译暂时不可用，已保留原文；请检查本地翻译模型配置后刷新重试')
    return {'item': item, 'description': description, 'warnings': warnings, 'translations_zh': translations}
