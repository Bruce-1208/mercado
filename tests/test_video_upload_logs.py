import ast
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask, request, jsonify
from werkzeug.datastructures import FileStorage

from bit import video_reviews, video_upload_logs as logs


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from bit import bit_mysql
    from erp import mercadolibre_store_link_store as links
    monkeypatch.setenv('BIT_STORE_LINK_MARKER_DB_PATH', str(tmp_path / 'markers.sqlite3'))
    monkeypatch.setenv('AI_VIDEO_STORAGE_PATH', str(tmp_path / 'videos'))
    monkeypatch.setattr(bit_mysql, 'get_mercado_store_token_summary', lambda _: {})
    monkeypatch.setattr(links, 'get_store_links_by_ids', lambda _: [])


def record(number, token=7):
    video_reviews.record_upload({
        'id': number, 'token_id': token, 'site_id': 'MLM', 'item_id': f'MLM{number}',
        'title': f'商品 {number}', 'store_name': '测试店铺', 'group_name': '墨西哥组',
        'thumbnail_url': 'https://example.com/product.jpg', 'salesperson': '小张',
    }, {'clip_uuid': f'clip-{number}'}, 'CBT1', 'remote', f'2026-10-10T{number % 24:02}:00:00Z')


def test_all_uploads_paginate_and_scope_before_counting():
    for number in range(25):
        record(number)
    record(99, token=8)
    page = logs.list_uploads(allowed_token_ids=[7])
    assert page['total'] == 25 and page['total_pages'] == 2
    assert len(page['rows']) == 20
    assert len(logs.list_uploads(page=2, allowed_token_ids=[7])['rows']) == 5
    assert logs.list_uploads(allowed_token_ids=[])['total'] == 0
    row = page['rows'][0]
    assert row['salesperson'] == '小张' and row['group_name'] == '墨西哥组'
    assert row['thumbnail_url'].endswith('product.jpg')
    assert row['link_id'] == 23  # SQLite journal IDs must not replace listing IDs.


def test_upload_filters_and_shanghai_date_boundaries():
    record(1)
    record(23)
    assert logs.list_uploads(search='墨西哥组', salesperson='小张')['total'] == 2
    assert logs.list_uploads(search='MLM23')['total'] == 1
    assert logs.list_uploads(salesperson='其他业务员')['total'] == 0
    data = logs.list_uploads(uploaded_from='2026-10-09T16:00:00Z', uploaded_before='2026-10-10T16:00:00Z')
    assert [row['item_id'] for row in data['rows']] == ['MLM1']
    with pytest.raises(ValueError, match='时间范围'):
        logs.list_uploads(uploaded_from='2026-10-11', uploaded_before='2026-10-10')


def test_legacy_manifests_merge_reviews_and_failed_attempts(tmp_path):
    record(1)
    folder = tmp_path / 'videos' / 'job-a'
    folder.mkdir(parents=True)
    manifest = {'id': 'job-a', 'name': '演示视频', 'published': [{
        'link_id': 1, 'item_id': 'MLM1', 'token_id': 7, 'site_id': 'MLM',
        'clip_uuid': 'clip-1', 'salesperson': '小张', 'published_at': '2026-10-10T01:00:00Z',
    }], 'publish_attempts': [{
        'link_id': 2, 'status': 'failed', 'started_at': '2026-10-10T02:00:00Z', 'salesperson': '小李',
        'token_id': 7, 'item_id': 'MLM2', 'title': '失败商品', 'group_name': '墨西哥组',
        'message': '授权失效',
    }]}
    (folder / 'job.json').write_text(json.dumps(manifest))
    data = logs.list_uploads(job_id='job-a', allowed_token_ids=[7])
    assert data['total'] == 2
    assert data['rows'][0]['status_label'] == '上传失败'
    assert data['rows'][1]['status_label'] == '已提交待审核'
    assert data['rows'][1]['video_name'] == '演示视频'
    # Accepted history survives deleting a video task.
    (folder / 'job.json').unlink()
    assert logs.list_uploads()['total'] == 1


