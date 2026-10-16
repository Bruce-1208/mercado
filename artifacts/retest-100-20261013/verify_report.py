import json
from pathlib import Path
from playwright.sync_api import sync_playwright
OUT=Path(__file__).resolve().parent
checks=[]
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',headless=True)
 for width in (1440,390):
  page=browser.new_page(viewport={'width':width,'height':1000});errors=[]
  page.on('pageerror',lambda err:errors.append(str(err)))
  page.goto((OUT/'report.html').as_uri());page.screenshot(path=str(OUT/f'report-{width}.png'))
  assert page.locator('h1').inner_text()=='并发 100 复测与功能检查'
  assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
  assert not errors
  checks.append({'width':width,'no_horizontal_overflow':True,'page_errors':errors})
  page.close()
 browser.close()
(OUT/'report-qa.json').write_text(json.dumps(checks,indent=2));print(checks)
