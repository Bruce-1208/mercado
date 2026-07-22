# mercado

## 同步美客多 Listings

`mercado_api/mercado_api_listings.py` 使用官方 Global Selling API，通过 access token 自动识别店铺，完整获取 listing 并保存到本地 SQLite。

直接运行（推荐，token 不会显示在终端，也不会存入数据库）：

```bash
python3 -m mercado_api.mercado_api_listings
```

也可以在代码中调用：

```python
from mercado_api import sync_listings

result = sync_listings("店铺的 access token")
print(result.database_path)
```

默认生成 `mercado_api_listings.db`，主要数据表为 `mercado_listings`；变体位于 `mercado_listing_variations`，每次同步记录位于 `mercado_sync_runs`。重复运行会更新已有商品，最新一次已不存在的商品会保留并标记为 `is_current = 0`。

## 自动登录美客多账号

`bit/bit_mercado_login.py` 会连接指定的 BitBrowser 窗口，自动填写 Mercado Libre Global Selling 的账号密码。登录 Cookie 保存在对应的 BitBrowser 窗口中；程序不会把密码写入代码、Excel、数据库或日志。

交互式运行（密码会在终端中隐藏输入）：

```bash
python3 -m bit.bit_mercado_login \
  --window-id 你的BitBrowser窗口ID \
  --username 你的美客多登录账号
```

无人值守运行时通过环境变量提供凭据：

```bash
export BIT_MERCADO_WINDOW_ID="你的BitBrowser窗口ID"
export MERCADO_LOGIN_USER="你的美客多登录账号"
export MERCADO_LOGIN_PASSWORD="你的美客多登录密码"
python3 -m bit.bit_mercado_login
```

若美客多要求验证码、二维码或二次验证，程序会保留浏览器窗口并等待人工完成，默认最多等待 300 秒。该程序不会尝试绕过验证码或平台安全验证。
