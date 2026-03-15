# Please install OpenAI SDK first: `pip3 install openai`

from openai import OpenAI



def get_title(tile):
    client = OpenAI(api_key="sk-ed493baf046d449997aa4077a4d2dfe1", base_url="https://api.deepseek.com")

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "user", "content": tile+"把这个标题改成西班牙语，要求60个字符以内,不能出现品牌侵权,把标题写在第一行，不要有特殊符号和其他解释"},
        ],
        stream=False
    )

    return response.choices[0].message.content


if __name__ == '__main__':
        get_title("跨境中式复古风台灯布艺灯")