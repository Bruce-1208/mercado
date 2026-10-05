from copy import deepcopy

import pytest

from scripts.repair_ai_original_manual_fields import repaired_snapshot
from erp.mercadolibre_batch_publish import product_publish_issues


def snapshot():
    return {'ai_original': {
        'status': 'failed', 'failed_step': 'attributes',
        'error': 'DeepSeek 已尝试补全，仍缺少必填属性：PACKAGE_WEIGHT；请重试生成或手动补齐',
        'missing_required_attributes': [{'id': 'PACKAGE_WEIGHT'}],
        'category_id': 'CBT123', 'main_image_url': '/test-ai-white.jpg',
        'preparation': dict.fromkeys(('image_ready', 'copy_ready', 'category_ready', 'variations_ready'), True),
        'generated_copy': {'title_es': 'Mascara', 'title_pt': 'Mascara', 'description_es': 'Descripcion',
                           'description_pt': 'Descricao', 'attributes': [{'id': 'OLD'}]},
        'category_checkpoint': {'attributes': [{'id': 'MODEL', 'value_name': 'Mask'}], 'variations': []},
    }, 'marketplace_listings': {'amazon': {'title': 'Preserve'}}}


def test_recovers_only_manual_failure_without_losing_mapped_attributes():
    original = snapshot()
    before = deepcopy(original)
    repaired = repaired_snapshot(original)
    assert original == before
    assert repaired['ai_original']['status'] == 'completed'
    assert repaired['ai_original']['attributes'] == [{'id': 'MODEL', 'value_name': 'Mask'}]
    assert repaired['source']['category_id'] == 'CBT123'
    assert repaired['marketplace_listings']['amazon'] == {'title': 'Preserve'}
    assert repaired_snapshot(repaired) is None


@pytest.mark.parametrize('change', ['model', 'variation', 'unfinished'])
def test_keeps_real_generation_failures(change):
    original = snapshot()
    state = original['ai_original']
    if change == 'model':
        state['missing_required_attributes'].append({'id': 'MODEL'})
    elif change == 'variation':
        state['failed_step'] = 'variations'
    else:
        state['preparation']['variations_ready'] = False
    assert repaired_snapshot(original) is None


@pytest.mark.parametrize('field', ['package_length_cm', 'package_width_cm', 'package_height_cm'])
def test_missing_dimensions_block_publishing(field):
    row = dict(source_type='ai_original', weight_g=100, net_proceeds_usd=10,
               package_length_cm=10, package_width_cm=10, package_height_cm=10)
    assert not any('包装长宽高' in issue for issue in product_publish_issues(row))
    row[field] = None
    assert any('包装长宽高' in issue for issue in product_publish_issues(row))
