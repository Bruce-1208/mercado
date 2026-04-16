from openai import OpenAI

# 指向本地 Ollama 服务
client = OpenAI(
    base_url='http://localhost:11434/v1/',
    api_key='ollama',  # 随便填，不需要真实 key
)

def get_ai_response(user_input):
    response = client.chat.completions.create(
        model="qwen2.5-coder:14b",
        messages=[
            {"role": "system", "content": "你是一位专业的网页客服助手。"},
            {"role": "user", "content": user_input}
        ]
    )
    return response.choices[0].message.content



if __name__ == '__main__':
    message=""
    print(get_ai_response("""亲爱的客服，我叫Bruce，因为菜鸟没有及时揽收我的物流，对我店铺声誉造成了影响，我总结了下面这些订单，你能帮我消除对我声誉的影响吗？
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

我理解这种情况令人沮丧，尤其是当你按时完成发货，但问题出在Cainiao扫描的外部因素上。为了确保我理解正确：你是否需要我们分析这些特定订单，让相关团队评估是否有可能减轻因扣留而对你的声誉造成的损害？"""+"帮我分析这段对话，请记住我是Bruce，回复客服问题，简短一点，如果他明确拒绝我，返回 fail"))