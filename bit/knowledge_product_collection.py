"""Read infringement product feeds after an explicit, per-run login confirmation."""
from __future__ import annotations

import copy
import html
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SOURCES = {
    'zying': {'label': '智赢', 'url': 'https://meli.zying.net/#/goods/tort'},
    'lanjing': {'label': '蓝鲸选品', 'url': 'https://xp.lingdongsz.com/#/allItems/infringement'},
}
PAGE_SIZE = 50


class LoginRequired(RuntimeError):
    pass


class CollectionStopped(RuntimeError):
    pass


def clean(value):
    if value is None:
        return ''
    return ' '.join(html.unescape(re.sub(r'<[^>]*>', '', str(value))).split())


def pick(row, *keys):
    return next((row[key] for key in keys if row.get(key) is not None and row[key] != ''), None)


def safe_url(value):
    value = str(value or '').strip()
    if value.startswith('//'):
        value = 'https:' + value
    return value if urlsplit(value).scheme in ('https', 'http') else ''


def normalize_product(source, row, site=''):
    """Keep platform units and missing fields; zero sales/price are real values."""
    if not isinstance(row, dict):
        raise ValueError('平台返回的产品记录格式异常')
    product_id = clean(pick(row, 'Sku', 'id', 'item_id', 'sku'))
    title = clean(pick(row, 'Title', 'title', 'name'))
    if not product_id or not title:
        raise ValueError('平台返回的产品缺少编号或名称，请检查页面是否已改版')
    path = pick(row, 'path_from_root', 'CateFullName', 'CategoryPath', 'category_path') or []
    if isinstance(path, list):
        if (len(path) > 1 and isinstance(path[0], dict)
                and clean(path[0].get('id')) == clean(row.get('category_id'))):
            path = list(reversed(path))
        category_path = ' > '.join(clean(item.get('name') if isinstance(item, dict) else item) for item in path)
        leaf = clean(path[-1].get('name') if isinstance(path[-1], dict) else path[-1]) if path else ''
    else:
        category_path = clean(path)
        leaf = re.split(r'\s*[/>]\s*', category_path)[-1]
    brand = pick(row, 'Brand', 'brand_id', 'brand', 'brand_name', 'bland')
    if isinstance(brand, list):
        brand = next((value for value in brand if value), None)
    if isinstance(brand, dict):
        brand = pick(brand, 'name', 'value', 'text')
    if not brand and isinstance(row.get('attributes'), list):
        brand = next((item.get('value_name') for item in row['attributes']
                      if isinstance(item, dict) and item.get('id') == 'BRAND'), None)
    images = pick(row, 'Images', 'images', 'pictures') or []
    if isinstance(images, str):
        images = [images]
    images = [safe_url(pick(item, 'secure_url', 'url', 'src') if isinstance(item, dict) else item)
              for item in images] if isinstance(images, list) else []
    image = safe_url(pick(row, 'Thumb', 'pic_big', 'pic_small', 'thumbnail', 'main_image_url'))
    images = list(dict.fromkeys(value for value in [image, *images] if value))
    listed_at = pick(row, 'Uptime', 'UpTime', 'start_time', 'date_created')
    if isinstance(listed_at, (int, float)):
        listed_at = datetime.fromtimestamp(listed_at / (1000 if listed_at > 10**11 else 1), timezone.utc).date().isoformat()
    resolved_site = clean(site or pick(row, 'Site', 'site_id', 'siteId'))
    currencies = {'1': 'MXN', '2': 'BRL', '3': 'CLP', '4': 'COP', '5': 'ARS',
                  'MLM': 'MXN', 'MLB': 'BRL', 'MLC': 'CLP', 'MCO': 'COP', 'MLA': 'ARS'}
    return {
        'source': source, 'source_name': SOURCES[source]['label'], 'product_id': product_id,
        'title': title, 'main_image_url': images[0] if images else '', 'image_urls': images,
        'sales': pick(row, 'Orders', 'sold_his', 'sold_quantity', 'sales'),
        'sales_30d': pick(row, 'orders_30', 'sold_30', 'sales30', 'sold_30d'),
        'price': pick(row, 'Price', 'price'), 'currency': clean(pick(row, 'Currency', 'currency_id', 'currency')) or currencies.get(resolved_site, ''),
        'brand': clean(brand), 'listed_at': clean(listed_at),
        'subcategory': clean(pick(row, 'CateName', 'CategoryName', 'category_name', 'subcategory')) or leaf,
        'category_path': category_path, 'category_id': clean(pick(row, 'Cate', 'category_id')),
        'site': resolved_site,
        'product_url': safe_url(pick(row, 'Url', 'permalink', 'url')),
        'seller': clean(pick(row, 'Sellername', 'seller_name', 'seller_id')),
        'storage_mode': '自发货' if source == 'zying' else clean(pick(row, 'shipping_html_type', 'logistic_type')),
        'status': clean(pick(row, 'Status', 'item_status', 'status')),
        'stock': pick(row, 'Stock', 'available_quantity'), 'reviews': pick(row, 'Comment', 'reviews'),
        'rating': pick(row, 'Rate', 'rating'),
        'detail_error': clean(row.get('_detail_error')),
        'collected_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }


class ProductStore:
    @contextmanager
    def _connect(self):
        path = Path(os.environ.get('KNOWLEDGE_PRODUCT_COLLECTION_DB',
                                   Path(__file__).resolve().parents[1] / '.data' / 'knowledge-products.sqlite3'))
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30)
        connection.execute('''CREATE TABLE IF NOT EXISTS products (
            organization TEXT NOT NULL, source TEXT NOT NULL, site TEXT NOT NULL,
            product_id TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY (organization, source, site, product_id))''')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, organization, products):
        with self._connect() as connection:
            connection.executemany('INSERT OR REPLACE INTO products VALUES (?, ?, ?, ?, ?)', [
                (organization, item['source'], item['site'], item['product_id'], json.dumps(item, ensure_ascii=False))
                for item in products])

    def listing(self, organization, source='', search='', page=1, page_size=50):
        page, page_size = max(1, int(page)), min(100, max(1, int(page_size)))
        clauses, params = ['organization = ?'], [organization]
        if source:
            validate_source(source)
            clauses.append('source = ?'); params.append(source)
        if search:
            clauses.append('payload LIKE ? ESCAPE \'\\\'')
            params.append('%' + str(search).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        where = ' AND '.join(clauses)
        with self._connect() as connection:
            total = connection.execute('SELECT COUNT(*) FROM products WHERE ' + where, params).fetchone()[0]
            rows = connection.execute('SELECT payload FROM products WHERE ' + where +
                                      ' ORDER BY source, site, product_id LIMIT ? OFFSET ?',
                                      [*params, page_size, (page - 1) * page_size]).fetchall()
        return {'rows': [json.loads(row[0]) for row in rows], 'total': total, 'page': page,
                'page_size': page_size, 'pages': max(1, (total + page_size - 1) // page_size)}


def validate_source(source):
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError('请选择智赢或蓝鲸选品')
    return source


# The official request clients keep authentication inside the selected browser.
# Discover loaded module URLs at runtime; dated/hash filenames are never pinned.
DISCOVER_ZYING = r"""async () => {
    const urls = [...new Set(performance.getEntriesByType('resource').map(r => r.name))]
        .filter(url => new URL(url).origin === location.origin && url.endsWith('.js'));
    for (const url of urls) {
        const source = await (await fetch(url)).text();
        if (!source.includes('getMeliItemsByPage')) continue;
        const module = await import(url);
        const api = Object.values(module).find(value => value && typeof value.getMeliItemsByPage === 'function');
        if (api) { window.__knowledgeZyingApi = api; return true; }
    }
    return false;
}"""
ZYING_LIST = r"""async page => {
    let timer;
    try {
        const response = await Promise.race([
            window.__knowledgeZyingApi.getMeliItemsByPage({Status: 8, StockType: 1, page, pagesize: 50}),
            new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('智赢列表读取超时')), 30000); })
        ]);
        return {response, site: localStorage.getItem('meliSiteikey') || '1'};
    } finally { clearTimeout(timer); }
}"""
# Blue Whale's XpTable imports its POST client from the entry module. Resolve
# the export binding from the current table source rather than a fixed alias.
DISCOVER_LANJING = r"""async () => {
    const urls = [...new Set(performance.getEntriesByType('resource').map(r => r.name))]
        .filter(url => new URL(url).origin === location.origin && url.endsWith('.js'));
    for (const url of urls) {
        const source = await (await fetch(url)).text();
        if (!source.includes('resDataMap') || !source.includes('pageNo')) continue;
        const call = source.match(/([\w$]+)\(\{'url':[^,]+,'siteId':/);
        if (!call) continue;
        const imports = [...source.matchAll(/import\{([^}]+)\}from['"]([^'"]+)['"]/g)];
        for (const entry of imports) {
            const binding = entry[1].split(',').map(v => v.trim().split(/\s+as\s+/))
                .find(parts => parts[parts.length - 1] === call[1]);
            if (!binding) continue;
            const module = await import(new URL(entry[2], url).href);
            const request = module[binding[0]];
            // The table request is read-only; never expose the general client.
            if (typeof request !== 'function') continue;
            window.__knowledgeLanjingList = async ({page, site}) => request({
                url: '/web/items/search', siteId: site, storeStatusVol: 'under_review',
                sortKey: 'sold_his', sortOrder: 'desc', pageNo: page, pageSize: 50
            }, AbortSignal.timeout(30000));
            window.__knowledgeLanjingDetail = async ({id, site, productId}) => request({
                url: '/web/items/findOne', itemId: id, siteId: site, productId
            }, AbortSignal.timeout(30000));
            return true;
        }
    }
    return false;
}"""
LANJING_LIST = r"""async params => await window.__knowledgeLanjingList(params)"""
LANJING_DETAILS = r"""async ({rows, site}) => await Promise.all(rows.map(async row => {
    try {
        const response = await window.__knowledgeLanjingDetail({id: row.id, site, productId: row.product_id});
        return {id: row.id, response};
    } catch (_) { return {id: row.id, response: {code: -1}}; }
}))"""


def parse_listing(source, response):
    if not isinstance(response, dict):
        raise RuntimeError('平台列表返回格式异常')
    code = response.get('code')
    expected = 200 if source == 'zying' else 0
    if code != expected:
        if code in (401, 403) or '登录' in clean(response.get('msg') or response.get('message')):
            raise LoginRequired('登录状态已失效，请重新登录并确定')
        raise RuntimeError('平台未成功返回列表，请检查账号套餐、查询权限或登录状态')
    if response.get('totalMore'):
        raise RuntimeError('平台限制了可读取的结果数量，请检查账号套餐，无法确认全部产品')
    data = response.get('data')
    if source == 'zying':
        rows, total = (data.get('Datas'), data.get('Total')) if isinstance(data, dict) else (None, None)
    else:
        rows = data.get('list') if isinstance(data, dict) else data
        total = data.get('total') if isinstance(data, dict) else response.get('total')
    if not isinstance(rows, list) or total is None:
        raise RuntimeError('平台分页信息缺失，无法确认已读取全部产品')
    try:
        total = int(total)
    except (TypeError, ValueError) as exc:
        raise RuntimeError('平台产品总数格式异常') from exc
    if total < 0:
        raise RuntimeError('平台产品总数格式异常')
    return rows, total


class PageReader:
    def __init__(self, page, source):
        self.page, self.source, self.site = page, source, ''
        self.stop_check = lambda: None
        self.blue_site = ''
        if source == 'lanjing':
            page.on('request', self._observe_site)

    def _observe_site(self, request):
        if urlsplit(request.url).path.endswith('/web/items/search'):
            try:
                self.blue_site = clean((request.post_data_json or {}).get('siteId')) or self.blue_site
            except Exception:
                pass

    def prepare(self):
        page = self.page
        page.goto(SOURCES[self.source]['url'], wait_until='domcontentloaded')
        try:
            selector = 'text=仓储模式' if self.source == 'zying' else 'button:has-text("查询")'
            page.locator(selector).first.wait_for(timeout=20000)
        except Exception as exc:
            if 'login' in page.url.lower() or page.locator('input[type=password]').count():
                raise LoginRequired('尚未登录，请在打开的网页完成登录后点击确定已登录') from exc
            raise RuntimeError('未找到目标模块，请检查账号权限或页面是否改版') from exc
        if self.source == 'zying':
            page.get_by_text('自发货', exact=True).first.click()
            if not page.evaluate(DISCOVER_ZYING):
                raise RuntimeError('未识别智赢列表组件，请检查页面是否改版')
            self.site = clean(page.evaluate("localStorage.getItem('meliSiteikey') || '1'"))
        else:
            # Clicking the page's query button initializes the real selected site.
            with page.expect_request(lambda r: urlsplit(r.url).path.endswith('/web/items/search')) as pending:
                page.get_by_role('button', name='查询', exact=True).click()
            self._observe_site(pending.value)
            if not self.blue_site:
                raise RuntimeError('未读取到蓝鲸当前站点，无法确认产品范围')
            self.site = self.blue_site
            if not page.evaluate(DISCOVER_LANJING):
                raise RuntimeError('未识别蓝鲸列表组件，请检查页面是否改版')

    def listing(self, number):
        if self.source == 'zying':
            data = self.page.evaluate(ZYING_LIST, number)
            return parse_listing(self.source, data['response'])
        rows, total = parse_listing(self.source, self.page.evaluate(LANJING_LIST, {'page': number, 'site': self.site}))
        # The list's brand_id may be absent. Read its official detail to fill
        # brand/images/category/date, while preserving the list's sales/price.
        missing = [row for row in rows if not pick(row, 'brand_id', 'brand', 'brand_name')]
        for start in range(0, len(missing), 5):
            self.stop_check()
            batch = missing[start:start + 5]
            outcomes = self.page.evaluate(LANJING_DETAILS, {'rows': batch, 'site': self.site})
            by_id = {clean(item['id']): item['response'] for item in outcomes}
            for row in batch:
                response = by_id.get(clean(row.get('id')), {})
                if response.get('code') in (401, 403):
                    raise LoginRequired('蓝鲸登录状态已失效，请重新登录并确定')
                detail = response.get('data')
                if response.get('code') != 0 or not isinstance(detail, dict) or clean(detail.get('id')) != clean(row.get('id')):
                    row['_detail_error'] = '详情未补全，请检查平台查看权限后重新采集'
                    continue
                for key in ('brand_id', 'brand', 'pictures', 'pic_big', 'path_from_root', 'category_id', 'start_time', 'permalink'):
                    if detail.get(key) is not None:
                        row[key] = detail[key]
        return rows, total


@contextmanager
def open_reader(source):
    from playwright.sync_api import sync_playwright
    from bit import bit_zying_caiji as config
    endpoint = config.DEFAULT_ZYING_EDGE_DEBUGGER_ADDRESS
    config.ensure_visible_zying_edge_login_window(endpoint, start_url=SOURCES[source]['url'])
    if not endpoint.startswith(('http://', 'https://', 'ws://', 'wss://')):
        endpoint = 'http://' + endpoint
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=30000)
        page = browser.contexts[0].new_page()
        page.set_default_timeout(30000)
        page.goto(SOURCES[source]['url'], wait_until='domcontentloaded')
        page.bring_to_front()
        # Exiting Playwright disconnects while keeping the visible page/session.
        yield PageReader(page, source)


class CollectionManager:
    def __init__(self, store=None):
        self.store = store or ProductStore()
        self.lock = threading.RLock()
        self.jobs = {}
        self.active = None

    def status(self, owner):
        with self.lock:
            job = self.jobs.get(owner)
            return copy.deepcopy(job['state']) if job else {'running': False, 'phase': 'idle', 'message': '等待打开网页'}

    def start(self, owner, source, browser_factory=None):
        validate_source(source)
        with self.lock:
            if self.active:
                raise ValueError('已有产品采集正在使用浏览器，请先结束当前任务')
            state = {'running': True, 'phase': 'opening', 'source': source,
                     'message': '正在打开' + SOURCES[source]['label'], 'pages': 0, 'collected': 0, 'total': 0}
            job = {'state': state, 'confirm': threading.Event(), 'stop': threading.Event()}
            self.jobs[owner] = self.active = job
        thread = threading.Thread(target=self._run, args=(owner, job, browser_factory or open_reader), daemon=True,
                                  name='knowledge-product-collection')
        try:
            thread.start()
        except Exception:
            with self.lock:
                state.update(running=False, phase='failed', message='采集线程启动失败')
                self.active = None
            raise
        return self.status(owner)

    def confirm(self, owner):
        with self.lock:
            job = self.jobs.get(owner)
            if not job or not job['state']['running'] or job['state']['phase'] != 'waiting_login':
                raise ValueError('当前没有等待确认登录的任务')
            job['state'].update(phase='collecting', message='正在验证登录并读取产品')
            job['confirm'].set()
        return self.status(owner)

    def stop(self, owner):
        with self.lock:
            job = self.jobs.get(owner)
            if job and job['state']['running']:
                job['stop'].set(); job['confirm'].set()
                job['state'].update(phase='stopping', message='正在结束任务，已读取结果会保留')
        return self.status(owner)

    def _update(self, job, **values):
        with self.lock:
            job['state'].update(values)

    def _check_stop(self, job):
        if job['stop'].is_set():
            raise CollectionStopped()

    def _wait_login(self, job, message):
        with self.lock:
            job['confirm'].clear()
            job['state'].update(phase='waiting_login', message=message)
        if not job['confirm'].wait(900):
            raise RuntimeError('等待确定登录超时，请重新打开网页')
        self._check_stop(job)

    def _run(self, owner, job, browser_factory):
        source = job['state']['source']
        try:
            with browser_factory(source) as reader:
                reader.stop_check = lambda: self._check_stop(job)
                self._wait_login(job, '请在' + SOURCES[source]['label'] + '网页完成登录，再点击确定已登录')
                while True:
                    self._check_stop(job)
                    try:
                        reader.prepare()
                        break
                    except LoginRequired as exc:
                        self._wait_login(job, str(exc))
                seen = set()
                detail_failures = 0
                number, read_count, target = 1, 0, None
                while True:
                    self._check_stop(job)
                    try:
                        rows, total = reader.listing(number)
                    except LoginRequired as exc:
                        self._wait_login(job, str(exc))
                        while True:
                            try:
                                reader.prepare()
                                break
                            except LoginRequired as login_error:
                                self._wait_login(job, str(login_error))
                        if reader.site != job['state'].get('site', reader.site):
                            raise RuntimeError('重新登录后站点发生变化，请重新启动采集')
                        continue
                    if target is None:
                        target = total
                    elif total != target:
                        raise RuntimeError('采集期间平台产品总数发生变化，已保留结果，请重新采集以确认完整性')
                    products = [normalize_product(source, row, reader.site) for row in rows]
                    fresh = [item for item in products if (item['site'], item['product_id']) not in seen]
                    detail_failures += sum(bool(item['detail_error']) for item in fresh)
                    if rows and not fresh:
                        raise RuntimeError('平台重复返回同一页，已保留结果，未标记为全部完成')
                    self._check_stop(job)
                    self.store.save(owner[0], fresh)
                    seen.update((item['site'], item['product_id']) for item in fresh)
                    read_count += len(rows)
                    self._update(job, pages=number, collected=len(seen), total=target, site=reader.site, detail_failures=detail_failures,
                                 message=f'已读取第 {number} 页，共 {len(seen)} / {target} 件产品')
                    if read_count >= target:
                        if len(seen) != target:
                            raise RuntimeError('产品数量与平台总数不一致，已保留结果，请重新采集')
                        break
                    if not rows:
                        raise RuntimeError('分页提前结束，已保留结果，尚未读取全部产品')
                    number += 1
                self._update(job, phase='completed', message=f'采集完成，共 {len(seen)} 件产品' +
                             (f'；{detail_failures} 件详情未补全，可重新采集' if detail_failures else ''))
        except CollectionStopped:
            self._update(job, phase='stopped', message='任务已结束，已读取的产品已保存')
        except Exception as exc:
            self._update(job, phase='failed', message=str(exc)[:500])
        finally:
            with self.lock:
                job['state']['running'] = False
                if self.active is job:
                    self.active = None


manager = CollectionManager()
