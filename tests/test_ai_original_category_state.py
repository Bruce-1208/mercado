from copy import deepcopy
import json
import pytest

from erp.ai_original_state import category_failure_state
from erp.mercadolibre_collection_store import _mirror_zying_snapshot_fields


@pytest.mark.parametrize('status,category,workflow,expected', [
    ('pending', '', '', 'pending'),
    ('processing', '', 'needs_category', 'processing'),
    ('waiting_off_peak', '', 'needs_category', 'waiting_off_peak'),
    ('completed', 'CBT123', 'completed', 'completed'),
    ('completed', '', 'completed', 'failed'),
    ('completed', 'MLM123', 'completed', 'failed'),
    ('completed', 'CBT_WRONG', 'completed', 'failed'),
    ('completed', 'CBT123', 'needs_category', 'failed'),
    ('needs_category', '', '', 'failed'),
])
def test_category_status_matches_list_and_does_not_mutate_snapshot(status, category, workflow, expected):
    draft = dict(status=status, category_id='CBT123', workflow_status=workflow)
    row = dict(source_type='ai_original', category_id=category,
               source_snapshot_json=json.dumps({'ai_original': draft}))
    before = deepcopy(row)
    state = category_failure_state(row, draft)
    listed = _mirror_zying_snapshot_fields(row)
    assert state['status'] == listed['ai_status'] == expected
    assert listed['marketplace_listings']['mercadolibre'] == listed['ai_original']
    if expected == 'failed':
        assert listed['ai_error']
        assert state['failed_step'] == 'category'
    assert row == before
    assert draft['status'] == status
