import json
from io import BytesIO

import pytest
from pathlib import Path

from erp.ai_original_products import (
    build_copy_prompt,
    generate_marketplace_copy,
    generate_ai_white_background_image,
    normalize_1688_product,
    create_white_background_image,
    normalize_1688_image_url,
    prepare_ai_original_product,
)
from erp.mercadolibre_batch_publish import (
    _prepared_listing_from_product_row,
    product_publish_issues,
)


def test_normalize_1688_product_keeps_source_facts():
    product = normalize_1688_product({
        "source_url": "https://detail.1688.com/offer/123456789.html",
        "title": "  测试   商品  ",
        "price": "12.50",
        "images": ["//cbu01.alicdn.com/a.jpg"],
        "properties": [{"name": "材质", "value": "ABS"}],
    })

    assert product["source_item_id"] == "1688123456789"
    assert product["source_1688_item_id"] == "123456789"
    assert product["title"] == "测试 商品"
    assert product["images"] == ["https://cbu01.alicdn.com/a.jpg"]


def test_normalize_1688_image_url_removes_expired_cdn_resize_suffix():
    transformed = (
        "https://cbu01.alicdn.com/img/ibank/photo.jpg_460x460q100.jpg_.jpg"
    )

    assert normalize_1688_image_url(transformed) == (
        "https://cbu01.alicdn.com/img/ibank/photo.jpg"
    )
    assert normalize_1688_product({
        "source_url": "https://detail.1688.com/offer/123456789.html",
        "title": "测试商品",
        "main_image_url": transformed,
        "images": [transformed],
    })["main_image_url"].endswith("/photo.jpg")


def test_ai_copy_removes_declared_brand_and_limits_both_titles():
    response = json.dumps({
        "title_es": "Acme Organizador portátil para escritorio con muchos accesorios y almacenamiento",
        "title_pt": "Acme Organizador portátil para mesa com acessórios e armazenamento",
        "description_es": "Descripción original en español.",
        "description_pt": "Descrição original em português.",
        "attributes": [
            {
                "name_es": "Material",
                "name_pt": "Material",
                "value_name_es": "Plástico",
                "value_name_pt": "Plástico resistente",
            },
            {"name": "Tipo de cierre", "value_name": "Tapa a presión"},
        ],
        "brand_terms": ["Acme"],
    })

    copy = generate_marketplace_copy(
        {"title": "Acme 收纳盒", "properties": [{"name": "品牌", "value": "Acme"}]},
        chat=lambda *_args, **_kwargs: response,
    )

    assert "acme" not in copy["title_es"].lower()
    assert "acme" not in copy["title_pt"].lower()
    assert len(copy["title_es"]) <= 60
    assert len(copy["title_pt"]) <= 60
    assert copy["attributes"][0]["name"] == "Material"
    assert copy["attributes"][0]["value_name"] == "Plástico"


def test_ai_copy_rejects_reused_original_description():
    response = json.dumps({
        "title_es": "Organizador portátil",
        "title_pt": "Organizador portátil",
        "description_es": "Texto original sin cambios.",
        "description_pt": "Novo texto em português.",
        "brand_terms": [],
    })

    with pytest.raises(ValueError, match="原创描述"):
        generate_marketplace_copy(
            {"title": "收纳盒", "description_text": "Texto original sin cambios."},
            chat=lambda *_args, **_kwargs: response,
        )


