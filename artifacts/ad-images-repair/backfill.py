from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from bit.bit_ad_analysis import _load_snapshot, _persist_snapshot, _enrich_images
from bit.bit_store_link_sync import _client_and_token
from bit import bit_mysql

def run(token_id, rows):
    try:
        token = bit_mysql.get_mercado_store_token(token_id)
        client = _client_and_token(token)[0]
        _enrich_images(client, rows)
        return rows
    except Exception as exc:
        print('failed', token_id, type(exc).__name__, flush=True)
        return rows

if __name__ == '__main__':
    groups = defaultdict(list)
    for row in _load_snapshot()['links']:
        if not row.get('thumbnail_url'):
            groups[row['token_id']].append(row)
    images = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        tasks = [pool.submit(run, token, rows) for token, rows in groups.items()]
        for task in as_completed(tasks):
            for row in task.result():
                if row.get('thumbnail_url'):
                    images[(row['token_id'], row['item_id'])] = row['thumbnail_url']
            print('recovered', len(images), flush=True)
    snapshot = _load_snapshot()
    for row in snapshot['links']:
        if not row.get('thumbnail_url'):
            row['thumbnail_url'] = images.get((row['token_id'], row['item_id']), '')
    print('saved', _persist_snapshot(snapshot), 'images', sum(bool(row.get('thumbnail_url')) for row in snapshot['links']), flush=True)
