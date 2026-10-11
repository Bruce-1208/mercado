"""SKU photos live on specification options, not necessarily on skuItems."""
from test_zeshun_popup_browser import browser, supplier_page, supplier_extract


def test_option_images_are_joined_by_value_not_order(supplier_page):
    supplier_page.evaluate("""() => {
      document.querySelector('#skuSelection').__reactFiber$test.memoizedProps.skuProps = [
        {name:'颜色', values:[
          {name:'蓝色', imageUrl:'//cbu01.alicdn.com/blue.jpg_100x100.jpg'},
          {name:'红色', imageUrl:'//cbu01.alicdn.com/red.jpg_100x100.jpg'}]}];
    }""")
    rows = supplier_extract(supplier_page)['product']['variations']
    assert [r['image_url'] for r in rows] == ['https://cbu01.alicdn.com/red.jpg', 'https://cbu01.alicdn.com/blue.jpg']


def test_dom_option_images_and_shared_color_sizes(supplier_page):
    supplier_page.evaluate("""() => {
      const el = document.querySelector('#skuSelection');
      el.innerHTML = '<img alt="红色" src="https://cbu01.alicdn.com/red.jpg">';
      el.__reactFiber$test.memoizedProps.skuItems.push({skuId:'33', specAttrs:'颜色:红色&gt;尺寸:大号',price:'15'});
    }""")
    rows = supplier_extract(supplier_page)['product']['variations']
    assert rows[0]['image_url'] == rows[2]['image_url'] == 'https://cbu01.alicdn.com/red.jpg'
    assert not rows[1].get('image_url')


def test_conflicting_option_images_are_not_guessed(supplier_page):
    supplier_page.evaluate("""() => {
      document.querySelector('#skuSelection').innerHTML = '<img alt="红色" src="https://cbu01.alicdn.com/one.jpg"><img alt="红色" src="https://cbu01.alicdn.com/two.jpg">';
    }""")
    assert not supplier_extract(supplier_page)['product']['variations'][0].get('image_url')
