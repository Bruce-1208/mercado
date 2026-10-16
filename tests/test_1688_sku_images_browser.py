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


def test_images_in_parent_props_are_not_cut_off_by_partial_child_images(supplier_page):
    supplier_page.evaluate("""() => {
      const fiber = document.querySelector('#skuSelection').__reactFiber$test;
      fiber.memoizedProps.skuProps = [{name:'红色', imageUrl:'//cbu01.alicdn.com/red.jpg'}];
      fiber.return = {memoizedProps: {skuProps: [
        {name:'红色', imageUrl:'//cbu01.alicdn.com/red.jpg'},
        {name:'蓝色', imageUrl:'//cbu01.alicdn.com/blue.jpg'}]}};
    }""")
    rows = supplier_extract(supplier_page)['product']['variations']
    assert [r['image_url'] for r in rows] == ['https://cbu01.alicdn.com/red.jpg', 'https://cbu01.alicdn.com/blue.jpg']


def test_direct_sku_image_url_and_empty_alias(supplier_page):
    supplier_page.evaluate("""() => {
      const items = document.querySelector('#skuSelection').__reactFiber$test.memoizedProps.skuItems;
      items[0].imageUrl = '';
      items[0].image_url = '//cbu01.alicdn.com/red.jpg';
    }""")
    assert supplier_extract(supplier_page)['product']['variations'][0]['image_url'] == 'https://cbu01.alicdn.com/red.jpg'


def test_all_gallery_and_description_photos_are_collected(supplier_page):
    supplier_page.evaluate("""() => {
      const detail = document.createElement('div'); detail.id = 'desc-lazyload-container';
      detail.innerHTML = Array.from({length: 30}, (_, i) => `<img data-src="https://cbu01.alicdn.com/detail-${i}.jpg">`).join('');
      document.body.appendChild(detail);
    }""")
    images = supplier_extract(supplier_page)['product']['images']
    assert len(images) >= 30
    assert 'https://cbu01.alicdn.com/detail-29.jpg' in images


def test_nested_option_images_join_by_property_value_id_and_keep_each_cost(supplier_page):
    supplier_page.evaluate("""() => {
      const props = document.querySelector('#skuSelection').__reactFiber$test.memoizedProps;
      props.skuItems[0].pvs = '123:10;456:20'; props.skuItems[1].pvs = '123:11;456:21';
      props.skuProps = [{values:[
        {propId:'123',valueId:'11',skuImage:{url:'//cbu01.alicdn.com/blue.jpg'}},
        {propId:'123',valueId:'10',skuImage:{url:'//cbu01.alicdn.com/red.jpg'}}]}];
    }""")
    rows = supplier_extract(supplier_page)['product']['variations']
    assert [r['image_url'] for r in rows] == ['https://cbu01.alicdn.com/red.jpg', 'https://cbu01.alicdn.com/blue.jpg']
    assert [r['net_proceeds_usd'] for r in rows] == [3, 4]
    assert rows[0]['available_quantity'] == 0


def test_direct_sku_gallery_preserves_main_and_secondary_order(supplier_page):
    supplier_page.evaluate("""() => {
      document.querySelector('#skuSelection').__reactFiber$test.memoizedProps.skuItems[0].images =
        [{url:'//cbu01.alicdn.com/red.jpg'}, {url:'//cbu01.alicdn.com/detail.jpg'}];
    }""")
    row = supplier_extract(supplier_page)['product']['variations'][0]
    assert row['images'] == ['https://cbu01.alicdn.com/red.jpg','https://cbu01.alicdn.com/detail.jpg']
    assert row['image_status'] == 'confirmed'
