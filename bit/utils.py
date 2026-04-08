from opencc import OpenCC



def convert_text(text):
    # 初始化OpenCC对象，可以选择不同的转换配置文件
    # 't2s' 代表繁体转简体，'s2t' 代表简体转繁体
    cc = OpenCC('t2s')  # 繁体转简体
    # cc = OpenCC('s2t')  # 简体转繁体
    converted_text = cc.convert(text)
    return converted_text # 输出简体化的文本

