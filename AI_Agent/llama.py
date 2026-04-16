import requests
import json


def chat_with_llama(prompt):
    url = "http://localhost:11434/api/generate"
    data = {
        "model": "llama3.1:8b",
        "prompt": prompt,
        "stream": False  # 设置为 False 会直接返回完整结果，True 则会流式返回
    }

    response = requests.post(url, json=data)

    if response.status_code == 200:
        return response.json().get("response")
    else:
        return f"Error: {response.text}"


# 测试
print(chat_with_llama("""亲爱的客服，我叫Mike，因为菜鸟没有及时揽收我的物流，对我店铺声誉造成了影响，我总结了下面这些订单，你能帮我消除对我声誉的影响吗？
Hi! Someone from our team will join the conversation soon to help you. Your case number is 450789482.
你好，很高兴向大家问好，wuhanshizejianyuesha团队，wuhanshizejianyuesha团队！希望一切都很好！我是来自Mercado Libre的Diego Andres，今天将成为您的助手。我理解您需要帮助解决您的发货延误问题，我想让您知道我完全愿意为您解决任何问题。在开始之前，您能告诉我您贵姓吗？我非常想了解细节，以便以最好的方式支持您。
'2000015870885728
'2000015870367362
'2000015869719798
'2000015867814016
'2000015864275470
'2000015863849340
我叫 Bruce
这些是订单
布鲁斯，很高兴认识你，非常感谢你给我这些订单
我完全理解你对这些订单由于物流延误而对你的声誉产生影响的担忧。我愿意全力协助你检查发生了什么。

我理解这种情况令人沮丧，尤其是当你按时完成发货，但问题出在Cainiao扫描的外部因素上。为了确保我理解正确：你是否需要我们分析这些特定订单，让相关团队评估是否有可能减轻因扣留而对你的声誉造成的损害？"""+"帮我分析这段对话，我叫 Bruce，我在找客服申诉延误的订单，帮我敷衍性的简短回复客服"))