"""Shared category failure semantics for stored drafts and generation retries."""
import re


def generation_input_blockers(draft, original=None):
    """Describe missing source facts; transient/model errors remain failures."""
    original = original or {}
    blockers = []
    manual = {a.get('id') for a in draft.get('manual_attributes') or []
              if a.get('value_name') or a.get('value_id')}
    supplied_number = original.get('part_number') or any(
        re.search(r'零件|配件号|货号|part.?number|oem', str(a.get('name') or a.get('id') or ''), re.I)
        and (a.get('value_name') or a.get('value')) for a in original.get('properties') or [] if isinstance(a, dict))
    if (not supplied_number and 'PART_NUMBER' not in manual
            and not re.fullmatch(r'[0-9]{5,30}', str(original.get('source_1688_item_id') or ''))
            and any(a.get('id') == 'PART_NUMBER' for a in draft.get('missing_required_attributes') or [])):
        blockers.append({'field': 'PART_NUMBER', 'message': '请补充供应商实际零件编号 PART_NUMBER'})
    if draft.get('failed_step') == 'sku_images':
        from erp.ai_original_images import restore_product_variation_image_sources, variation_image_sources, variation_white_image_ready
        rows = restore_product_variation_image_sources(draft.get('variations') or [], original,
                                                       draft.get('source_variations') or [])
        missing = [i for i, v in enumerate(rows, 1) if not variation_white_image_ready(v) and not variation_image_sources(v)]
        if missing:
            blockers.append({'field': 'sku_images', 'sku_indices': missing,
                             'message': f'{len(missing)} 个 SKU 缺少采集原图，请重新采集或补充图片链接'})
    return blockers


def source_input_state(draft, original=None):
    state = dict(draft)
    if state.get('status') not in {'failed', 'incomplete'}:
        return state
    blockers = generation_input_blockers(state, original)
    state['input_blockers'] = blockers
    # Missing source images/attributes are repair failures. "待补充" belongs
    # only to the listing's missing physical dimensions or actual weight.
    if state.get('status') == 'incomplete':
        state['status'] = 'failed'
    if state.get('workflow_status') == 'needs_input':
        state['workflow_status'] = {'sku_images': 'needs_sku_images',
                                    'attributes': 'needs_attributes'}.get(state.get('failed_step'), 'failed')
    return state


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


def manual_listing_issues(row):
    """Return missing shared physical/economic facts for every target platform."""
    import math

    issues = []
    for field, label in (('package_length_cm', '包装长'), ('package_width_cm', '包装宽'),
                         ('package_height_cm', '包装高'), ('weight_g', '实重'),
                         ('net_proceeds_usd', '净收益 USD')):
        try:
            valid = math.isfinite(float(row.get(field))) and float(row[field]) > 0
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            issues.append(f'{label}必须填写且大于 0')
    if row.get('weight_basis') in {'calculated_volumetric', 'legacy_unknown', 'plugin_volumetric_fallback'}:
        issues.append('必须填写有效实重，不能使用体积重')
    return issues


def manual_approval_issues(row, draft, original=None):
    """Validate saved Mercado listing facts before an explicit human approval."""
    from erp.ai_original_images import missing_variation_white_images

    original = original or {}
    issues = []
    manual_only_failure = (
        draft.get('status') in {'failed', 'incomplete'}
        and draft.get('failed_step') in {'weight', 'dimensions', 'net_proceeds', 'manual_fields'}
    )
    if draft.get('status') != 'completed' and not manual_only_failure:
        issues.append('AI 原创任务完成后才能审核通过')
    row_category = str(row.get('category_id') or '').strip().upper()
    draft_category = str(draft.get('category_id') or '').strip().upper()
    if not re.fullmatch(r'CBT[0-9]+', row_category):
        issues.append('美客多类目未完成')
    elif draft_category != row_category:
        issues.append('美客多类目与 AI 生成资料不一致，请重新制作')
    attributes = draft.get('attributes') or []
    def populated(attribute):
        return bool(attribute.get('value_name') or attribute.get('value_id') or attribute.get('values'))

    def package(attribute):
        return bool(re.fullmatch(r'(SELLER_)?PACKAGE_(LENGTH|WIDTH|HEIGHT|WEIGHT)',
                                 str(attribute.get('id') or '').upper()))

    if not any(populated(a) and not package(a) and str(a.get('id') or '').upper() not in {'BRAND', 'ITEM_CONDITION'} for a in attributes):
        issues.append('商品属性未完成')
    if any(not package(a) and not any(a.get('id') == value.get('id') and populated(value) for value in attributes)
           for a in draft.get('missing_required_attributes') or []):
        issues.append('必填商品属性未补齐')
    if (draft.get('preparation') or {}).get('required_ready') is False:
        issues.append('必填商品属性需重新校验')
    for field in ('title_es', 'title_pt'):
        title = str(draft.get(field) or '').strip()
        if not title or len(title) > 60 or re.search(r'[\u3400-\u9fff]', title):
            issues.append('双语标题未完成')
            break
        from erp.mercadolibre_translation import title_language_issue
        language_issue = title_language_issue(
            title, 'es' if field == 'title_es' else 'pt-BR',
        )
        if language_issue:
            issues.append(('西班牙语' if field == 'title_es' else '葡萄牙语') + language_issue)
            break
    validation = draft.get('validation')
    if not isinstance(validation, dict):
        issues.append('平台规则校验未完成')
    elif validation.get('errors'):
        issues.append('平台规则校验未通过')
    if not all(str(draft.get(field) or '').strip() for field in ('description_es', 'description_pt')):
        issues.append('双语描述未完成')
    if (draft.get('image_generation_method') not in {'ai_image_edit', 'local_background_removal'}
            or '-ai-white.jpg' not in str(row.get('main_image_url') or '')):
        issues.append('AI 白底主图未完成')
    if missing_variation_white_images(draft, original):
        issues.append('SKU 白底主图未完成')
    variations = draft.get('variations') or []
    if ((draft.get('preparation') or {}).get('variations_ready') is False or
        (original.get('variations') and (len(variations) != len(original['variations']) or
         any(not sku.get('attribute_combinations') or any(not a.get('id') or not (a.get('value_id') or a.get('value_name'))
             for a in sku['attribute_combinations']) for sku in variations)))):
        issues.append('SKU 规格未完成')
    issues.extend(manual_listing_issues(row))
    return issues
