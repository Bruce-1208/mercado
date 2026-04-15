import pandas as pd
import os
from bit_summary_delayfile import *

rp_file = Path(__file__).resolve().parent / "美客多声誉"
filename = get_latest_modified_file(rp_file)
filepath = rp_file / filename
filepath = '/Users/a11/mercado/bit/美客多声誉/武汉泽顺店铺声誉信息汇总2026-04-12-20.xlsx'
df = pd.read_excel(filepath, engine='openpyxl')
print("✅ 读取成功！")

if not os.path.exists(filepath):
    print("❌ 错误：文件路径不存在！请检查路径是否正确。")
elif not os.access(filepath, os.R_OK):
    print("❌ 错误：没有读取权限！请检查 Mac 磁盘访问设置。")
else:
    try:
        df = pd.read_excel(filepath, engine='openpyxl')
        print("✅ 读取成功！")
        print(df.head())
    except Exception as e:
        print(f"❌ 读取失败，具体错误是: {e}")