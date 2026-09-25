import json
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path.cwd()
script = root / 'browser_extension/zeshun_collector/content-1688.js'
results = {}
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless=True)
    page = browser.new_page()
    page.route('https://www.1688.com/**', lambda route: route.fulfill(content_type='text/html', body='''
    <meta charset="utf-8"><section><input id="picture" type="file" hidden>
    <button id="submit">搜索图片</button></section>
    <script>
    window.events=[];window.accepted=0;
    picture.onchange=e=>events.push({kind:'file',name:picture.files[0].name,type:picture.files[0].type});
    submit.onclick=e=>{events.push({kind:'click',trusted:e.isTrusted,active:navigator.userActivation.isActive});if(e.isTrusted)accepted++};
    </script>'''))
    page.goto('https://www.1688.com/')
    page.evaluate('window.chrome={runtime:{onMessage:{addListener:l=>window.listener=l},sendMessage:async()=>({ok:true})}}')
    page.add_script_tag(path=str(script))
    cdp=page.context.new_cdp_session(page)
    result=cdp.send('Runtime.evaluate', {'expression': '''new Promise(resolve=>listener({type:'AI_WEIGHT_PRICE_SEARCH',data_url:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII=',timeout_ms:3000},null,resolve))''','awaitPromise':True,'returnByValue':True,'userGesture':False})
    results['extension_response']=result['result'].get('value')
    results['extension_events']=page.evaluate('events')
    results['extension_accepted']=page.evaluate('accepted')
    page.locator('#submit').click()
    results['playwright_click_event']=page.evaluate('events[events.length-1]')
    results['accepted_after_playwright_click']=page.evaluate('accepted')
    page.set_content('''<div style="position:absolute;top:10px"><input id="visible" type="file" hidden></div><div style="position:absolute;top:5000px;width:50px;height:50px"><input id="offscreen" type="file" hidden></div>''')
    results['active_inputs_extension_algorithm']=page.evaluate('''[...document.querySelectorAll('input')].filter(input=>{let visibleRegion=false;for(let p=input.parentElement;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden')return false;const b=p.getBoundingClientRect();if(b.width>0&&b.height>0&&b.bottom>0&&b.right>0&&b.top<innerHeight&&b.left<innerWidth)visibleRegion=true;}return visibleRegion}).map(e=>e.id)''')
    browser.close()
print(json.dumps(results,ensure_ascii=False,indent=2))
