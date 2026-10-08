import pytest
from bit import bit_check_risk as risk
from bit.bit_infringement_knowledge import normalize_list_type, knowledge_rule_matches
from bit.infringement_state import knowledge_version


def rule(**fields):
    return dict(id=99, brand_name='Sample', aliases=['SampleAlias'], source_type='manual',
                list_type='borderline', **fields)


@pytest.mark.parametrize('keywords,level,remaining', [(['SampleAlias'], 0, []), (['SampleAlias', 'OtherIP'], 2, ['OtherIP'])])
def test_borderline_is_advisory_and_other_risks_remain(keywords, level, remaining):
    result = risk._apply_borderline_policy({'title': 'SampleAlias box'},
        {'risk_level': 2, 'keywords': keywords}, [rule()])
    assert result['risk_level'] == level
    assert result['keywords'] == remaining
    assert result['borderline_brands'] == ['SampleAlias']
    assert '仅提示，不处理' in result['reason']


def test_blacklist_wins_and_rule_scope_expiry_are_respected():
    blocked = {**rule(), 'id': 100, 'list_type': 'blacklist'}
    assert risk._apply_borderline_policy({'title': 'Sample'}, {'risk_level': 2, 'keywords': ['Sample']}, [rule(), blocked])['risk_level'] == 2
    assert not knowledge_rule_matches(rule(valid_until='2020-01-01'))
    assert not knowledge_rule_matches(rule(scope={'mode': 'scoped', 'sites': ['MLM']}), site='MLB')
    assert normalize_list_type('擦边品牌') == 'borderline'
    assert knowledge_version([rule()]) != knowledge_version([])


def test_scan_persists_warning_and_rule_association(monkeypatch):
    product = dict(row_id=1, source_row_id=1, source_type='product_list', record_key='product_list:1', title='SampleAlias box')
    monkeypatch.setattr(risk, 'classify_risk_records', lambda *a, **k: [dict(row_id=1, record_key='product_list:1', risk_level=2, keywords=['SampleAlias'])])
    written = []
    result = risk.scan_products(sources=['product_list'], knowledge_records=[rule()],
        candidate_reader=lambda **k: [product], risk_writer=lambda rows: written.extend(rows) or len(rows))
    assert result['risk_0'] == 1
    assert written[0]['matched_rule_ids'] == [99]
    assert written[0]['borderline_brands'] == ['SampleAlias']
