import pytest

from bit import bit_ai_appeal_copy


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("request failed")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, timeout):
        self.urls.append((url, timeout))
        if url.endswith("/description"):
            return FakeResponse({"plain_text": "适用于家庭照明的普通灯具"})
        return FakeResponse({"title": "家用桌面灯"})


def test_fetch_product_contexts_reads_title_and_description_for_each_id():
    session = FakeSession()

    rows = bit_ai_appeal_copy.fetch_product_contexts(
        ["mlm123", "MLM123", "MLB456"], session=session
    )

    assert rows == [
        {
            "product_id": "MLM123",
            "title": "家用桌面灯",
            "description": "适用于家庭照明的普通灯具",
        },
        {
            "product_id": "MLB456",
            "title": "家用桌面灯",
            "description": "适用于家庭照明的普通灯具",
        },
    ]
    assert len(session.urls) == 4


def test_generate_ai_appeal_copy_uses_manual_token_and_prefixes_ids():
    captured = {}

    def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "商品为普通家用照明用品，请人工复核并移除误判。"

    result = bit_ai_appeal_copy.generate_ai_appeal_copy(
        "侵权",
        ["MLM123"],
        "manual-secret",
        product_loader=lambda ids: [
            {"product_id": "MLM123", "title": "桌面灯", "description": "家用照明"}
        ],
        chat=fake_chat,
    )

    assert result["message"].startswith("产品编号：MLM123\n")
    assert captured["kwargs"]["api_key"] == "manual-secret"
    assert "桌面灯" in captured["messages"][1]["content"]
    assert "不得虚构" in captured["messages"][0]["content"]


def test_generate_ai_appeal_copy_rejects_reason_over_fifty_characters():
    with pytest.raises(RuntimeError, match="超过 50 个字"):
        bit_ai_appeal_copy.generate_ai_appeal_copy(
            "禁限售",
            ["MLB456"],
            "manual-secret",
            product_loader=lambda ids: [
                {"product_id": "MLB456", "title": "收纳盒", "description": "家用收纳"}
            ],
            chat=lambda messages, **kwargs: "理" * 51,
        )


def test_generate_ai_appeal_copy_requires_manual_token():
    with pytest.raises(ValueError, match="手动填写 DeepSeek Token"):
        bit_ai_appeal_copy.generate_ai_appeal_copy("侵权", ["MLM123"], "")


def test_generate_ai_appeal_copy_rejects_more_than_three_products():
    with pytest.raises(ValueError, match="最多处理 3 个产品"):
        bit_ai_appeal_copy.generate_ai_appeal_copy(
            "侵权", ["MLM1", "MLM2", "MLM3", "MLM4"], "manual-secret"
        )


@pytest.mark.parametrize('value', [None, {}, ['请复核']])
def test_model_output_must_be_text(value):
    with pytest.raises(RuntimeError, match='格式无效'):
        bit_ai_appeal_copy._clean_model_text(value)


def test_generation_bounds_request_and_hides_provider_secrets():
    def chat(messages, **kwargs):
        assert kwargs['thinking'] is False
        assert kwargs['timeout'] == 60
        assert kwargs['max_retries'] == 0
        raise RuntimeError('Authorization: manual-secret')

    with pytest.raises(RuntimeError, match='DeepSeek 话术生成失败') as error:
        bit_ai_appeal_copy.generate_ai_appeal_copy(
            '侵权', ['MLM1'], 'manual-secret',
            product_loader=lambda ids: [{'product_id': 'MLM1', 'title': '灯'}],
            chat=chat,
        )
    assert 'manual-secret' not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize('appeal_type,ids', [('侵权', []), ('未知类型', ['MLM1'])])
def test_invalid_request_rejected_before_loading_products(appeal_type, ids):
    def load(ids):
        pytest.fail('invalid request must not fetch products')
    with pytest.raises(ValueError):
        bit_ai_appeal_copy.generate_ai_appeal_copy(
            appeal_type, ids, 'manual-secret', product_loader=load,
        )


def test_deepseek_wrapper_applies_request_limits(monkeypatch):
    from types import SimpleNamespace
    from AI_Agent import deepseek

    captured = {}

    class Client:
        def with_options(self, **kwargs):
            captured['options'] = kwargs
            return self

        @property
        def chat(self):
            return SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            captured['request'] = kwargs
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='请人工复核。'))])

    monkeypatch.setattr(deepseek, '_get_client', lambda **kwargs: Client())
    assert deepseek.chat_deepseek(
        [], api_key='manual-secret', thinking=False, timeout=60, max_retries=0,
    ) == '请人工复核。'
    assert captured['options'] == {'timeout': 60, 'max_retries': 0}
    assert captured['request']['extra_body'] == {'thinking': {'type': 'disabled'}}


