from test_zeshun_category_browser import popup


def test_popup_remembers_price_and_freezes_batch_choice(popup, tmp_path):
    select = popup.locator('#product-price-mode')
    assert select.input_value() == 'original'
    popup.screenshot(path=str(tmp_path / 'mercado-price-popup.png'), full_page=True)
    select.select_option('discount')
    popup.wait_for_function("saved.productBatchOptions['price-mode'] === 'discount'")
    popup.evaluate("document.querySelector('#product-price-mode').value = 'original'; initializeProductBatch()")
    popup.wait_for_function("document.querySelector('#product-price-mode').value === 'discount'")
    popup.locator('#product-tab').select_option('11')
    popup.locator('#product-batch-start').click()
    popup.wait_for_function("messages.some(m => m.type === 'START_PRODUCT_BATCH')")
    assert popup.evaluate("messages.find(m => m.type === 'START_PRODUCT_BATCH').params.price_mode") == 'discount'
    assert select.is_disabled()
