from unittest.mock import Mock
import pytest
from bit import bit_daily_task as daily


@pytest.mark.parametrize('loader', [daily.load_authorized_appeal_api_targets, daily.load_authorized_appeal_collection_targets, daily.load_authorized_appeal_shop_site_config])
def test_store_scope_intersects_authorized_sites(monkeypatch, loader):
    monkeypatch.setattr(daily, 'list_mercado_store_tokens', lambda: {'rows': [
        {'id': i, 'display_name': f'shop{i}', 'site_settings': [
            {'site_id': 'MLM', 'appeal_enabled': True, 'group_name': '甲组', 'salesperson': '张三'},
            {'site_id': 'MLB', 'appeal_enabled': False, 'group_name': '甲组'},
        ]} for i in (1, 2)
    ]})
    result = loader(store_ids=[2], group_names=['甲组'])
    assert 'shop2' in str(result) and 'shop1' not in str(result)
    assert 'BR' not in str(result) and 'MLB' not in str(result)
    assert not loader(store_ids=[2], group_names=['其他组'])
    assert not loader(store_ids=[99])


@pytest.mark.parametrize('mode', ['once', 'loop'])
def test_store_ids_reach_agent_executor(monkeypatch, tmp_path, mode):
    from bit import bit_interface as web
    params = web.build_daily_task_params({'mode': mode, 'store_ids': [2, '1', 2], 'browser_provider': 'ziniao'})
    runner = Mock(return_value=[])
    monkeypatch.setattr(daily, 'run_ai_appeal_once' if mode == 'once' else 'loop_ai_appeal', runner)
    web.execute_daily_task(params, Mock(), Mock(is_set=lambda: False), 'offline', tmp_path/'task.log', {})
    assert runner.call_args.kwargs['store_ids'] == [1, 2]
    assert runner.call_args.kwargs['browser_provider'] == 'ziniao'


def test_rejects_store_outside_current_scope(monkeypatch):
    from bit import bit_interface as web
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: {})
    monkeypatch.setattr(web, '_authorized_token_ids_for_user', lambda user: {1})
    monkeypatch.setattr(web.bit_db_api, 'list_mercado_store_tokens', lambda: {'rows': []})
    monkeypatch.setattr(web, '_filter_mercado_tokens_for_user', lambda *args: {'rows': [{'id': 1}]})
    with web.app.test_request_context():
        with pytest.raises(ValueError, match='授权范围'):
            web.build_daily_task_params({'store_ids': [2]})


@pytest.mark.parametrize('mode,strategy,types', [
    ('once', 'normal', ['侵权']), ('loop', 'normal', ['混合模式']),
    ('once', 'algorithm', ['侵权', '投诉']),
])
def test_store_ids_reach_all_plans(monkeypatch, mode, strategy, types):
    plans = []
    def build(*args, **kwargs):
        plans.append(kwargs['store_ids'])
        return []
    monkeypatch.setattr(daily, 'build_appeal_plan', build)
    fn = daily.loop_ai_appeal if mode == 'loop' else daily.run_ai_appeal_once
    fn(types, store_ids=[1, 2], browser_provider='ziniao', task_strategy=strategy,
       _task_lock=Mock(acquired=True), **({'max_rounds': 1} if mode == 'loop' else {}))
    assert plans == [[1, 2]] * (6 if types == ['混合模式'] else 1)
