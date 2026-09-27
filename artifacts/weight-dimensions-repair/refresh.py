import json
import time
from pathlib import Path
from collections import Counter
from bit import weight_dimensions_records as records, bit_db_api
out = Path('artifacts/weight-dimensions-repair/result.json')
tokens = bit_db_api.list_mercado_store_tokens()
rows = tokens.get('rows', [])
allowed = {int(row['id']) for row in rows if row.get('id') is not None}
result = records.start_full_refresh('system:weight-dimensions-repair', rows, allowed)
while True:
    with records._lock:
        task = records._tasks[result['task_id']]
        summary = {key: task.get(key) for key in ('task_id','status','message','total','processed','finished_at')}
        summary['query_errors'] = dict(Counter(row.get('query_error','') for row in task.get('records',[]) if row.get('query_status') == '查询失败'))
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "query_errors"}, ensure_ascii=False), flush=True)
    if summary['status'] in {'ready', 'failed'}:
        break
    time.sleep(15)
