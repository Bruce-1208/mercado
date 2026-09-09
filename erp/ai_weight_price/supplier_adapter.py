"""Read-only, page-local supplier adaptation from observed DOM nodes.

The model selects node numbers, never executable code or arbitrary selectors.
Only verified observations become selectors; they are not reused on other pages.
"""
import re


AUTO_FIELDS = ("supplier_title", "supplier_image", "supplier_merchant", "sku_rows", "sku_label")
MERCHANT_ATTRIBUTES = ("data-member-id", "data-memberid", "data-login-id", "data-seller-id", "memberid")
SKU_ATTRIBUTES = ("data-sku-id", "data-skuid", "skuid", "data-sku")
ID_ATTRIBUTES = (*MERCHANT_ATTRIBUTES, *SKU_ATTRIBUTES)


class SupplierAdaptationError(ValueError):
    pass


DOM_SNAPSHOT = r"""() => {
  const visible=e=>e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
  const ignored='script,style,noscript,template,input,textarea,select,header,nav,footer,aside,[contenteditable="true"]';
  const idAttrs=ATTRS;
  const clean=s=>String(s||'').replace(/\s+/g,' ').trim();
  const path=e=>{let parts=[];while(e&&e.nodeType===1){
    let n=1;for(let p=e.previousElementSibling;p;p=p.previousElementSibling)if(p.tagName===e.tagName)n++;
    parts.unshift(e.tagName.toLowerCase()+':nth-of-type('+n+')');e=e.parentElement;
  }return parts.join(' > ');};
  const nodes=[];let size=0,truncated=false;
  for(const e of document.body.querySelectorAll('*')){
    if(!visible(e)||e.closest(ignored))continue;
    const attrs={};for(const name of idAttrs){const value=e.getAttribute(name);
      if(value&&/^[\w.:-]{1,160}$/.test(value))attrs[name]=value;}
    // Never collect input values, scripts, cookies, storage or arbitrary data-* attributes.
    const text=clean(Array.from(e.childNodes).filter(n=>n.nodeType===3).map(n=>n.textContent).join(' ')).slice(0,500);
    const src=e.tagName==='IMG'?(e.currentSrc||e.src):'';
    const image=/^https?:\/\//i.test(src)?src:'';
    if(!text&&!image&&!e.children.length&&!Object.keys(attrs).length)continue;
    const node={n:nodes.length,path:path(e),tag:e.tagName.toLowerCase(),
      hint:clean(e.className?.baseVal??e.className).slice(0,180),text,attrs};
    if(image)node.image=image;
    size+=JSON.stringify(node).length;
    if(nodes.length>=2500||size>180000){truncated=true;break;}
    nodes.push(node);
  }
  return {nodes,truncated};
}""".replace("ATTRS", repr(list(ID_ATTRIBUTES)).replace("'", '"'))


