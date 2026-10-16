"""Small UI projections; durable generation checkpoints stay on the server."""

_VARIATION_INTERNAL = {'raw_dimensions', 'raw_text', 'raw_weight', 'dimension_evidence_present'}
_DRAFT_INTERNAL = {'category_checkpoint', 'generated_copy', 'sku_image_cache'}


def _variations(rows):
    return [{k: v for k, v in row.items() if k not in _VARIATION_INTERNAL}
            if isinstance(row, dict) else row for row in rows]


def _draft(value):
    if not isinstance(value, dict):
        return value
    result = {k: v for k, v in value.items() if k not in _DRAFT_INTERNAL}
    for key in ('variations', 'source_variations'):
        if isinstance(result.get(key), list):
            result[key] = _variations(result[key])
    budget = result.get('generation_budget')
    if isinstance(budget, dict):
        result['generation_budget'] = {k: v for k, v in budget.items() if k in {
            'status', 'spent_yuan', 'reserved_yuan', 'total_tokens', 'budget_limit_enabled', 'cache_hits'}}
        if isinstance(budget.get('calls'), list):
            result['generation_budget']['total_tokens'] = sum(
                int(call.get('prompt_tokens') or 0) + int(call.get('completion_tokens') or 0)
                for call in budget['calls'] if isinstance(call, dict))
    return result


def compact_product_row(row):
    result = {k: v for k, v in row.items() if k != 'source_snapshot_json'}
    if 'ai_original' in result:
        result['ai_original'] = _draft(result['ai_original'])
    if isinstance(result.get('marketplace_listings'), dict):
        # Mercado's authoritative draft is already returned as ai_original.
        result['marketplace_listings'] = {k: _draft(v) for k, v in result['marketplace_listings'].items()
                                          if k != 'mercadolibre'}
    original = result.get('original_1688')
    if isinstance(original, dict):
        result['original_1688'] = dict(original)
        if isinstance(original.get('variations'), list):
            result['original_1688']['variations'] = _variations(original['variations'])
    return result
