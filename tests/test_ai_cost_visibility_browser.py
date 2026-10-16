"""Offline real-template regression for the reported SKU failure log."""
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('role', ['super_admin', 'enterprise_admin', 'member'])
def test_sku_error_cost_visibility(console_page, width, role):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate('''role => {
      currentWorkbenchUser.role_key = role;
      document.querySelectorAll('.tab-page').forEach(e => e.classList.remove('active'));
      document.getElementById('tab-ai-original-products').classList.add('active');
      const message = '24 个 SKU 缺少采集原图，请重新采集或补充图片链接 · 模型计费估算 ¥0.013195（含已记录尝试） · 累计 60,318 Token · 金额不限额';
      renderAiOriginalTaskStatus({status:'error',message:'生成结束',requested_count:1,processed_count:1,failed_count:1,
        results:[{status:'failed',product_id:16943431,message,generation_budget:{spent_yuan:0.013195,total_tokens:60318}}]});
    }''', role)
    log = page.locator('#ai-original-task-log')
    log.scroll_into_view_if_needed()
    text = log.text_content()
    assert '产品 16943431：24 个 SKU 缺少采集原图' in text
    for marker in ['Token', '¥0.013195', '金额不限额']:
        assert (marker in text) == (role == 'super_admin')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = ROOT / 'artifacts/ai-cost-privacy-20261012'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'{role}-{width}.png'))
