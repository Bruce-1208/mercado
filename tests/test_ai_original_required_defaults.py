import pytest
from erp.ai_original_products import complete_required_attributes


def complete(schema, variations=()):
    return complete_required_attributes({}, [], schema, variations,
        chat=lambda *a, **k: '{"attributes":[]}', allow_defaults=True)


def test_required_defaults_use_schema_choices_and_keep_provenance():
    attrs, missing = complete([
        {'id': 'GENDER', 'tags': {'required': True}, 'values': [{'id': '2', 'name': 'Women'}, {'id': '3', 'name': 'Unisex'}]},
        {'id': 'MATERIAL', 'tags': {'required': True}, 'values': [{'id': '9', 'name': 'Plastic'}]},
        {'id': 'MODEL', 'value_type': 'string', 'tags': {'required': True}},
        {'id': 'OPTIONAL', 'value_type': 'string'}])
    assert not missing
    assert [a['id'] for a in attrs] == ['GENDER', 'MATERIAL', 'MODEL']
    assert attrs[0]['value_id'] == '3'
    assert attrs[1]['value_id'] == '9'
    assert all(a['value_basis'] == 'ai_default' for a in attrs)


@pytest.mark.parametrize('aid', ['GTIN', 'ANVISA_REGISTRATION', 'CERTIFICATION_NUMBER', 'OEM', 'COMPATIBLE_MODELS'])
def test_identifiers_and_regulatory_facts_are_not_fabricated(aid):
    attrs, missing = complete([{'id': aid, 'tags': {'required': True}, 'value_type': 'string'}])
    assert not attrs
    assert missing[0]['id'] == aid


def test_partial_sku_defaults_do_not_overwrite_known_values():
    variations = [{'attributes': [{'id': 'MATERIAL', 'value_name': 'Wood'}]}, {'attributes': []}]
    attrs, missing = complete([{'id': 'MATERIAL', 'value_type': 'string', 'tags': {'required': True}}], variations)
    assert not attrs and not missing
    assert variations[0]['attributes'][0]['value_name'] == 'Wood'
    assert variations[1]['attributes'][0]['value_basis'] == 'ai_default'
