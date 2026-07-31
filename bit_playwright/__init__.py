"""
Playwright-based automation package for MercadoApp.

Browser automation modules live here. Non-browser infrastructure such as
BitBrowser API, database, email and utility helpers are re-exported from
``bit`` to keep one source of truth.
"""

from bit.mercado_click_delay import install_playwright_click_delay


install_playwright_click_delay()
