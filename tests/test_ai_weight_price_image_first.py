"""Current workflow regressions using isolated in-memory ERP/supplier fixtures."""
import io
import threading

import pytest
from openpyxl import load_workbook

from erp.ai_weight_price.config import validate, selection_key
from erp.ai_weight_price.browser import CircuitOpen
from erp.ai_weight_price.models import Models
from erp.ai_weight_price.reports import execution_xlsx
from erp.ai_weight_price.service import Service
from erp.ai_weight_price.store import Store


class ImageModel:
    def match_images(self, task, candidates):
        score = .95 if int(task['erp_goods_id']) % 3 == 1 else .98
        evidence = [{'candidate': candidates[0], 'review': {'index': 1, 'same_product': True, 'confidence': score, 'reason': '离线图片证据'}}]
        approved = [{**candidates[0], 'image_confidence': score}] if score >= .95 else []
        return approved, evidence

    def match(self, task, candidate):
        return {**candidate, 'selected_sku': candidate['skus'][0], 'confidence': .98}, [{'confidence': .98}]

    def supplier_info(self, task, text):
        return {'weight_g': '450' if int(task['erp_goods_id']) % 3 == 0 else None}


class ImageBrowser:
    def __init__(self, config):
        self.config = config
        self.operations = []
        self.messages = []
        self.records = {}
        self.corrupt_weight = False

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def confirm_login(self): pass

    def collect(self, store, on_task):
        for index in range(1, 13):
            key = str(index)
            self.operations.append(('collect', key))
            store.add({'erp_goods_id': key, 'title': '蓝色测试杯' + key, 'main_image_url': 'https://img.example/' + key + '.jpg'})
            store.include_in_scope(self.config['run_scope'], key, 1)
            self.records[key] = {'weight_g': '430', 'net_income_usd': '9.5', 'review_status': '待审核'}
            on_task(key)

    def search_images(self, task):
        self.operations.append(('image', task['erp_goods_id']))
        return [{'url': 'https://detail.1688.com/offer/1.html', 'title': '蓝色杯', 'main_image_url': 'https://img.example/cup.jpg'}]

    def read_offer(self, task, candidate):
        self.operations.append(('detail', task['erp_goods_id']))
        return {**candidate, 'skus': [{'id': 'blue', 'label': '蓝色一只', 'price': '22',
                                     'base_price_cny': '20', 'variant_surcharge_cny': '2',
                                     'raw_price': '20', 'raw_surcharge': '+2'}]}

    def write_patch(self, task, changes, before_save):
        key = task['erp_goods_id']
        before_save(dict(self.records[key]))
        self.operations.append(('save', key))
        self.records[key].update(changes)
        if self.corrupt_weight:
            self.records[key]['weight_g'] = '0'
        return dict(self.records[key])


def make_service(tmp_path, monkeypatch):
    config = validate({'writeback_enabled': True, 'usd_cny_rate': '7.2'})
    config.update(run_selection={'category': '', 'start_page': 1, 'end_page': 1}, max_items=10, run_id='image-ten')
    config['run_scope'] = selection_key(config['run_selection'], config)
    browser = ImageBrowser(config)
    service = Service(tmp_path, browser_factory=lambda *a: browser, models_factory=lambda *a: ImageModel())
    monkeypatch.setattr(service, 'exchange_rate', lambda cfg: {'cny_per_usd': '7.2', 'date': '2026-09-07', 'source': 'manual'})
    service.store.set_state('run', {'run_id': 'image-ten', 'mode': 'pipeline', 'selection': config['run_selection'],
                                    'max_items': 10, 'processed_items': 0})
    return service, browser, config


