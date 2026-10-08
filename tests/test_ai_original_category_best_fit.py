"""Best-fit category fallback uses product facts and live platform validation."""
import json

import pytest

from erp import ai_original_products as products


def test_keycaps_fall_back_to_best_accessory_category_and_continue_schema():
    prompts = []
    calls = []
    candidates = [
        {'category_id': 'CBT101', 'category_name': 'Keyboard Accessories'},
        {'category_id': 'CBT102', 'category_name': 'Keyboards'},
    ]

    class Client:
        def request(self, method, path, **kwargs):
            calls.append(path)
            if 'domain_discovery' in path:
                return candidates
            assert path == '/categories/CBT101/attributes'
            return []

    def chat(messages, **kwargs):
        prompt = messages[0]['content']
        prompts.append(prompt)
        if '最终兜底选类' in prompt:
            context = json.loads(prompt.split('\n', 1)[1])
            assert context['original']['title'] == '机械键盘替换键帽'
            assert context['original']['description_text'] == '仅键帽，不包含键盘'
            assert '上位类' in prompt
            return '{"category_id":"CBT101", "reason":"Keyboard accessory"}'
        if '选择与原始商品实体' in prompt:
            return '{"category_id":"", "search_query":"mechanical keyboard keycaps"}'
        return '{"attributes":[]}'

    checkpoints = []
    result = products.complete_marketplace_category(
        {'title': '机械键盘替换键帽', 'description_text': '仅键帽，不包含键盘'},
        {'product_type_en': 'mechanical keyboard keycaps'}, Client(), chat=chat,
        on_category_ready=checkpoints.append,
    )
    assert result['category_id'] == 'CBT101'
    assert checkpoints[0]['category_id'] == 'CBT101'
    assert calls == ['/marketplace/domain_discovery/search', '/categories/CBT101/attributes']
    assert sum('选择与原始商品实体' in prompt for prompt in prompts) == 0  # whole keyboards excluded


def test_best_fit_does_not_accept_invented_id_outside_existing_candidates(monkeypatch):
    monkeypatch.setattr(products, '_fallback_marketplace_category',
                        lambda *a, **k: {'category_id': 'CBT103'})
    result = products._select_marketplace_category(
        {'title': '键帽'}, 'keycaps', [
            {'category_id': 'CBT101'}, {'category_id': 'CBT102'},
        ], object(), chat=lambda *a, **k: '{"category_id":"CBT999", "search_query":"keycaps"}')
    assert result['category_id'] == 'CBT103'