def test_ai_copy_merges_explicit_1688_properties_when_model_omits_them():
    response = json.dumps({
        "title_es": "Capa de disfraz",
        "title_pt": "Capa para fantasia",
        "description_es": "Una capa nueva para disfraces.",
        "description_pt": "Uma capa nova para fantasias.",
        "attributes": [],
        "brand_terms": [],
    })
    product = {
        "title": "万圣节披风",
        "properties": [
            {"id": "COLOR", "name": "颜色", "value": "橙色", "value_id": "52000"},
            {"id": "MATERIAL", "name": "材质", "value": "涤纶"},
        ],
        "weight_g": 200,
        "package_length_cm": 30,
        "package_width_cm": 25,
        "package_height_cm": 1,
        "variations": [{"attribute_combinations": [{"name": "长度", "value": "130cm"}]}],
    }

    copy = generate_marketplace_copy(product, chat=lambda *_args, **_kwargs: response)

    assert {item["id"] for item in copy["attributes"]} >= {"COLOR", "MATERIAL"}
    assert next(item for item in copy["attributes"] if item["id"] == "COLOR")["value_name"] == "橙色"


def test_ai_copy_prompt_contains_all_source_fact_groups():
    prompt = build_copy_prompt({
        "title": "测试披风",
        "properties": [{"name": "材质", "value": "涤纶"}],
        "description_text": "长度 130cm",
        "category_name": "Capes",
        "weight_g": 200,
        "package_length_cm": 30,
        "package_width_cm": 25,
        "package_height_cm": 1,
        "variations": [{"name": "橙色 / 130cm"}],
    })

    assert "1688商品属性" in prompt
    assert "重量和包装尺寸" in prompt
    assert "1688变体和规格组合" in prompt
    assert "尽可能填全" in prompt


