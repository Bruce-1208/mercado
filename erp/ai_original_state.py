"""Shared category failure semantics for stored drafts and generation retries."""
import re


def category_failure_state(row, draft):
    """Interpret legacy false completions without changing the supplied snapshot."""
    state = dict(draft)
    status = state.get('status') or 'pending'
    if status in {'processing', 'waiting_off_peak', 'failed'}:
        return state
    category_id = row.get('category_id') if 'category_id' in row else state.get('category_id')
    invalid_completion = status == 'completed' and not re.fullmatch(r'CBT[0-9]+', str(category_id or ''))
    if invalid_completion or status == 'needs_category' or state.get('workflow_status') == 'needs_category':
        state.update(status='failed', failed_step='category', workflow_status='needs_category',
                     error=state.get('error') or '美客多类目无效或尚未生成，请重新生成所选产品')
    return state