def test_ten_image_first_products_keep_status_and_only_write_weight_or_profit(tmp_path, monkeypatch):
    service, browser, config = make_service(tmp_path, monkeypatch)
    lock = service.lock(); assert lock.acquire()
    service.run(config, 'pipeline', None, lock)
    store = Store(tmp_path)
    assert store.counts()['blocked'] == 0
    assert store.counts()['risk'] == 7
    assert store.counts()['success'] == 3
    assert not browser.messages
    assert len(browser.records) == 10 and store.state('pipeline_current') is None
    assert browser.operations[:8] == [('collect', '1'), ('image', '1'), ('detail', '1'), ('save', '1'),
                                     ('collect', '2'), ('image', '2'), ('detail', '2'), ('save', '2')]
    assert browser.records['1'] == {'weight_g': '430', 'net_income_usd': '4', 'review_status': '待审核'}
    assert browser.records['2'] == {'weight_g': '430', 'net_income_usd': '4', 'review_status': '待审核'}
    assert browser.records['3'] == {'weight_g': '450', 'net_income_usd': '4', 'review_status': '待审核'}
    run, rows = store.run_report('image-ten')
    assert run['outcome'] == 'completed' and run['processed_items'] == 10
    assert all(row.get('execution_duration_seconds') is not None for row in rows)
    assert (run.get('blocked_items', 0), run['risk_items'], run['success_items']) == (0, 7, 3)
    written = [row for row in rows if row.get('write_intent')]
    assert len(written) == 10
    assert all(row['write_verified'] and row['write_history'][-1]['verified'] for row in written)
    assert all(row['review_status'] == '待审核' for row in browser.records.values())
    assert all('review_status' not in (row.get('write_intent') or {}) for row in rows)
    assert 'weight_g' not in rows[1]['write_intent']
    assert rows[1]['erp_before']['weight_g'] == rows[1]['erp_after']['weight_g'] == '430'
    assert sum('保存成功并回读确认' in event['message'] for event in store.logs(limit=2000)) == 10
    assert sum('无需回写重量或净收益' in event['message'] for event in store.logs(limit=2000)) == 0
    sheet = load_workbook(io.BytesIO(execution_xlsx(rows, run))).active
    assert sheet['C6'].value == sheet['C7'].value == '风险'
    assert sheet['K7'].value is None and sheet['M7'].value == 430
    assert (sheet['V7'].value, sheet['W7'].value, sheet['X7'].value) == ('待审核', None, '待审核')


def test_missing_weight_must_retain_original_after_save_and_skip_on_mismatch(tmp_path, monkeypatch):
    service, browser, config = make_service(tmp_path, monkeypatch)
    browser.corrupt_weight = True
    lock = service.lock(); assert lock.acquire()
    service.run(config, 'pipeline', None, lock)
    assert service.store.state('run')['outcome'] == 'completed'
    assert service.store.state('run')['processed_items'] == 10
    assert service.store.get('1')['status'] == 'skipped'
    assert service.store.get('1')['skip_reason'].startswith('自动跳过异常：')
    assert not service.store.get('1')['write_verified']
    assert len(browser.records) == 10


def test_search_timeout_is_skipped_before_reading_later_products(tmp_path, monkeypatch):
    from erp.ai_weight_price.browser import SearchTimeout
    service, browser, config = make_service(tmp_path, monkeypatch)
    def timeout(task):
        raise SearchTimeout('未返回搜索结果')
    monkeypatch.setattr(browser, 'search_images', timeout)
    lock = service.lock(); assert lock.acquire()
    service.run(config, 'pipeline', None, lock)
    assert service.store.get('1')['status'] == 'skipped'
    assert service.store.state('run')['outcome'] == 'completed'
    assert service.store.state('run')['processed_items'] == 10
    assert service.store.state('pipeline_current') is None
    assert [key for step, key in browser.operations if step == 'collect'] == list(map(str, range(1, 11)))
    assert not any(step == 'save' for step, _ in browser.operations)


def test_1688_read_exception_is_skipped_before_reading_later_products(tmp_path, monkeypatch):
    service, browser, config = make_service(tmp_path, monkeypatch)
    original = browser.search_images

    def fail_first_only(task):
        if task['erp_goods_id'] == '1':
            raise ValueError('1688图片按钮临时变化')
        return original(task)

    monkeypatch.setattr(browser, 'search_images', fail_first_only)
    lock = service.lock(); assert lock.acquire()
    service.run(config, 'pipeline', None, lock)
    assert service.store.get('1')['status'] == 'skipped'
    assert service.store.state('run')['processed_items'] == 10
    assert service.store.state('run')['outcome'] == 'completed'
    assert service.store.state('pipeline_current') is None
    assert ('image', '2') in browser.operations


def test_low_image_match_score_is_retained_for_task_display(tmp_path, monkeypatch):
    service, browser, config = make_service(tmp_path, monkeypatch)

    class LowScoreModel(ImageModel):
        def match_images(self, task, candidates):
            return [], [{'candidate': candidates[0], 'review': {
                'index': 1, 'same_product': False, 'confidence': .72, 'reason': '颜色不一致'}}]

    config['max_items'] = 1
    service.models_factory = lambda *a: LowScoreModel()
    lock = service.lock(); assert lock.acquire()
    service.run(config, 'pipeline', None, lock)
    task = service.store.get('1')
    assert task['status'] == 'blocked'
    assert task['image_match_confidence'] == .72