def test_white_background_image_is_square_and_white(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    source = Image.new("RGB", (700, 500), (230, 230, 230))
    for x in range(220, 480):
        for y in range(110, 410):
            source.putpixel((x, y), (25, 80, 180))
    buffer = BytesIO()
    source.save(buffer, format="PNG")

    class Response:
        content = buffer.getvalue()

        @staticmethod
        def raise_for_status():
            return None

    def remove_background(image):
        cutout = image.convert("RGBA")
        alpha = Image.new("L", cutout.size, 0)
        for x in range(220, 480):
            for y in range(110, 410):
                alpha.putpixel((x, y), 255)
        cutout.putalpha(alpha)
        return cutout

    path, url = create_white_background_image(
        "https://cbu01.alicdn.com/source.png",
        "123456789",
        image_dir=tmp_path,
        http_get=lambda *_args, **_kwargs: Response(),
        background_remove=remove_background,
    )
    with Image.open(path) as output:
        assert output.size == (1024, 1024)
        assert all(channel >= 245 for channel in output.getpixel((0, 0)))
        assert output.getpixel((512, 512))[2] >= 170
    assert url.endswith("/api/ai-original-products/images/1688-123456789-ai-white.jpg")


def test_ai_white_background_image_uses_image_model_callback(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    source = Image.new("RGB", (320, 240), (30, 80, 180))
    source_buffer = BytesIO()
    source.save(source_buffer, format="PNG")
    generated = Image.new("RGB", (512, 512), "white")
    generated_buffer = BytesIO()
    generated.save(generated_buffer, format="PNG")

    class Response:
        content = source_buffer.getvalue()

        @staticmethod
        def raise_for_status():
            return None

    path, url = generate_ai_white_background_image(
        "https://cbu01.alicdn.com/source.png",
        "123456789",
        image_dir=tmp_path,
        http_get=lambda *_args, **_kwargs: Response(),
        image_generate=lambda **_kwargs: generated_buffer.getvalue(),
    )
    with Image.open(path) as output:
        assert output.width == output.height
        assert output.width >= 1024
        assert output.getpixel((0, 0)) == (255, 255, 255)
    assert url == "/api/ai-original-products/images/1688-123456789-ai-white.jpg"


def test_prepare_persists_white_image_before_invalid_copy_response(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    source = Image.new("RGB", (320, 240), (30, 80, 180))
    source_buffer = BytesIO()
    source.save(source_buffer, format="PNG")
    generated = Image.new("RGB", (512, 512), "white")
    generated_buffer = BytesIO()
    generated.save(generated_buffer, format="PNG")

    class Response:
        content = source_buffer.getvalue()

        @staticmethod
        def raise_for_status():
            return None

    persisted = []
    row = {
        "source_item_id": "1688123456789",
        "source_snapshot_json": {
            "original_1688": {
                "source_1688_item_id": "123456789",
                "title": "测试商品",
                "main_image_url": "https://cbu01.alicdn.com/source.png",
                "images": ["https://cbu01.alicdn.com/source.png"],
            }
        },
    }

    with pytest.raises(ValueError, match="JSON"):
        prepare_ai_original_product(
            row,
            image_model="test-image-model",
            image_generate=lambda **_kwargs: generated_buffer.getvalue(),
            image_dir=tmp_path,
            http_get=lambda *_args, **_kwargs: Response(),
            chat=lambda *_args, **_kwargs: "not json",
            on_image_ready=lambda url, method: persisted.append((url, method)),
        )

    assert persisted == [(
        "/api/ai-original-products/images/1688-123456789-ai-white.jpg",
        "ai_image_edit",
    )]


def _ai_row():
    prepared = {
        "status": "completed",
        "title_es": "Organizador portátil de escritorio",
        "title_pt": "Organizador portátil para mesa",
        "description_es": "Nueva descripción en español.",
        "description_pt": "Nova descrição em português.",
        "image_generation_method": "ai_image_edit",
        "attributes": [
            {
                "id": "MATERIAL",
                "name": "Material",
                "name_es": "Material",
                "name_pt": "Material",
                "value_name": "Plástico",
                "value_name_es": "Plástico",
                "value_name_pt": "Plástico resistente",
            },
            {"id": "COLOR", "name": "Color", "value_name": "Azul"},
            {"id": "BRAND", "value_name": "Generic"},
            {"id": "ITEM_CONDITION", "value_name": "New"},
        ],
        "main_image_url": "http://127.0.0.1:5000/api/ai-original-products/images/1688-123-ai-white.jpg",
    }
    return {
        "id": 8,
        "source_type": "ai_original",
        "source_item_id": "1688123",
        "source_url": "https://detail.1688.com/offer/123.html",
        "main_image_url": prepared["main_image_url"],
        "title": prepared["title_es"],
        "description_text": prepared["description_es"],
        "currency_id": "USD",
        "category_id": "CBT123",
        "weight_g": 500,
        "weight_basis": "ai_original_manual",
        "net_proceeds_usd": 20,
        "review_status": "approved",
        "infringement_risk_level": 0,
        "infringement_checked_at": "2026-09-22 12:00:00",
        "source_snapshot_json": json.dumps({
            "ai_original": prepared,
            "source": {"id": "CBT123", "site_id": "CBT", "pictures": [{"source": prepared["main_image_url"]}]},
            "description": {"plain_text": prepared["description_es"]},
        }),
    }


def test_publish_preparation_uses_portuguese_only_for_brazil():
    row = _ai_row()

    mexico_source, mexico_description = _prepared_listing_from_product_row(row, "MLM")
    brazil_source, brazil_description = _prepared_listing_from_product_row(row, "MLB")

    assert mexico_source["title"].startswith("Organizador portátil de")
    assert mexico_description["plain_text"].startswith("Nueva")
    assert brazil_source["title"].startswith("Organizador portátil para")
    assert brazil_description["plain_text"].startswith("Nova")
    assert {item["id"] for item in mexico_source["attributes"]} >= {"MATERIAL", "COLOR", "BRAND", "ITEM_CONDITION"}
    assert next(item for item in brazil_source["attributes"] if item["id"] == "MATERIAL")["value_name"] == "Plástico resistente"
    assert product_publish_issues(row) == []


def test_ai_original_publish_requires_completed_copy_and_white_image():
    row = _ai_row()
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["ai_original"]["status"] = "pending"
    row["source_snapshot_json"] = json.dumps(snapshot)
    row["main_image_url"] = "https://example.test/original.jpg"

    issues = product_publish_issues(row)

    assert "AI 原创任务尚未完成" in issues
    assert "首图尚未完成 AI 白底生成" in issues


def test_ai_original_publish_requires_generated_listing_attributes():
    row = _ai_row()
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["ai_original"]["attributes"] = [
        {"id": "BRAND", "value_name": "Generic"},
        {"id": "ITEM_CONDITION", "value_name": "New"},
    ]
    row["source_snapshot_json"] = json.dumps(snapshot)

    assert "AI 商品属性尚未生成" in product_publish_issues(row)


def test_ai_original_publish_accepts_free_local_white_background():
    row = _ai_row()
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["ai_original"]["image_generation_method"] = "local_background_removal"
    row["source_snapshot_json"] = json.dumps(snapshot)

    assert product_publish_issues(row) == []


def test_ai_original_publish_requires_completed_safe_infringement_check():
    row = _ai_row()
    row["infringement_risk_level"] = None
    row["infringement_checked_at"] = None
    assert "AI 原创商品尚未完成侵权检测" in product_publish_issues(row)

    row["infringement_risk_level"] = 1
    row["infringement_checked_at"] = "2026-09-22 12:00:00"
    assert "AI 原创商品侵权检测未通过：疑似侵权" in product_publish_issues(row)


def test_workbench_exposes_ai_original_module_and_batch_actions():
    root = Path(__file__).resolve().parents[1]
    template = (root / "bit" / "templates" / "index.html").read_text(encoding="utf-8")
    script = (root / "bit" / "static" / "ai-original-products.js").read_text(encoding="utf-8")

    assert 'data-tab="ai-original-products"' in template
    assert 'id="tab-ai-original-products"' in template
    assert "1688 原始资料" in script
    assert "AI 美客多刊登稿" in script
    assert "renderAiOriginalAttributes" in script
    assert "rembg / isnet-general-use" in template
    assert 'id="ai-original-image-model"' not in template
    assert "执行所选 AI 任务" in template
    assert "上架所选到对应店铺" in template
    assert 'fetch("/api/ai-original-products/process"' in script
    assert 'fetch("/api/mercado-products/publish"' in script
    assert "aiOriginalDisplayImageUrl" in script
    assert "/api/ai-original-products/source-image?url=" in script
    assert 'referrerpolicy="no-referrer"' in script


def test_server_requirements_include_free_local_background_removal():
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "bit" / "requirements-server.txt").read_text(encoding="utf-8")

    assert "rembg[cpu]==2.0.67" in requirements


def test_workbench_exposes_1688_product_area_for_collector_records():
    root = Path(__file__).resolve().parents[1]
    template = (root / "bit" / "templates" / "index.html").read_text(encoding="utf-8")
    script = (root / "bit" / "static" / "1688-products.js").read_text(encoding="utf-8")

    assert 'data-tab="1688-products"' in template
    assert 'id="tab-1688-products"' in template
    assert "1688产品区" in template
    assert 'fetch(`/api/1688-products?' in script
    assert "products1688OpenDetail" in script
    assert "products1688SourceUrl" in script
    assert 'class="p1688-product-image-link"' in script
    assert 'class="p1688-product-title-link"' in script
    assert "1688 商品编号" not in script
    assert "products1688DisplayImageUrl" in script
    assert "/api/ai-original-products/source-image?url=" in script
    assert 'referrerpolicy="no-referrer"' in script


@pytest.mark.parametrize('invalid', ['', 'not json', '{"title_es": "incomplete"', '{}'])
def test_copy_regenerates_invalid_or_incomplete_response(invalid):
    calls = []
    valid = json.dumps({
        'title_es': 'Organizador', 'title_pt': 'Organizador',
        'description_es': 'Para organizar objetos.',
        'description_pt': 'Para organizar itens.', 'attributes': [],
    })

    def chat(messages, **kwargs):
        calls.append(kwargs['max_tokens'])
        return invalid if len(calls) == 1 else valid

    result = generate_marketplace_copy({'title': '收纳盒'}, chat=chat)
    assert result['title_es'] == 'Organizador'
    assert calls == [4000, 8000]


def test_copy_invalid_response_retries_are_bounded():
    calls = []

    def chat(*args, **kwargs):
        calls.append(kwargs['max_tokens'])
        return ''

    with pytest.raises(ValueError, match='JSON'):
        generate_marketplace_copy({'title': '收纳盒'}, chat=chat)
    assert calls == [4000, 8000, 16000]


def test_original_product_update_uses_client_api(monkeypatch):
    from bit import bit_db_api
    calls = []
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {'id': 123, 'title_es': 'Organizador'}

    monkeypatch.setattr(bit_db_api, '_request', request)
    changes = {'title': 'Organizador'}
    saved = bit_db_api.update_ai_original_product(123, changes)
    assert saved['id'] == 123
    assert calls == [('PATCH', '/api/db/ai-original-products/123',
                      {'timeout': 120, 'json': changes})]


def test_batch_continues_after_one_product_initial_save_fails(monkeypatch):
    import ast
    import concurrent.futures
    import logging
    import threading
    import erp.ai_original_products as original

    # Load only the worker: importing the web app starts unrelated services.
    source = Path('bit/bit_interface.py').read_text(encoding='utf-8')
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == '_run_ai_original_task')
    state = {}
    saved_ids = []

    def save(product_id, changes):
        if product_id == 1:
            raise RuntimeError('database unavailable for first record')
        saved_ids.append(product_id)
        return {}

    monkeypatch.setattr(original, 'prepare_ai_original_product', lambda *a, **kw: {
        'title': 'Organizador', 'description_text': 'Description',
        'main_image_url': '/image.jpg', 'source_snapshot_json': {},
        'ai_original': {'title_es': 'Organizador', 'title_pt': 'Organizador'},
    })
    namespace = dict(json=json, logging=logging,
                     ThreadPoolExecutor=concurrent.futures.ThreadPoolExecutor,
                     as_completed=concurrent.futures.as_completed,
                     _ai_original_task_lock=threading.Lock(),
                     _ai_original_task_state=state,
                     db_update_ai_original_product=save)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<worker>', 'exec'), namespace)
    namespace['_run_ai_original_task']([{'id': 1}, {'id': 2}], workers=1)
    assert state['status'] == 'partial'
    assert state['processed_count'] == 2
    assert state['failed_count'] == state['completed_count'] == 1
    assert saved_ids == [2, 2]


@pytest.mark.parametrize('base_url,model,disabled', [
    ('https://api.deepseek.com', 'deepseek-v4-pro', True),
    ('https://api.deepseek.com', 'deepseek-chat', False),
    ('https://other.example/v1', 'deepseek-v4-pro', False),
])
def test_original_copy_thinking_setting_is_provider_scoped(monkeypatch, base_url, model, disabled):
    from AI_Agent import deepseek
    calls = []

    def chat(*args, **kwargs):
        calls.append(kwargs)
        return json.dumps({'title_es': 'Caja', 'title_pt': 'Caixa',
                           'description_es': 'Una caja.', 'description_pt': 'Uma caixa.'})

    monkeypatch.setattr(deepseek, 'chat_deepseek', chat)
    generate_marketplace_copy({'title': '盒子'}, base_url=base_url, model=model)
    if disabled:
        assert calls[0]['thinking'] is False
    else:
        assert 'thinking' not in calls[0]


def test_chat_forwards_explicit_thinking_without_changing_default(monkeypatch):
    from types import SimpleNamespace
    from AI_Agent import deepseek
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(deepseek, '_get_client', lambda **kw: client)
    deepseek.chat_deepseek([], thinking=False)
    deepseek.chat_deepseek([])
    assert calls[0]['extra_body'] == {'thinking': {'type': 'disabled'}}
    assert 'extra_body' not in calls[1]