def test_real_upload_captures_snapshot_actor_and_failure(monkeypatch):
    from bit import bit_mysql, bit_store_link_sync, bit_store_link_video
    from erp import mercadolibre_store_link_store as links
    row = {'id': 42, 'token_id': 7, 'item_id': 'MLM42', 'site_id': 'MLM', 'store_name': '测试店铺'}
    monkeypatch.setattr(links, 'get_store_links_by_ids', lambda _: [row])
    monkeypatch.setattr(bit_mysql, 'get_mercado_store_token', lambda _: {'id': 7})
    monkeypatch.setattr(bit_mysql, 'get_mercado_store_token_summary', lambda _: {
        'site_settings': [{'site_id': 'MLM', 'group_name': '墨西哥组'}],
    })
    client = Mock()
    client.get_marketplace_item.return_value = {
        'status': 'active', 'cbt_item_id': 'CBT42', 'site_id': 'MLM', 'seller_id': 123,
        'title': '真实链接名', 'permalink': 'https://example.com/item',
        'pictures': [{'secure_url': 'https://example.com/main.jpg'}],
    }
    client.request.side_effect = [{'id': 7}, {'marketplaces': [
        {'site_id': 'MLM', 'user_id': 123, 'logistic_type': 'remote'},
    ]}]
    client.upload_item_clip.return_value = {'clip_uuid': 'real-clip'}
    monkeypatch.setattr(bit_store_link_sync, '_client_and_token', lambda token: (client, token))
    file = lambda: FileStorage(stream=io.BytesIO(b'video'), filename='test.mp4')
    bit_store_link_video.upload_store_link_video(42, file(), actor='上传业务员',
                                                video_metadata={'job_id': 'job-a', 'video_name': '成品视频'})
    data = logs.list_uploads(job_id='job-a')
    assert data['total'] == 1  # Moderation record and attempt represent one upload.
    result = data['rows'][0]
    assert (result['salesperson'], result['group_name'], result['title']) == ('上传业务员', '墨西哥组', '真实链接名')
    assert result['thumbnail_url'] == 'https://example.com/main.jpg'
    client.get_marketplace_item.side_effect = ValueError('店铺授权失效')
    with pytest.raises(ValueError):
        bit_store_link_video.upload_store_link_video(42, file(), actor='小李')
    assert logs.list_uploads()['total'] == 2
    assert logs.list_uploads(salesperson='小李')['rows'][0]['message'] == '店铺授权失效'


def test_unknown_legacy_actor_is_not_replaced_with_store_owner():
    video_reviews.record_upload({'id': 2, 'token_id': 7, 'site_id': 'MLM', 'item_id': 'MLM2'},
                               {'clip_uuid': 'legacy'}, 'CBT2', 'remote')
    assert not logs.list_uploads()['rows'][0].get('salesperson')


def test_retries_in_same_second_are_separate_and_manifest_is_not_duplicated(tmp_path, monkeypatch):
    monkeypatch.setattr(video_reviews, '_now', lambda: '2026-10-10T02:00:00Z')
    details = {'job_id': 'retry-job', 'link_id': 42, 'token_id': 7, 'site_id': 'MLM',
               'item_id': 'MLM42', 'started_at': '2026-10-10T02:00:00Z', 'salesperson': '小张'}
    first = logs.begin_attempt(details)
    first.update(status='failed', message='平台拒绝')
    logs.save_attempt(first)
    second = logs.begin_attempt(details)
    second.update(status='failed', message='再次拒绝')
    logs.save_attempt(second)
    folder = tmp_path / 'videos' / 'retry-job'
    folder.mkdir(parents=True)
    (folder / 'job.json').write_text(json.dumps({'id': 'retry-job', 'publish_attempts': [first, second]}))
    assert logs.list_uploads()['total'] == 2


def test_proxy_keeps_empty_store_scope(monkeypatch):
    from bit import bit_db_api
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    remote = Mock(return_value={'rows': []})
    monkeypatch.setattr(bit_db_api, '_request', remote)
    bit_db_api.list_video_upload_logs(page=2, allowed_token_ids=[])
    assert remote.call_args.kwargs['params'] == {'page': 2, 'token_ids': ''}


def test_public_route_uses_server_scope_and_ignores_caller_scope():
    # Load the actual route definitions without starting the application's workers.
    source = Path(__file__).resolve().parents[1] / 'bit/bit_interface.py'
    tree = ast.parse(source.read_text())
    names = {'_video_upload_log_filters', 'api_video_upload_logs'}
    definitions = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[])
    app = Flask(__name__)
    submit = Mock(return_value={'rows': [], 'total': 0})
    namespace = {'app': app, 'login_required': lambda fn: fn, 'request': request, 'jsonify': jsonify,
                 'logging': __import__('logging'), '_parse_int_param': lambda args, key, default, *_: int(args.get(key, default)),
                 '_authorized_token_ids_for_user': lambda: {7}, 'bit_db_api': SimpleNamespace(list_video_upload_logs=submit)}
    exec(compile(definitions, str(source), 'exec'), namespace)
    response = app.test_client().get('/api/ai-videos/upload-logs?token_ids=8&allowed_token_ids=8&search=MLM')
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert submit.call_args.kwargs['allowed_token_ids'] == {7}
    assert submit.call_args.kwargs['search'] == 'MLM'