def test_ambiguous_supplier_variants_are_recorded_as_explicit_risk_not_system_error(tmp_path, monkeypatch):
    service, browser, config = make_service(tmp_path, monkeypatch)
    config['max_items'] = 1

    class AmbiguousBrowser(ImageBrowser):
        def read_offer(self, task, candidate):
            return {**candidate, 'skus': [
                {'id': 'flamingo', 'label': '火烈鸟款 / 115*55', 'price': '13'},
                {'id': 'unicorn', 'label': '独角兽款 / 115*55', 'price': '13'},
            ]}

    class AmbiguousModel(ImageModel):
        def match(self, task, candidate):
            return None, [{'same_product': False, 'confidence': .3,
                           'specs_confirmed': False, 'reason': 'ERP未提供唯一变体规格'}]

    browser = AmbiguousBrowser(config)
    service.browser_factory = lambda *args: browser
    service.models_factory = lambda *args: AmbiguousModel()
    lock = service.lock(); assert lock.acquire()
    service.run(config, 'pipeline', None, lock)

    task = service.store.get('1')
    assert task['status'] == 'risk'
    assert task['decision_reason'].startswith('图片匹配成功，但1688详情有2个变体')
    assert '图片已匹配，但无法确认目标SKU及其最终售价' not in task['decision_reason']
    assert not any('图片已匹配，但无法确认目标SKU及其最终售价' in event['message']
                   for event in service.store.logs(limit=2000))


def test_visible_search_timeout_is_retried_before_later_rows(tmp_path, monkeypatch):
    service, browser, config = make_service(tmp_path, monkeypatch)
    service.store.add({'erp_goods_id': '1', 'title': '旧超时商品'})
    service.store.skip('1', '搜索超时：未返回搜索结果')
    lock = service.lock(); assert lock.acquire()
    service.run(config, 'pipeline', None, lock)
    first_image = next(key for step, key in browser.operations if step == 'image')
    assert first_image == '1'
    assert any('自动重新处理' in event['message'] for event in service.store.logs(limit=2000))


def test_1688_login_pause_keeps_current_item_and_resume_starts_it_before_collection(tmp_path, monkeypatch):
    service, browser, config = make_service(tmp_path, monkeypatch)
    released = {'value': False}
    def search(task):
        browser.operations.append(('image', task['erp_goods_id']))
        if not released['value']:
            raise CircuitOpen('1688需要登录')
        return [{'url': 'https://detail.1688.com/offer/1.html', 'title': '蓝色杯',
                 'main_image_url': 'https://img.example/cup.jpg'}]
    monkeypatch.setattr(browser, 'search_images', search)
    lock = service.lock(); assert lock.acquire()
    service.run(config, 'pipeline', None, lock)
    task = service.store.get('1')
    assert task['status'] == 'pending' and task['stage'] == 'collected'
    assert service.store.state('run')['outcome'] == 'blocked'
    assert service.store.state('run')['processed_items'] == 0
    assert service.store.state('pipeline_current')['task_id'] == '1'
    assert service.store.state('circuit')['kind'] == 'browser_attention'
    assert browser.records['1'] == {'weight_g': '430', 'net_income_usd': '9.5', 'review_status': '待审核'}

    started = []
    monkeypatch.setattr(service, 'start', lambda *args: started.append(args))
    service.continue_after_human()
    assert service.store.state('circuit') is None
    assert started == [('pipeline', None, config['run_selection'], 10, True)]

    released['value'] = True
    before_resume = len(browser.operations)
    service.store.set_state('run', {'run_id': config['run_id'], 'processed_items': 0})
    lock = service.lock(); assert lock.acquire()
    service.run(config, 'pipeline', None, lock)
    assert browser.operations[before_resume] == ('image', '1')
    assert service.store.state('pipeline_current') is None
    assert service.store.state('run')['processed_items'] == 10
    assert len(browser.records) == 10


