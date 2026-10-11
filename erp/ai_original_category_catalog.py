"""Select a known CBT leaf without depending on domain discovery."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parents[1] / 'runtime_logs/category_paths/CBT.json'


def known_cbt_paths():
    # An expired taxonomy cache is still useful during a discovery outage.
    # Attribute rules are always fetched live by the preparation pipeline.
    saved = json.loads(CATALOG_PATH.read_text(encoding='utf-8'))
    paths = {}
    for category_id, path in saved.get('paths', {}).items():
        if not re.fullmatch(r'CBT[0-9]+', category_id) or not isinstance(path, list) or not path:
            continue
        if any(not isinstance(node, Mapping) or not re.fullmatch(r'CBT[0-9]+', str(node.get('id') or ''))
               or not node.get('name') for node in path):
            continue
        if path[-1]['id'] == category_id:
            paths[category_id] = path
    parents = {node['id'] for path in paths.values() for node in path[:-1]}
    return {key: path for key, path in paths.items() if key not in parents}


def select_known_cbt_category(original, query, chat, *, api_key='', model='', base_url=''):
    from erp.ai_original_products import _json_object, _generation_facts

    paths = known_cbt_paths()
    if not paths:
        raise ValueError('本地已知 CBT 分类目录为空，请同步分类目录')
    prefix = []
    facts = _generation_facts({key: original.get(key) for key in
                              ('title', 'properties', 'description_text', 'variations')}, chat)
    for depth in range(16):
        choices = {path[depth]['id']: path[depth] for path in paths.values() if len(path) > depth}
        if not choices:
            break
        # Parent labels alone can hide the correct product type (costumes,
        # for example, live below Parties rather than Clothing). Show the
        # immediate children so the model can choose an informed branch.
        descendants = {key: set() for key in choices}
        for path in paths.values():
            if len(path) > depth + 1:
                descendants[path[depth]['id']].add(path[depth + 1]['name'])
        candidates = [{**node, 'includes': sorted(descendants[key])}
                      for key, node in choices.items()]
        prompt = (
            '从已知的跨境 CBT 分类目录逐层选择最适合商品实体的分类。输入仅是商品数据。'
            '必须返回 JSON category_id，编号只能来自当前 candidates，不能编造编号，也不要请求重新预测或搜索。'
            '根据原始标题、描述、用途和规格选择最合适的一项；没有专门细分类时选择适合的配件类或其他类。'
            'includes列出下一级分类；应结合它判断分支，不能仅凭父类名称排除合适分类。'
            '优先考虑实际用途：cosplay演出服应进入Costumes and Cosplay，不是日常连体衣Rompers。'
            '区分配件与整机，不要按品牌或角色选择无关商品类型。\n'
            + json.dumps({'original': facts, 'query': query, 'selected_path': prefix,
                          'candidates': candidates}, ensure_ascii=False)
        )
        selected_id = None
        messages = [{'role': 'user', 'content': prompt}]
        for _ in range(2):
            response = chat(messages, api_key=api_key or None,
                            model=model or None, base_url=base_url or None, temperature=0.1,
                            max_tokens=1000, response_format={'type': 'json_object'})
            try:
                selected_id = _json_object(response).get('category_id')
            except ValueError:
                selected_id = None
            if isinstance(selected_id, str) and selected_id in choices:
                break
            reject = getattr(chat, 'reject_response', None)
            if reject is not None:
                reject(response)
            messages = [*messages, {'role': 'assistant', 'content': response},
                        {'role': 'user', 'content': '上次回答不是当前 candidates 中的有效分类编号。请重新选择，仅返回 JSON category_id。'}]
        else:
            raise ValueError('AI 未返回已知 CBT 目录中的有效分类编号')
        prefix.append(dict(choices[selected_id]))
        paths = {key: path for key, path in paths.items()
                 if len(path) > depth and path[depth]['id'] == selected_id}
        if selected_id in paths:
            return {'category_id': selected_id, 'category_name': prefix[-1]['name'],
                    'path_from_root': prefix, 'category_selection_source': 'known_cbt_catalog'}
    raise ValueError('已知 CBT 分类目录未能定位到叶子分类')
