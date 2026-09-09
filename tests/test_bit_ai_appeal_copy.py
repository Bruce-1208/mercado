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
