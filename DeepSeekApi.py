import os

from AI_Agent.deepseek import chat_deepseek


TITLE_MODEL = os.getenv("DEEPSEEK_TITLE_MODEL", os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"))


def get_title(title: str) -> str:
    prompt = (
        f"{title}\n"
        "把这个标题改成西班牙语，要求 60 个字符以内，不能出现品牌侵权。"
        "只把标题写在第一行，不要特殊符号和其他解释。"
    )
    return chat_deepseek(
        [{"role": "user", "content": prompt}],
        model=TITLE_MODEL,
        temperature=0.3,
    ).strip()


if __name__ == "__main__":
    print(get_title("跨境中式复古风台灯布艺灯"))