def verified_selectors(page, snapshot, answer):
    """Resolve model-selected IDs to observed nodes and recheck the live DOM."""
    if snapshot.get("truncated"):
        raise SupplierAdaptationError("1688页面结构过大，无法完整校验，请使用手工适配")
    if not isinstance(answer, dict) or answer.get("certain") is not True:
        raise SupplierAdaptationError("无法确认1688标题、主图、商家及完整SKU行，需要手工适配")
    nodes = {node["n"]: node for node in snapshot["nodes"]}

    def observed(index):
        if type(index) is not int or index not in nodes:
            raise SupplierAdaptationError("适配结果引用了页面中不存在的节点")
        node = nodes[index]
        item = page.locator(node["path"])
        if item.count() != 1 or not item.is_visible():
            raise SupplierAdaptationError("适配期间页面发生变化，节点不能唯一回读")
        direct = item.evaluate("e => Array.from(e.childNodes).filter(n=>n.nodeType===3).map(n=>n.textContent).join(' ').replace(/\\s+/g,' ').trim().slice(0,500)")
        if direct != node["text"]:
            raise SupplierAdaptationError("适配期间页面内容发生变化")
        for attribute, value in node["attrs"].items():
            if item.get_attribute(attribute) != value:
                raise SupplierAdaptationError("适配期间商家或SKU标识发生变化")
        return node, item

    def identifier(node, item, attribute, allowed):
        if attribute not in allowed or attribute not in node["attrs"]:
            raise SupplierAdaptationError("商家或SKU缺少可核验的稳定ID属性，不能使用行号代替")
        value = item.get_attribute(attribute) or ""
        if value != node["attrs"][attribute] or not re.fullmatch(r"[\w.:-]{1,160}", value):
            raise SupplierAdaptationError("商家或SKU标识无法回读确认")
        return value

    title, title_item = observed(answer.get("title"))
    photo, photo_item = observed(answer.get("image"))
    merchant = answer.get("merchant")
    if not isinstance(merchant, dict):
        raise SupplierAdaptationError("适配结果缺少商家节点")
    seller, seller_item = observed(merchant.get("node"))
    merchant_id = identifier(seller, seller_item, merchant.get("attribute"), MERCHANT_ATTRIBUTES)
    title_text = title_item.inner_text().strip()
    if not title_text or len(title_text) > 1000:
        raise SupplierAdaptationError("商品标题为空或选择了过大的页面容器")
    if photo["tag"] != "img" or not photo.get("image"):
        raise SupplierAdaptationError("主图必须是页面实际展示的HTTP(S)图片")
    image_url = photo_item.evaluate("e=>e.currentSrc||e.src")
    if image_url != photo["image"]:
        raise SupplierAdaptationError("适配期间商品主图发生变化")
    # Browser.value reads src, so a lazy or srcset-only image cannot silently
    # resolve to a placeholder different from the verified product image.
    if photo_item.evaluate("e=>e.src") != image_url:
        raise SupplierAdaptationError("商品主图使用动态图片源，需要手工适配")

    rows = answer.get("skus")
    sku_attribute = answer.get("sku_attribute")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise SupplierAdaptationError("未识别到可校验的SKU行")
    row_paths, label_paths, skus = [], [], []
    for spec in rows:
        if not isinstance(spec, dict):
            raise SupplierAdaptationError("SKU适配结构无效")
        row, row_item = observed(spec.get("row"))
        label, label_item = observed(spec.get("label"))
        if not label["path"].startswith(row["path"] + " > "):
            raise SupplierAdaptationError("SKU标签不属于对应SKU行")
        sku_id = identifier(row, row_item, sku_attribute, SKU_ATTRIBUTES)
        label_text = label_item.inner_text().strip()
        if not label_text or len(label_text) > 1000:
            raise SupplierAdaptationError("SKU规格为空或不是单行规格")
        if row["path"] in row_paths or any(s["id"] == sku_id for s in skus):
            raise SupplierAdaptationError("SKU行或稳定ID重复，不能唯一定位变体")
        row_paths.append(row["path"])
        label_paths.append(":scope > " + label["path"][len(row["path"]) + 3:])
        skus.append({"id": sku_id, "label": label_text})
    selectors = {"supplier_title": title["path"], "supplier_image": photo["path"],
                 "supplier_merchant": seller["path"], "supplier_merchant_attribute": merchant["attribute"],
                 "sku_rows": ", ".join(row_paths), "sku_label": ", ".join(dict.fromkeys(label_paths)),
                 "sku_id_attribute": sku_attribute}
    for row_path, sku in zip(row_paths, skus):
        label = page.locator(row_path).locator(selectors["sku_label"])
        if label.count() != 1 or label.inner_text().strip() != sku["label"]:
            raise SupplierAdaptationError("SKU标签不能在每行中唯一定位")
    evidence = {"title": title_text, "main_image_url": image_url, "merchant_id": merchant_id, "skus": skus}
    return selectors, evidence