def test_old_login_redirect_exception_is_migrated_to_resume_switch(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.add({'erp_goods_id': 'old-1', 'title': '旧版登录跳转任务'})
    store.exception('old-1', '主图核重核价执行异常',
                    '1688搜图跳转至 login.taobao.com；请核对登录状态')
    selection = {'category': '', 'start_page': 1, 'end_page': 1}
    store.set_state('pipeline_current', {'scope': 'old-scope', 'task_id': 'old-1'})
    store.set_state('run_selection', selection)
    store.set_state('run', {'mode': 'pipeline', 'selection': selection, 'max_items': 10,
                            'processed_items': 0, 'outcome': 'blocked'})
    service = Service(tmp_path)
    assert service.store.state('circuit')['kind'] == 'browser_attention'
    started = []
    monkeypatch.setattr(service, 'start', lambda *args: started.append(args))
    service.continue_after_human()
    assert service.store.get('old-1')['status'] == 'pending'
    assert service.store.state('circuit') is None
    assert started == [('pipeline', None, selection, 10, True)]


@pytest.mark.parametrize('score,approved', [(.94999, False), (.95, True), (.99, True)])
def test_image_matching_threshold_is_inclusive_and_precedes_sku_read(monkeypatch, score, approved):
    model = Models(validate({}), lambda *a: None)
    calls = []
    def call(name, prompt, images, json_output):
        calls.append(images)
        return {'matches': [{'index': 1, 'same_product': True, 'confidence': score, 'reason': '同款'}]}
    monkeypatch.setattr(model, 'call', call)
    matches, evidence = model.match_images({'title': '杯', 'main_image_url': 'https://img.example/target.jpg'},
                                         [{'title': '杯', 'main_image_url': 'https://img.example/candidate.jpg', 'url': 'https://detail.1688.com/offer/1.html'}])
    assert bool(matches) is approved and len(evidence) == 1
    assert calls == [['https://img.example/target.jpg', 'https://img.example/candidate.jpg']]


def test_first_five_images_choose_highest_true_match_and_ignore_high_confidence_rejection(monkeypatch):
    model = Models(validate({}), lambda *a: None)
    candidates = [{'title': f'候选{i}', 'main_image_url': f'https://img.example/{i}.jpg',
                   'url': f'https://detail.1688.com/offer/{i}.html'} for i in range(1, 6)]
    scores = [(True, .95), (False, .99), (True, .96), (True, .97), (True, .94)]
    monkeypatch.setattr(model, 'call', lambda *a: {'matches': [
        {'index': index, 'same_product': same, 'confidence': score, 'reason': '测试'}
        for index, (same, score) in enumerate(scores, 1)]})

    matches, evidence = model.match_images(
        {'title': '目标', 'main_image_url': 'https://img.example/target.jpg'}, candidates)

    assert len(evidence) == 5
    assert [item['url'] for item in matches] == [
        'https://detail.1688.com/offer/4.html',
        'https://detail.1688.com/offer/3.html',
        'https://detail.1688.com/offer/1.html',
    ]


def test_image_matching_fills_missing_model_rows_as_unconfirmed(monkeypatch):
    model = Models(validate({}), lambda *a: None)
    candidates = [{'title': f'候选{i}', 'main_image_url': f'https://img.example/{i}.jpg',
                   'url': f'https://detail.1688.com/offer/{i}.html'} for i in range(1, 6)]
    # A valid but truncated JSON response must not pause the whole serial run.
    monkeypatch.setattr(model, 'call', lambda *a: {'matches': [
        {'index': 1, 'same_product': True, 'confidence': .96, 'reason': '同款'},
        {'index': 2, 'same_product': False, 'confidence': .1, 'reason': '非同款'},
        {'index': 3, 'same_product': False, 'confidence': .1, 'reason': '非同款'},
    ]})

    matches, evidence = model.match_images(
        {'title': '目标', 'main_image_url': 'https://img.example/target.jpg'}, candidates)

    assert [item['review']['index'] for item in evidence] == [1, 2, 3, 4, 5]
    assert evidence[-1]['review']['confidence'] == 0
    assert [item['url'] for item in matches] == ['https://detail.1688.com/offer/1.html']


@pytest.mark.parametrize('score', [True, '0.99', float('nan'), 1.1, -1])
def test_image_matching_rejects_invalid_scores(monkeypatch, score):
    model = Models(validate({}), lambda *a: None)
    monkeypatch.setattr(model, 'call', lambda *a: {'matches': [{'index': 1, 'same_product': True, 'confidence': score}]})
    with pytest.raises(ValueError, match='置信度无效'):
        model.match_images({'main_image_url': 'https://img.example/1.jpg'}, [{'main_image_url': 'https://img.example/2.jpg'}])
