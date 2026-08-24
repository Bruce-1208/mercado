"""Read a Mercado Libre public listing through an existing BitBrowser profile.

This is a fallback for third-party listings hidden by the Items API policy.  It
opens a temporary tab, extracts only public product-page data, then closes the
tab and (by default) the BitBrowser window it opened.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from bit.bit_api import closeBrowser, releaseBrowserLease
from bit.bit_mercado_login import _connect_to_open_bit_browser


EXTRACT_PAGE_SCRIPT = r"""
return {
  url: location.href,
  title: document.title,
  metas: Array.from(document.querySelectorAll('meta'))
    .filter((node) => {
      const key = node.getAttribute('property') || node.getAttribute('name');
      return [
        'og:title', 'og:image', 'og:description',
        'product:price:amount', 'product:price:currency',
        'twitter:title', 'twitter:image'
      ].includes(key);
    })
    .map((node) => ({
      key: node.getAttribute('property') || node.getAttribute('name'),
      value: node.content
    })),
  jsonld: Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
    .map((node) => node.textContent),
  h1: Array.from(document.querySelectorAll('h1')).map((node) => node.innerText),
  body: (document.body && document.body.innerText || '').slice(0, 30000),
  images: Array.from(document.images)
    .map((node) => ({src: node.currentSrc || node.src, alt: node.alt}))
    .filter((entry) => entry.src)
    .slice(0, 100)
};
"""


def inspect_source_page(
    window_id: str,
    source_url: str,
    *,
    settle_seconds: float = 5,
    close_window: bool = True,
    handoff: bool = False,
) -> dict[str, Any]:
    driver = None
    original_handle = None
    temporary_tab = False
    try:
        driver = _connect_to_open_bit_browser(window_id, page_load_timeout=35)
        original_handle = driver.current_window_handle
        driver.switch_to.new_window("tab")
        temporary_tab = True
        try:
            driver.get(source_url)
        except Exception:
            try:
                driver.execute_cdp_cmd("Page.stopLoading", {})
            except Exception:
                pass
        time.sleep(max(float(settle_seconds), 0))
        return dict(driver.execute_script(EXTRACT_PAGE_SCRIPT) or {})
    finally:
        if driver is not None and temporary_tab and not handoff:
            try:
                driver.close()
            except Exception:
                pass
            if original_handle:
                try:
                    driver.switch_to.window(original_handle)
                except Exception:
                    pass
        if close_window and not handoff:
            closeBrowser(window_id)
        else:
            releaseBrowserLease(window_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="读取 Mercado Libre 公开商品页")
    parser.add_argument("--window-id", required=True)
    parser.add_argument("source_url")
    parser.add_argument("--settle-seconds", type=float, default=5)
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument(
        "--handoff",
        action="store_true",
        help="保留商品页标签和浏览器窗口，交给用户完成账号验证",
    )
    args = parser.parse_args(argv)
    result = inspect_source_page(
        args.window_id,
        args.source_url,
        settle_seconds=args.settle_seconds,
        close_window=not args.keep_open,
        handoff=args.handoff,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    raise SystemExit(main())
