import json
import time
from pathlib import Path
from erp import mercadolibre_store_link_store as store

results = []
# Schema migration has already completed before running this verifier.
for field in ('sold_quantity', *store.STORE_LINK_SORT_INDEXES):
    for direction in ('desc', 'asc'):
        started = time.monotonic()
        data = store.list_store_links(include_categories=False, sort_by=field,
                                      sort_order=direction, page_size=50)
        rows = data['rows']
        fields = list(dict.fromkeys((field, 'sold_quantity', 'last_synced_at', 'id')))
        keys = [tuple((row.get(name) is not None, row.get(name)) for name in fields)
                for row in rows]
        assert keys == sorted(keys, reverse=direction == 'desc'), (field, direction)
        assert len({row['id'] for row in rows}) == len(rows) == 50
        result = dict(sort=field, direction=direction,
                      seconds=round(time.monotonic()-started, 3), rows=len(rows))
        results.append(result)
        print(result, flush=True)
Path('artifacts/store-link-speed-20260927/verified-benchmark.json').write_text(
    json.dumps(results, indent=2) + '\n')
