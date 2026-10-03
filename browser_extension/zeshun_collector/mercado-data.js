(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.ZeshunMercadoData = api;
})(globalThis, function() {
  'use strict';
  function listingId(url) {
    let value = String(url || '');
    try { value = decodeURIComponent(value); } catch (_) {}
    const explicit = value.match(/(?:item_id|itemId|wid)=((?:MLM|MLB|MLC|MLA|MLU|MCO)-?\d{5,})/i);
    if (explicit) return explicit[1].replace('-', '').toUpperCase();
    if (/\/p\/|\/up\//i.test(value)) return ''; // A catalog ID is not a seller listing.
    return (value.match(/\b(?:MLM|MLB|MLC|MLA|MLU|MCO)-?\d{5,}\b/i)?.[0] || '').replace('-', '').toUpperCase();
  }
  function apply(product, record) {
    if (!record?.product) throw Error('数据源尚无该商品记录，请稍后重试');
    const info = record.product;
    if (info.sku !== product.source_item_id) throw Error('商品编号与数据响应不一致');
    const size = info.size_cm || [];
    const complete = info.weight_g > 0 && size.length === 3 && size.every(v => v > 0);
    Object.assign(product, {
      weight_g: info.weight_g, package_length_cm:size[0] ?? null,
      package_width_cm:size[1] ?? null, package_height_cm:size[2] ?? null,
      volumetric_weight_kg: info.volumetric_weight_g == null ? null : info.volumetric_weight_g / 1000,
      weight_basis:'server_actual', net_proceeds_usd:info.net_proceeds_usd,
      commission_amount_usd:info.commission_amount_usd,
      scrape_status:complete && product.main_image_url ? 'ok' : 'partial',
      error_message:!complete ? '商品数据暂缺实际重量或完整包装尺寸' : !product.main_image_url ? '未识别到商品主图' : '',
      plugin_snapshot:{...info, source:'泽顺商品数据服务', read_method:'zeshun_server_api',
        fetched_at:record.fetched_at, weight_basis:'server_actual',
        fulfillment_type:info.fulfillment_type, fulfillment_label:info.fulfillment_label,
        fulfillment_eligible:info.fulfillment_eligible, self_ship_origin:info.seller_country || 'unknown',
        sales:info.sales, volumetric_formula:'length_cm * width_cm * height_cm / 6000'}
    });
    product.source.sold_quantity = info.sales;
    return product;
  }
  function sellerUrl(info) {
    const hosts = {MLM:'listado.mercadolibre.com.mx', MLB:'lista.mercadolivre.com.br',
      MLC:'listado.mercadolibre.cl', MCO:'listado.mercadolibre.com.co',
      MLA:'listado.mercadolibre.com.ar', MLU:'listado.mercadolibre.com.uy'};
    const host = hosts[String(info.sku || '').slice(0,3)];
    const id = String(info.seller_id ?? '');
    return host && /^[1-9]\d*$/.test(id) ? `https://${host}/_CustId_${id}` : '';
  }
  function render(doc, panel, record, options = {}) {
    const info = record.product;
    const display = value => value == null || value === '' ? '—' : String(value);
    const money = value => value == null ? '—' : `$${Number(value).toFixed(2)}`;
    const el = (tag, cls, text) => {
      const node = doc.createElement(tag); node.className = cls;
      if (text != null) node.textContent = text;
      return node;
    };
    panel.replaceChildren();
    panel.setAttribute('aria-label', `泽顺 ERP · ${info.sku} 商品数据`);
    const header = el('div','zs-info-header');
    const identity = el('div','zs-info-identity');
    identity.append(el('strong','zs-info-id',info.sku));
    const country = String(info.seller_country || '').toUpperCase();
    const flags = {CN:'🇨🇳',MX:'🇲🇽',BR:'🇧🇷',AR:'🇦🇷',CL:'🇨🇱',CO:'🇨🇴',UY:'🇺🇾',US:'🇺🇸'};
    if (country) {
      const flag = el('span','zs-info-country',flags[country] || country);
      flag.title = `卖家所在国家：${country}`;
      flag.setAttribute('aria-label', flag.title);
      identity.append(flag);
    }
    identity.append(el('span','zs-info-tag zs-info-fulfillment',info.fulfillment_label || '未识别'));
    const brand = el('span','zs-info-brand','泽');
    brand.title = '泽顺 ERP';
    brand.setAttribute('aria-label','泽顺 ERP');
    header.append(identity,brand);
    const seller = el('div','zs-info-seller');
    seller.append(el('span','zs-info-label','卖家：'));
    const href = sellerUrl(info);
    const name = el(href ? 'a' : 'span','zs-info-shop',display(info.seller));
    name.title = href ? `查看 ${display(info.seller)} 的店铺商品（新标签页）` : '暂无卖家店铺链接';
    if (href) {name.href=href; name.target='_blank'; name.rel='noopener noreferrer';}
    seller.append(name);
    const tags = el('span','zs-info-tags');
    if (info.seller_status) tags.append(el('span','zs-info-tag zs-info-status',info.seller_status));
    if (info.seller_level != null) tags.append(el('span','zs-info-tag zs-info-reputation',`${info.seller_level}_GREEN`));
    if (tags.childNodes.length) seller.append(tags);
    const line=(label,value,cls='')=>{
      const node=el('div',`zs-info-row ${cls}`.trim());
      node.append(el('span','zs-info-label',label),el('span','zs-info-value',value));
      return node;
    };
    const stats = el('div','zs-info-stats');
    stats.append(line('销量：',display(info.sales)),line('佣金：',info.commission_percent == null ? '—' : `${info.commission_percent}%`));
    const measure = (label, value, hint, shipping, source) => {
      const cell=line(label,value,'zs-info-measure');
      if (hint) cell.querySelector('.zs-info-value').append(el('span','zs-info-hint',hint));
      const selected = info.effective_freight_source === source;
      const freight=el('span',`zs-info-freight${selected ? ' is-selected' : ''}`);
      freight.title=`${source === 'weight' ? '实际重量' : '体积重量'}运费${selected ? '（净收益采用此项）' : ''}`;
      freight.setAttribute('aria-label', `${freight.title}：${money(shipping)}${info.low_freight ? '，低价档' : ''}`);
      freight.append(el('span','zs-info-freight-amount',money(shipping)));
      if (info.low_freight && shipping != null) freight.append(el('span','zs-info-low','低'));
      cell.append(freight);
      return cell;
    };
    const weight=measure('重量：',info.weight_g == null ? '—' : `${info.weight_g}g`,
      '',info.weight_freight_usd,'weight');
    const size=measure('尺寸：',info.size_cm ? `${info.size_cm.join(' × ')} cm` : '—',
      info.volumetric_weight_g == null ? '' : `（计抛${info.volumetric_weight_g}g）`,info.size_freight_usd,'size');
    const profit=line('净收益：',`${money(info.net_proceeds_usd)} / ${info.net_proceeds_cny == null ? '—' : `¥${Number(info.net_proceeds_cny).toFixed(2)}`}`,'zs-info-profit');
    profit.title=info.estimate_note || '净收益按美元售价减15%佣金及运费计算；不代表实际结算利润';
    const foot=el('div','zs-info-foot');
    const updated=Number(record.fetched_at);
    const estimate=info.estimate_commission_percent ?? 15;
    const freshness=el('span','zs-info-updated',`${record.cached ? '缓存' : '更新'}${updated > 0 ? ' '+new Date(updated*1000).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',hour12:false}) : ''}`);
    freshness.title=`净收益按${estimate}%佣金估算；${record.cached ? '服务端缓存数据' : '商品数据已更新'}`;
    foot.append(el('span','zs-info-profit-rule',`按${estimate}%佣金估算`),freshness);
    if (typeof options.onRefresh === 'function') {
      const refresh=el('button','zs-info-refresh','刷新');
      refresh.type='button';
      refresh.title='重新读取泽顺商品数据';
      refresh.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();options.onRefresh();});
      foot.append(refresh);
    }
    // Match the compact action strip: action first, quiet context at the right.
    const refresh = foot.querySelector('.zs-info-refresh');
    if (refresh) foot.prepend(refresh);
    panel.append(header,seller,stats,line('品牌：',display(info.brand)),weight,size,
      line('上架：',rows(info).find(row=>row[0]==='上架')[1]),profit,foot);
    for (const warning of record.warnings || []) panel.append(el('div','zs-info-warning',warning));
  }
  function rows(info, now = Date.now()) {
    const display = v => v === null || v === undefined || v === '' ? '—' : String(v);
    const money = v => v === null || v === undefined ? '—' : `$${Number(v).toFixed(2)}`;
    const listed = info.listed_at ? String(info.listed_at).slice(0, 10) : '';
    const days = listed ? Math.floor((now - Date.parse(listed+'T00:00:00Z'))/86400000) : NaN;
    const age = Number.isFinite(days) && days >= 0 ? `（${days}天前）` : '';
    return [
      ['商品', `${info.sku} · ${display(info.seller_country)} · ${info.fulfillment_label}`],
      ['卖家', `${display(info.seller)}${info.seller_level != null ? ` · ${info.seller_level}_GREEN` : ''}${info.seller_status ? ` · ${info.seller_status}` : ''}`],
      ['销量 / 佣金', `${display(info.sales)} / ${info.commission_percent == null ? '—' : info.commission_percent+'%'}`],
      ['品牌', display(info.brand)],
      ['重量', `${info.weight_g == null ? '—' : info.weight_g+'g'} · 运费 ${money(info.weight_freight_usd)}${info.effective_freight_source === 'weight' ? ' ✓' : ''}`],
      ['尺寸', `${info.size_cm ? info.size_cm.join(' × ')+' cm' : '—'}（计抛${display(info.volumetric_weight_g)}g）`],
      ['体积运费', `${money(info.size_freight_usd)}${info.effective_freight_source === 'size' ? ' ✓' : ''}${info.low_freight ? ' · 低价档' : ''}`],
      ['上架', listed ? listed+age : '—'],
      ['预计净收益', `${money(info.net_proceeds_usd)} / ${info.net_proceeds_cny == null ? '—' : '¥'+Number(info.net_proceeds_cny).toFixed(2)}`]
    ];
  }
  return {listingId, apply, rows, sellerUrl, render};
});
