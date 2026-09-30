from unittest.mock import Mock
import pytest
from bit import video_reviews as reviews

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('BIT_STORE_LINK_MARKER_DB_PATH', str(tmp_path / 'markers.sqlite3'))


def test_review_matches_site_item_and_clip_and_runs_daily(monkeypatch):
    from bit import bit_mysql, bit_store_link_sync
    row = {'id': 2, 'token_id': 3, 'site_id': 'MLM', 'item_id': 'MLM1', 'store_name': '测试店铺'}
    reviews.record_upload(row, {'clip_uuid': 'clip'}, 'CBT1', 'remote')
    assert reviews.records()[0]['status_label'] == '已提交待审核'
    client = Mock()
    client.request.return_value = {'clips': [{'clip_uuid': 'clip', 'metadata': [
        {'item_id': 'MLB1', 'site_id': 'MLB', 'logistic_type': 'remote', 'moderation_status': 'PUBLISHED'},
        {'item_id': 'MLM1', 'site_id': 'MLM', 'logistic_type': 'remote', 'moderation_status': 'UNDER_REVIEW'}]}]}
    monkeypatch.setattr(bit_mysql, 'get_mercado_store_token', lambda _: {'id': 3})
    monkeypatch.setattr(bit_store_link_sync, '_client_and_token', lambda _: (client, {}))
    reviews.sync_pending()
    reviews.sync_pending()
    assert client.request.call_count == 1
    assert reviews.records()[0]['status'] == 'UNDER_REVIEW'
    with reviews.database() as db:
        db.execute("UPDATE video_upload_history SET checked_at=''")
    client.request.return_value['clips'][0]['metadata'][1]['moderation_status'] = 'PUBLISHED'
    reviews.sync_pending()
    assert reviews.records()[0]['status_label'] == '视频审核通过'
    reviews.sync_pending()
    assert client.request.call_count == 2


def test_api_failure_keeps_pending(monkeypatch):
    from bit import bit_mysql
    reviews.record_upload({'token_id': 3, 'site_id': 'MLM', 'item_id': 'MLM1'}, {'clip_uuid': 'clip'}, 'CBT1', 'remote')
    monkeypatch.setattr(bit_mysql, 'get_mercado_store_token', Mock(side_effect=RuntimeError('授权失效')))
    reviews.sync_pending()
    row = reviews.records()[0]
    assert row['status'] == 'UNDER_REVIEW'
    assert row['error'] == '授权失效'


def test_video_page_capped_at_twenty(tmp_path, monkeypatch):
    import json
    from bit import bit_ai_video as video
    monkeypatch.setattr(video, 'storage_root', lambda: tmp_path)
    monkeypatch.setattr(video, 'provider_settings', lambda _: {})
    for number in range(25):
        folder = tmp_path / str(number)
        folder.mkdir()
        (folder / 'job.json').write_text(json.dumps({'id': str(number), 'status': 'succeeded', 'created_at': '2026-09-29T00:00:00+00:00'}))
    page = video.list_jobs(limit=100)
    assert len(page['rows']) == 20
    assert page['total_pages'] == 2
    assert len(video.list_jobs(limit=100, page=2)['rows']) == 5


@pytest.mark.parametrize('status', ['REJECTED', 'TRANSCODING_REJECTED'])
def test_rejected_review_is_terminal(monkeypatch, status):
    from bit import bit_mysql, bit_store_link_sync
    reviews.record_upload({'token_id': 3, 'site_id': 'MLM', 'item_id': 'MLM1'}, {'clip_uuid': 'clip'}, 'CBT1', 'remote')
    client = Mock()
    client.request.return_value = {'clips': [{'clip_uuid': 'clip', 'metadata': [{
        'item_id': 'MLM1', 'site_id': 'MLM', 'logistic_type': 'remote',
        'moderation_status': status, 'moderation_reasons': {'reason': 'invalid content'},
    }]}]}
    monkeypatch.setattr(bit_mysql, 'get_mercado_store_token', lambda _: {'id': 3})
    monkeypatch.setattr(bit_store_link_sync, '_client_and_token', lambda _: (client, {}))
    reviews.sync_pending()
    reviews.sync_pending()
    row = reviews.records()[0]
    assert row['status'] == status
    assert '视频审核不通过' in row['status_label']
    assert 'invalid content' in row['error']
    assert client.request.call_count == 1


def test_public_job_review_does_not_mix_store_accounts():
    from bit import bit_ai_video as video
    for token in (3, 4):
        reviews.record_upload({'token_id': token, 'site_id': 'MLM', 'item_id': 'MLM1'}, {'clip_uuid': 'clip'}, 'CBT1', 'remote')
    with reviews.database() as db:
        db.execute("UPDATE video_upload_history SET status='PUBLISHED' WHERE token_id=4")
    job = video._public_job({'id': 'test', 'published': [
        {'token_id': 4, 'site_id': 'MLM', 'item_id': 'MLM1', 'clip_uuid': 'clip', 'salesperson': '张三'},
    ]})
    assert job['published'][0]['status_label'] == '视频审核通过'
    assert job['published'][0]['salesperson'] == '张三'
