"""Read the complete listing for a product belonging to an accessible order."""
import copy
import json
import re
import threading
import time
from html.parser import HTMLParser

from erp.mercadolibre_translation import ListingTranslationError, translate_texts

from bit import bit_mysql, mercado_tokens
from bit.bit_order_labels import _is_invalid_token_error
from mercado_api.client import MercadoLibreClient, MercadoAPIError


# An interactive read must not inherit the API client's four 30-second attempts.
_DETAIL_HTTP_TIMEOUT = (3, 8)
_DETAIL_CACHE_TTL = 300
_DETAIL_CACHE_LIMIT = 256
_DETAIL_CACHE = {}
_DETAIL_CACHE_LOCK = threading.Lock()


def _refresh_store_token(token_id):
    mercado_tokens.refresh_and_save(
        int(token_id),
        get_token=bit_mysql.get_mercado_store_token,
        update_token=bit_mysql.update_mercado_store_token,
        record_error=bit_mysql.record_mercado_store_token_error,
        timeout=8,
        lock_timeout=2,
    )
    return bit_mysql.get_mercado_store_token(int(token_id))


def _cached_detail(key, *, refresh=False):
    with _DETAIL_CACHE_LOCK:
        cached = _DETAIL_CACHE.get(key)
        if refresh or (cached and time.monotonic() - cached[0] >= cached[1]):
            _DETAIL_CACHE.pop(key, None)
            return None
        return copy.deepcopy(cached[2]) if cached else None


def _cache_detail(key, detail):
    # Retry a temporarily missing description sooner; normal listings can be
    # reused by the separate Chinese-translation request and repeated opens.
    ttl = 10 if detail.get('warnings') else _DETAIL_CACHE_TTL
    with _DETAIL_CACHE_LOCK:
        _DETAIL_CACHE[key] = (time.monotonic(), ttl, copy.deepcopy(detail))
        while len(_DETAIL_CACHE) > _DETAIL_CACHE_LIMIT:
            oldest = min(_DETAIL_CACHE, key=lambda entry: _DETAIL_CACHE[entry][0])
            _DETAIL_CACHE.pop(oldest, None)


def _http_status(error):
    match = re.search(r'失败\s*\((\d{3})\)', str(error))
    return int(match.group(1)) if match else None


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


def get_order_product_detail(order_ids, product_id, allowed_token_ids=None, *,
                             include_translation=False, force_refresh=False, include_description=True):
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
        products = {str(candidate.get('product_id') or '').upper()}
        order_items = raw.get('order_items') or []
        if isinstance(order_items, list):
            products.update(str((entry.get('item') or {}).get('id') or '').upper()
                            for entry in order_items if isinstance(entry, dict)
                            and isinstance(entry.get('item') or {}, dict))
        if product_id in products:
            context = candidate
            break
    if context is None:
        raise ValueError('商品不属于所选订单或无权查看')
    # Always authorize against the selected order before reading the cache.
    # Token IDs separate listings belonging to different stores.
    cache_key = (int(context['token_id']), product_id)
    detail = _cached_detail(cache_key, refresh=bool(force_refresh))
    include_description = bool(include_description or include_translation)
    if detail is None or (include_description and detail.get("description_pending")):
        client = MercadoLibreClient(context['access_token'], timeout=_DETAIL_HTTP_TIMEOUT,
                                    admission_timeout=2)
        refreshed = False

        def read(path, **kwargs):
            nonlocal client, refreshed
            try:
                return client.request('GET', path, max_attempts=1, **kwargs)
            except MercadoAPIError as exc:
                # The API client handles a 401 first and reports missing OAuth
                # credentials because this client stores them centrally. This
                # message must also trigger the persisted store-token refresh.
                invalid_token = (_is_invalid_token_error(exc)
                                 or 'access token 已失效' in str(exc).lower())
                if refreshed or not invalid_token or not context.get('refresh_token'):
                    raise
                refreshed = True
                token = _refresh_store_token(context['token_id'])
                client.session.close()
                client = MercadoLibreClient(token['access_token'], timeout=_DETAIL_HTTP_TIMEOUT,
                                            admission_timeout=2)
                return client.request('GET', path, max_attempts=1, **kwargs)

        try:
            item = detail['item'] if detail else None
            if item is None:
                params = {'include_attributes': 'all', 'include_internal_attributes': 'true'}
                local_path = f'/items/{product_id}'
                marketplace_path = f'/marketplace/items/{product_id}'
                # Global Selling authorizations also own localized MLA/MLB/etc.
                # listings. Their item prefixes do not identify the token's API
                # scope: /items can reject these with 403 while /marketplace/items
                # serves the same order and token successfully.
                primary, fallback = marketplace_path, local_path
                try:
                    item = read(primary, params=params)
                except MercadoAPIError as exc:
                    # A network failure or rate limit affects both paths. Fall back
                    # only when the primary endpoint cannot serve this listing.
                    if _http_status(exc) not in {404, 405}:
                        raise
                    item = read(fallback, params=params)
                if not isinstance(item, dict) or not item.get('id'):
                    raise ValueError('平台未返回完整商品资料，请稍后重试')
            warnings = []
            description = {}
            if include_description:
                try:
                    description = read(f'/items/{product_id}/description') or {}
                    if not isinstance(description, dict):
                        description = {}
                        warnings.append('商品描述暂时读取失败，请重试')
                except (MercadoAPIError, ValueError) as exc:
                    description = {}
                    # A listing without a description is a valid product detail.
                    if _http_status(exc) != 404:
                        warnings.append('商品描述暂时读取失败，请重试')
            detail = {'item': item, 'description': description, 'warnings': warnings,
                      'description_pending': not include_description}
            _cache_detail(cache_key, detail)
        finally:
            client.session.close()

    detail.setdefault('translations_zh', {})
    detail['translation_error'] = ''
    if include_translation and not detail.get('translations_ready'):
        try:
            detail['translations_zh'] = translate_product_detail(detail['item'], detail['description'])
            detail['translations_ready'] = True
            _cache_detail(cache_key, detail)
        except ListingTranslationError as exc:
            detail['translation_error'] = f'中文翻译暂时不可用，已保留原文。{exc}'
        except Exception:
            detail['translation_error'] = '中文翻译暂时不可用，已保留原文；请检查本地翻译模型配置后重试'
        if detail['translation_error']:
            detail['warnings'].append(detail['translation_error'])
    return detail
