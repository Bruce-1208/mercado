"""Offline price extraction regressions; all supplier requests are mocked."""
import pytest
from test_zeshun_popup_browser import browser, supplier_page, supplier_extract


def clear_sku_prices(page):
    page.evaluate("""() => {
      for (const row of document.querySelector('#skuSelection').__reactFiber$test.memoizedProps.skuItems) {
        delete row.price; delete row.discountPrice;
      }
    }""")


@pytest.mark.parametrize('markup,expected', [
    ('<span class="price-label">批发价</span><span class="price-currency">¥</span><span class="price-value">18.50</span><span>2件起批</span>', 18.5),
    ('<span class="price-value"><span>18</span><span> . </span><span>50</span></span><span>100件起批</span>', 18.5),
    ('<span class="price-value">¥ 18.50～25.00</span><span>2件起批</span>', 18.5),
    ('<del class="price-value">¥99</del><span class="price-value">¥18.50</span>', 18.5),
])
def test_offer_price_fallback_from_all_price_nodes(supplier_page, markup, expected):
    clear_sku_prices(supplier_page)
    supplier_page.evaluate("html => { const n=document.createElement('div'); n.id='productPrice'; n.innerHTML=html; document.body.append(n); }", markup)
    product = supplier_extract(supplier_page)['product']
    assert product['price'] == expected
    assert product['scrape_status'] == 'partial'
    assert '采购价' in product['error_message']
    assert all(row['price'] is None for row in product['variations'])


def test_structured_sku_prices_ignore_quantity_and_discount(supplier_page):
    supplier_page.evaluate("""() => {
      const rows=document.querySelector('#skuSelection').__reactFiber$test.memoizedProps.skuItems;
      rows[0].price={beginAmount:100,discount:0.8,amount:'12.50'};
      delete rows[1].price; rows[1].salePrice={quantity:200,price:'18.00'};
    }""")
    product = supplier_extract(supplier_page)['product']
    assert product['price'] == 12.5
    assert [v['price'] for v in product['variations']] == [12.5,18]


def test_offer_react_tiers_read_only_amount_fields(supplier_page):
    clear_sku_prices(supplier_page)
    supplier_page.evaluate("""() => {
      const n=document.createElement('div');n.id='productPrice';document.body.append(n);
      n.__reactFiber$test={memoizedProps:{priceRanges:[
        {beginAmount:2,price:'18.5'}, {beginAmount:100,price:{amount:'16.0'}}
      ]}};
    }""")
    product = supplier_extract(supplier_page)['product']
    assert product['price'] == 16
    assert all(v['price'] is None for v in product['variations'])


def test_missing_react_price_uses_dom_sku_identity(supplier_page):
    clear_sku_prices(supplier_page)
    supplier_page.evaluate("""() => {
      document.querySelector('#skuSelection').innerHTML='<table class="sku-table"><thead><tr><th>规格</th><th>价格</th></tr></thead><tbody><tr data-sku-id="22"><td>大号</td><td>18.00</td></tr><tr data-sku-id="11"><td>小号</td><td>12.50</td></tr></tbody></table>';
    }""")
    product = supplier_extract(supplier_page)['product']
    assert [v['price'] for v in product['variations']] == [12.5,18]


def test_delayed_price_retries_after_skus_already_loaded(supplier_page):
    clear_sku_prices(supplier_page)
    supplier_page.evaluate("""() => {
      setTimeout(() => {
        const rows=document.querySelector('#skuSelection').__reactFiber$test.memoizedProps.skuItems;
        rows[0].price='12.50';rows[1].price='18.00';
      },500);
    }""")
    assert supplier_extract(supplier_page)['product']['price'] == 12.5


def test_missing_price_is_not_saved_as_success(supplier_page):
    clear_sku_prices(supplier_page)
    # Advance the deadline after the first attempt to avoid a ten-second test.
    supplier_page.evaluate("""() => {
      const now=Date.now;let reads=0;Date.now=()=>now()+(reads++ > 1 ? 20000 : 0);
      const n=document.createElement('div');n.id='productPrice';n.textContent='登录后查看价格，100件起批';document.body.append(n);
    }""")
    result = supplier_extract(supplier_page)
    assert result['ok'] is False
    assert '采购价' in result['error']
    assert 'product' not in result


@pytest.mark.parametrize('has_offer_price', [False, True])
def test_delayed_partial_sku_prices_are_waited_for(supplier_page, has_offer_price):
    supplier_page.evaluate("""hasOffer => {
      const rows=document.querySelector('#skuSelection').__reactFiber$test.memoizedProps.skuItems;
      delete rows[1].price;
      if (hasOffer) {
        delete rows[0].price;
        const node=document.createElement('div');node.id='productPrice';
        node.innerHTML='<span class="price-value">¥10.00</span>';document.body.append(node);
      }
      setTimeout(()=>{rows[0].price='12.50';rows[1].price='18.00';},500);
    }""", has_offer_price)
    product = supplier_extract(supplier_page)['product']
    assert [row['price'] for row in product['variations']] == [12.5, 18]
    assert product['price'] == 12.5
    assert '采购价' not in product['error_message']


def test_empty_primary_price_does_not_hide_valid_amount(supplier_page):
    clear_sku_prices(supplier_page)
    supplier_page.evaluate("""() => {
      const node=document.createElement('div');node.id='productPrice';document.body.append(node);
      node.__reactFiber$test={memoizedProps:{priceRanges:[{price:'',priceAmount:'18.50'}]}};
    }""")
    assert supplier_extract(supplier_page)['product']['price'] == 18.5