def test_store_contexts_use_matching_authorization(monkeypatch):
    from bit import bit_mysql, mercado_infraction_sync
    calls = []
    monkeypatch.setattr(bit_mysql, 'list_mercado_store_tokens', lambda: {'rows': [
        {'id': 1, 'display_name': 'other'}, {'id': 2, 'display_name': 'shop'}]})
    monkeypatch.setattr(bit_mysql, 'get_mercado_store_token', lambda token_id: {'id': token_id})

    class Client:
        def request(self, method, path, **kwargs):
            calls.append(path)
            return {'plain_text': '家用照明'} if path.endswith('/description') else {'title': '台灯'}

    def client(token, **kwargs):
        assert token['id'] == 2
        return Client(), token

    monkeypatch.setattr(mercado_infraction_sync, '_client_and_token', client)
    rows = bit_ai_appeal_copy.fetch_store_product_contexts('shop', ['MLM123'])
    assert rows == [{'product_id': 'MLM123', 'title': '台灯', 'description': '家用照明'}]
    assert calls == ['/marketplace/items/MLM123', '/items/MLM123/description']


def test_store_contexts_fail_closed_on_ambiguous_store(monkeypatch):
    from bit import bit_mysql
    monkeypatch.setattr(bit_mysql, 'list_mercado_store_tokens', lambda: {'rows': [
        {'id': 1, 'display_name': 'shop'}, {'id': 2, 'nickname': 'shop'}]})
    with pytest.raises(ValueError, match='唯一匹配'):
        bit_ai_appeal_copy.fetch_store_product_contexts('shop', ['MLM123'])


def test_remote_contexts_do_not_send_store_token(monkeypatch):
    from bit import bit_db_api
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    captured = {}
    def request(method, path, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return [{'product_id': 'MLM123', 'title': '台灯'}]
    monkeypatch.setattr(bit_db_api, '_request', request)
    assert bit_db_api.get_ai_appeal_product_contexts('shop', ['MLM123'])[0]['title'] == '台灯'
    assert captured['json'] == {'shop_name': 'shop', 'product_ids': ['MLM123']}


@pytest.mark.parametrize('status', [404, 403, 500])
def test_missing_description_is_allowed_but_auth_and_network_errors_are_not(monkeypatch, status):
    from bit import bit_mysql, mercado_infraction_sync
    from mercado_api.client import MercadoAPIError
    monkeypatch.setattr(bit_mysql, 'list_mercado_store_tokens', lambda: {
        'rows': [{'id': 2, 'display_name': 'shop'}]})
    monkeypatch.setattr(bit_mysql, 'get_mercado_store_token', lambda _: {'id': 2})
    class Client:
        def request(self, method, path, **kwargs):
            if path.endswith('/description'):
                raise MercadoAPIError(f'GET {path} 失败 ({status}): provider-secret')
            return {'title': '台灯'}
    monkeypatch.setattr(mercado_infraction_sync, '_client_and_token', lambda token, **kw: (Client(), token))
    if status == 404:
        assert bit_ai_appeal_copy.fetch_store_product_contexts('shop', ['MLM123'])[0]['description'] == ''
    else:
        with pytest.raises(RuntimeError) as error:
            bit_ai_appeal_copy.fetch_store_product_contexts('shop', ['MLM123'])
        assert 'provider-secret' not in str(error.value)


def test_record_mode_filter_is_applied_before_limit(monkeypatch):
    from bit import bit_mysql as db
    calls = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params): calls.append((sql, params))
        def fetchall(self): return []
    class Connection:
        def cursor(self): return Cursor()
        def close(self): pass
    monkeypatch.setattr(db, 'initialize_appeal_storage', lambda: None)
    monkeypatch.setattr(db, '_appeal_connection', Connection)
    db.get_ai_appeal_records(500, appeal_copy_mode='AI话术模式')
    sql, params = calls[0]
    assert sql.index('WHERE') < sql.index('ORDER BY') < sql.index('LIMIT')
    assert params == ('AI话术模式', 500)


def test_record_mode_filter_survives_remote_transport(monkeypatch):
    from bit import bit_db_api as db
    monkeypatch.setattr(db, 'DB_MODE', 'api')
    calls = []
    monkeypatch.setattr(db, '_request', lambda *args, **kwargs: calls.append(kwargs))
    db.get_ai_appeal_records(500, appeal_copy_mode='AI话术模式')
    assert calls[0]['params'] == {'limit': 500, 'appeal_copy_mode': 'AI话术模式'}
