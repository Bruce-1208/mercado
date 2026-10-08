"""Offline regression checks for provider migration, never paid calls."""
from types import SimpleNamespace
import pytest
from AI_Agent import deepseek as adapter


def test_defaults_and_environment_use_only_dashscope(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'retired-key')
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    assert adapter.DEEPSEEK_MODEL == 'qwen3.7-flash'
    assert adapter.DEEPSEEK_BASE_URL == 'https://dashscope.aliyuncs.com/compatible-mode/v1'
    assert adapter._get_deepseek_api_key() == ''
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'current-key')
    assert adapter._get_deepseek_api_key() == 'current-key'


def test_default_request_disables_thinking_and_uses_qwen(monkeypatch):
    calls, clients = [], []
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='ok'), finish_reason='stop')], usage=None)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: calls.append(kw) or response)))
    monkeypatch.setattr(adapter, 'OpenAI', lambda **kw: clients.append(kw) or client)
    assert adapter.chat_deepseek([{'role': 'user', 'content': 'hello'}], api_key='current-key') == 'ok'
    assert calls[0]['model'] == 'qwen3.7-flash'
    assert calls[0]['extra_body'] == {'enable_thinking': False}
    assert clients[0] == {'api_key': 'current-key', 'base_url': adapter.QWEN_BASE_URL}


@pytest.mark.parametrize('options', [{'model': 'deepseek-v4-pro'}, {'base_url': 'https://api.deepseek.com'}])
def test_stale_deepseek_settings_rejected_before_client_creation(monkeypatch, options):
    monkeypatch.setattr(adapter, '_get_client', lambda **kw: pytest.fail('must not send credentials'))
    with pytest.raises(ValueError, match='已停用'):
        adapter.chat_deepseek([], api_key='old-key', **options)


def test_appeal_thinking_reaches_provider_and_only_final_text_is_used(monkeypatch):
    from bit.bit_ai_appeal_copy import generate_ai_appeal_copy
    calls, options = [], []
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='请依据商品用途人工复核。', reasoning_content='Internal reasoning'),
        finish_reason='stop')], usage=None)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: calls.append(kw) or response)))
    client.with_options = lambda **kw: options.append(kw) or client
    monkeypatch.setattr(adapter, '_get_client', lambda **kw: client)
    result = generate_ai_appeal_copy('侵权', ['MLM123'], 'test-key',
        product_loader=lambda ids: [{'product_id': 'MLM123', 'title': '台灯', 'description': '照明'}])
    assert calls[0]['extra_body'] == {'enable_thinking': True, 'thinking_budget': 4096}
    assert calls[0]['max_tokens'] == 8192
    assert options == [{'timeout': 120, 'max_retries': 0}]
    assert result['generated_text'] == '请依据商品用途人工复核。'
    assert 'Internal reasoning' not in result['message']
