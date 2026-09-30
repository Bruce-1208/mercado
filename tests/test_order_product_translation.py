from unittest.mock import patch

from bit.order_product_details import translate_product_detail


def test_translates_title_description_and_all_variant_attributes_without_sku_values():
    item = {'id': 'MLB123', 'title': 'Camisa', 'attributes': [
        {'id': 'BRAND', 'name': 'Marca', 'value_name': 'Acme'},
        {'id': 'COLOR', 'name': 'Cor', 'value_name': 'Azul'},
    ], 'variations': [{'attribute_combinations': [
        {'name': 'Tamanho', 'values': [{'name': 'Grande'}]},
    ], 'attributes': [{'id': 'SELLER_SKU', 'name': 'SKU', 'value_name': 'SKU-123'}]}]}
    with patch('bit.order_product_details.translate_texts', side_effect=lambda texts, source, target: ['中文' + text for text in texts]) as translate:
        result = translate_product_detail(item, {'text': '<p>Algodão &amp; seda</p>'})
    assert result['Camisa'] == '中文Camisa'
    assert result['Algodão & seda'] == '中文Algodão & seda'
    assert result['Grande'] == '中文Grande'
    assert 'Acme' not in result
    assert 'SKU-123' not in result
    assert translate.call_args.args[1:] == ('pt-BR', 'zh-CN')
    assert item['title'] == 'Camisa'


def test_batches_and_deduplicates_global_listing_text():
    item = {'id': 'CBT123', 'title': 'Product', 'attributes': [
        {'name': 'Color', 'value_name': f'Color {i}'} for i in range(105)
    ]}
    with patch('bit.order_product_details.translate_texts', side_effect=lambda texts, source, target: texts) as translate:
        result = translate_product_detail(item, {})
    assert len(result) == 107
    assert [len(call.args[0]) for call in translate.call_args_list] == [100, 7]
    assert translate.call_args.args[1:] == ('en', 'zh-CN')
