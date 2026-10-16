from pathlib import Path
import json
from playwright.sync_api import sync_playwright
out=Path(__file__).resolve().parent
with sync_playwright() as p:
 b=p.chromium.launch(executable_path='/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',headless=True)
 checks=[]
 for width in [1440,390]:
  page=b.new_page(viewport={'width':width,'height':1000});errors=[];page.on('pageerror',lambda error: errors.append(str(error)))
  page.route('**/*',lambda route: route.fulfill(path=str(out/'report.html'),content_type='text/html'))
  page.goto('http://audit-report.test/',wait_until='load');page.screenshot(path=str(out/f'report-{width}.png'))
  checks.append({'width':width,'document_width':page.evaluate('document.documentElement.scrollWidth'),'title':page.title(),'errors':errors,'tables':page.locator('table').count()})
  assert checks[-1]['document_width'] <= width and not errors
  page.close()
 b.close();(out/'report-qa.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2));print(checks)