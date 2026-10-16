"""Supplier link remains clickable within fixed-height product tiles."""
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 1024, 390])
@pytest.mark.parametrize('platform', ['mercadolibre', 'amazon'])
def test_source_link_is_not_covered_by_next_card_section(console_page, width, platform):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate('''platform => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        document.getElementById('ai-original-platform').value = platform;
        aiOriginalRows = [{id:1, source_item_id:'1688123456789', ai_status:'pending',
          title:'长标题'.repeat(50), price:29, visibility:'private', can_operate:true,
          original_1688:{title:'长标题'.repeat(50), source_url:'https://detail.1688.com/offer/1688123456789.html',
            variations:Array.from({length:12}, (_, i) => ({sku_id:String(i)}))}}];
        renderAiOriginalProducts(); decorateAiOriginalWorkbench();
    }''', platform)
    card = page.locator('#ai-original-grid > .ai-original-card')
    link = card.get_by_role('link', name='打开 1688 详情页 ↗')
    link.scroll_into_view_if_needed()
    assert link.is_visible()
    link.click(trial=True)  # Wait for content-visibility to paint after scrolling.
    assert link.evaluate('''el => {
        const r=el.getBoundingClientRect(), source=el.closest('.ai-original-source').getBoundingClientRect();
        const top=document.elementFromPoint(r.x+r.width/2, r.y+r.height/2);
        return el.contains(top) && r.top >= source.top && r.bottom <= source.bottom + 1;
    }''')
    assert link.get_attribute('href') == 'https://detail.1688.com/offer/1688123456789.html'
    assert link.get_attribute('target') == '_blank'
    link.focus()
    assert link.evaluate('(el) => document.activeElement === el')
    # Intercept navigation so this checks the action without visiting 1688.
    link.evaluate("el => el.addEventListener('click', event => {event.preventDefault(); window.sourceLinkClicked=true;})")
    link.press('Enter')
    assert page.evaluate('window.sourceLinkClicked')
    page.evaluate('toggleAllAiOriginalProducts(true)')
    assert page.evaluate('[...aiOriginalSelected]') == [1]
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    if platform == 'mercadolibre':
        destination = ROOT / 'artifacts' / 'ai-original-source-link'
        destination.mkdir(parents=True, exist_ok=True)
        card.screenshot(path=str(destination / f'source-link-{width}.png'))
