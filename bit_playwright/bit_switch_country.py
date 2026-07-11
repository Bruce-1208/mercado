import time

from bit_playwright.common import SITE_COUNTRY_MAP, deep_click


def force_select_country(page, country_name):
    country = SITE_COUNTRY_MAP.get(str(country_name or "").strip(), str(country_name or "").strip())
    script = """
    targetText => {
      function deepSearchAndClick(root) {
        const items = root.querySelectorAll('li');
        for (const item of items) {
          const title = item.querySelector('[data-andes-listbox-title="true"]');
          const hasFullIcon = item.querySelector('svg') !== null;
          if (title && title.textContent.trim() === targetText) {
            if (hasFullIcon) continue;
            item.scrollIntoView({block: 'center', inline: 'center'});
            item.click();
            return true;
          }
        }
        for (const node of root.querySelectorAll('*')) {
          if (node.shadowRoot && deepSearchAndClick(node.shadowRoot)) return true;
        }
        return false;
      }
      return deepSearchAndClick(document);
    }
    """
    try:
        success = page.evaluate(script, country)
        if success:
            print(f"成功选择站点: {country}")
        else:
            print(f"未找到站点: {country}")
        return success
    except Exception as exc:
        print(f"选择站点异常: {exc}")
        return False


def oepn_country_switch(page):
    return deep_click(page, 'button[aria-label="Select country"]')


def select_country(page, site, retries=3):
    country = SITE_COUNTRY_MAP.get(str(site or "").strip(), str(site or "").strip())
    for attempt in range(1, retries + 1):
        try:
            oepn_country_switch(page)
            time.sleep(1)
            if force_select_country(page, country):
                time.sleep(2)
                return True
        except Exception as exc:
            print(f"切换站点失败 {site}/{country}, 第 {attempt} 次: {exc}")
        time.sleep(2)
    return False
