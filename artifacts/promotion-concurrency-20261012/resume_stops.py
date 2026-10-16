"""Resume only the user's captured stop requests after the approved restart."""
from pathlib import Path
import sys,json
from concurrent.futures import ThreadPoolExecutor
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from bit.mercado_promotions import stop_campaign
from erp.mercadolibre_promotion_store import PromotionStore
from bit.bit_runtime_lock import InterProcessLock

manifest=json.loads(Path(__file__).with_name('resume-manifest.json').read_text())
lease=InterProcessLock('promotion-concurrency-20261012-resume',owner='approved-stop-resume')
if not lease.acquire(timeout=0):raise SystemExit('Recovery already running')
def resume(row):
    store=PromotionStore()
    old=store.list_jobs(job_ids=[row['job_id']])
    if not old or old[0]['promotion_fk']!=row['promotion_fk'] or old[0]['actor']!=row['actor']:
        return {'promotion_fk':row['promotion_fk'],'status':'scope_mismatch'}
    if old[0]['status']=='succeeded':return {'promotion_fk':row['promotion_fk'],'status':'already_completed'}
    try:
        outcome=stop_campaign(promotion_fk=row['promotion_fk'],actor=row['actor'],store=store,background=False)
        job=store.list_jobs(job_ids=[outcome['job_id']],summary_only=True)[0]
        return {k:job.get(k) for k in ('job_id','promotion_fk','status','total_count','success_count','failed_count','unknown_count')}
    except Exception as exc:
        return {'promotion_fk':row['promotion_fk'],'status':'not_started','message':str(exc)}
try:
    print(json.dumps({'event':'started','campaigns':len(manifest)},ensure_ascii=False),flush=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures=[pool.submit(resume,row) for row in manifest]
        outcomes=[]
        from concurrent.futures import as_completed
        for future in as_completed(futures):
            result=future.result();outcomes.append(result)
            print(json.dumps(result,ensure_ascii=False),flush=True)
            Path(__file__).with_name('resume-results.json').write_text(json.dumps(outcomes,ensure_ascii=False,indent=2))
finally:
    lease.release()
