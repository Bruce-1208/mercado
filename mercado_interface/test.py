import requests
import json

url = "http://zeshun.nat100.top/api/v1/records"

# url = "http://127.0.0.1:5000/api/v1/records"
# 爬虫抓取到或者大模型识别出的数据样例
payload = {
    "zhiying_category": "家居/厨房用品",
    "original_img_url": "https://img.alicdn.com/imgextra/i1/123456/O1CN01.jpg",
    "is_same_style": 1,
    "product_id": "ML_2026_TEST001",
    "title": "跨境爆款多功能不锈钢切菜机",
    "identified_weight": 450,
    "pre_modified_weight": 400,
    "post_modified_weight": 460,
    "pre_modified_cost_usd": 2.5000,
    "post_modified_cost_usd": 2.8500,
    "max_sku_price_cny": 18.50,
    "max_sku_spec": "不锈钢加大款-带3刀头",
    "max_sku_id": "1688_sku_99887766",
    "model_confidence": 0.98,
    "weight_issue": "原克重偏低，已根据1688规格重新修正",
    "matched_1688_url": "https://detail.1688.com/offer/712345678.html",
    "reason": "大模型高度置信同款，且克重误差>10%，已自动触发OMS克重和价格更新。"
}

response = requests.post(url, json=payload)
print(response.text)