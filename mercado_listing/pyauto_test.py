import pyautogui
import time

# 1. 回到桌面
pyautogui.hotkey('win', 'd')

# 2. 搜索浏览器 (假设是 Chrome)
pyautogui.press('win')
time.sleep(1)
pyautogui.write('chrome', interval=0.1)
pyautogui.press('enter')

# 3. 等待打开后，在地址栏输入并搜索
time.sleep(3)
pyautogui.write('https://www.google.com', interval=0.05)
pyautogui.press('enter')

# 4. 弹窗提示结束
pyautogui.alert("自动化搜索任务已完成！")