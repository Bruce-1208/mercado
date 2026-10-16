import sys,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_console_ui_browser import console_browser,console_page
out=Path(__file__).parent
phase=sys.argv[1] if len(sys.argv)>1 else 'before'
bg=console_browser.__wrapped__(); browser=next(bg)
pg=console_page.__wrapped__(browser); page=next(pg)
results=[]
for width in [1440,1024,390]:
 page.set_viewport_size({'width':width,'height':900})
 tabs=page.locator('.tab-page').evaluate_all('(els)=>els.map(el=>({id:el.id,title:el.querySelector("h2")?.textContent}))')
 for tab in tabs:
  page.evaluate('id=>{document.querySelectorAll(".tab-page").forEach(el=>el.classList.toggle("active",el.id===id));window.scrollTo(0,0)}',tab['id'])
  page.wait_for_timeout(40)
  measurement=page.evaluate('''() => ({overflow:document.documentElement.scrollWidth>innerWidth+1, width:document.documentElement.scrollWidth, offenders:[...document.querySelectorAll('.tab-page.active *')].filter(el=>{const r=el.getBoundingClientRect(); return r.width>0 && r.right>innerWidth+2 && !el.closest('.zs-table-scroll,.table-wrap,.table-scroll,.order-table-wrap,.store-link-table-wrap')}).slice(0,8).map(el=>({tag:el.tagName,cls:el.className,id:el.id,width:Math.round(el.getBoundingClientRect().width)}))})''')
  results.append({'viewport':width,**tab,**measurement})
  if width in [1440,390]: page.screenshot(path=str(out/f'{phase}-{width}-{tab["id"]}.png'))
(out/f'{phase}.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
print(json.dumps({'pages':len(tabs),'checks':len(results),'overflow':[r for r in results if r['overflow']]},ensure_ascii=False))
try:next(pg)
except StopIteration:pass
try:next(bg)
except StopIteration:pass
