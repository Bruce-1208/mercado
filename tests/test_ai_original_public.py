from copy import deepcopy
from erp.ai_original_public import compact_product_row


def test_list_projection_preserves_editor_values_and_images_without_durable_caches():
    sku = {'sku_id': 'a', 'image_url': 'image.jpg', 'weight_g': 100, 'raw_dimensions': 'large evidence',
           'attribute_combinations': [{'id': 'SIZE', 'value_name': 'S'}]}
    draft = {'status': 'failed', 'variations': [sku], 'category_checkpoint': {'variations': [sku]},
             'generated_copy': {'large': 'body'}, 'sku_image_cache': {'cached': True},
             'generation_budget': {'spent_yuan': .01, 'calls': [{'prompt_tokens': 10, 'completion_tokens': 20}], 'cache': {'response': 'large'}},
             'manual_attributes': [{'id': 'MODEL', 'value_name': 'User model'}], 'edit_revision': 3}
    row = {'id': 1, 'source_snapshot_json': 'large', 'ai_original': draft,
           'original_1688': {'images': ['a.jpg', 'b.jpg'], 'variations': [sku]},
           'marketplace_listings': {'mercadolibre': draft, 'amazon': {'title': 'Amazon title'}}}
    before = deepcopy(row); result = compact_product_row(row)
    assert row == before
    assert 'source_snapshot_json' not in result
    assert 'category_checkpoint' not in result['ai_original']
    assert result['ai_original']['generation_budget'] == {'spent_yuan': .01, 'total_tokens': 30}
    assert result['ai_original']['manual_attributes'] == draft['manual_attributes']
    assert result['ai_original']['edit_revision'] == 3
    assert result['original_1688']['images'] == ['a.jpg', 'b.jpg']
    assert result['ai_original']['variations'][0]['image_url'] == 'image.jpg'
    assert 'raw_dimensions' not in result['ai_original']['variations'][0]
    assert result['marketplace_listings'] == {'amazon': {'title': 'Amazon title'}}
