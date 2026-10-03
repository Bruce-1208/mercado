# WIPO 商标线索同步

工作台「侵权知识库」新增 WIPO 同步面板。服务端默认每 24 小时拉取官方马德里国际商标每日增量包，首次从最近 7 天开始。也可以选择授权的 JSON / CSV 数据源，用于导入更完整的商标集合。配置和最近 50 次运行记录持久化到 `.data/wipo-sync.sqlite3`，重启后继续调度；页面显示最近 10 次记录。

## 数据范围

WIPO 全球品牌数据库的公开检索站禁止自动查询和批量下载；此功能使用 WIPO 官方另行提供的马德里每日更新包，不请求公开检索站的内部搜索接口。

官方每日更新包只包含马德里体系中当天有变化的商标记录，不是全球品牌全库，也不是马德里完整历史。未变化的旧商标不会因启用每日更新而自动补齐。完整历史需购买或取得授权数据，再使用授权数据源入口导入。

- [全球品牌数据库使用条款](https://www.wipo.int/en/web/global-brand-database/terms_and_conditions)
- [马德里商标更新包下载说明](https://www.wipo.int/en/web/madrid-system/monitor/download)

同步到的文字商标只作为待复核线索，不直接用于拦截或放行商品。知识库由人工确认「风险拦截」或「人工放行」后才向风险检测提供处置规则；人工放行仅针对对应品牌，不排除其他品牌或独立图片风险。历史自动生成的黑白名单读取时也视为待复核，不需批量改写旧数据。

## 同步规则

1. 自动与手动任务共用跨进程锁，同一时间只执行一次同步。Web / worker 分离部署中，配置、立即同步和状态查询统一转发到中心 worker；client 模式转发到中心服务器。
2. 官方增量包按文件日期从旧到新读取。不存在更新包的周末或假日不补造数据，清单中列出的包读取失败则整次失败；只有本批全部解析并成功写入数据库后才推进包日期游标。较大历史范围按包边界分批，页面明确显示剩余包数，开启自动同步时一分钟后继续。起始日期早于官方现存包保留范围会明确报错。
3. 品牌去重后统一写为 `pending`，来源标记为 `wipo`。同名人工记录保留其处置、备注与证据，已删除记录不自动恢复；运行结果分别报告保留人工处置和跳过已删除数量。重复同步不会重复新增。
4. 不因为某品牌在下一次更新中缺席而删除已有线索。纯图形且没有文字名称的商标无法作为标题关键词，计入跳过数量。
5. 失败保留之前的知识库和最近成功时间，自动任务一小时后重试。进程中断后的任务显示“中断”，可重新同步。
6. 查看沿用 `infringement_knowledge.view`，保存配置和启动任务沿用 `infringement_knowledge.manage`。数据源令牌加密保存，不经状态、日志或页面读回。

侵权检测使用知识库游标分页读取，超过 5000 个品牌也会参与检测。

## 授权 JSON / CSV 数据源

数据源须为可公开解析的 HTTPS 地址，端口 443；认证使用可选 Bearer Token。文件使用 UTF-8 编码。页面上的 Token 留空表示保留旧值，保存成功后输入框清空。禁止地址内嵌用户名与密码；重定向不会自动跟随，JSON 下一页必须处于相同域名。连接固定到已验证的公开 IP，同时保留原域名的 TLS 校验，避免域名二次解析指向内网。

JSON 可以直接返回数组，或者返回包含 `records`、`results`、`data`、`items` 数组之一的对象。分页使用 `next` / `next_url` URL 字符串，支持相对地址。声明 `total`、`has_more` 或 `next_cursor` 却未提供完整记录或可跟随的下一页时，任务会报错，避免把一页数据误报成同步完成。

```json
{
  "records": [
    {"brand_name": "Example Mark", "registration_number": "123456", "country": "MX", "status": "Registered", "nice_classes": "25"}
  ],
  "next": "?page=2"
}
```

CSV 示例：

```csv
brand_name,registration_number,country,status,nice_classes
Example Mark,123456,MX,Registered,25
```

名称字段兼容 `brand_name`、`brand name`、`mark_name`、`mark`、`trademark`、`verbal_element`、`brand`、`name`、`品牌名称`、`商标名称`、`商标`。登记号、辖区、状态与尼斯分类记录在来源详情中；不同登记状态均进入待复核，供人工结合商品和证据决定处置。

单次授权 JSON / CSV 拉取限制为 50 MiB、250000 条、1000 页与 10 分钟；超出会失败并提示提供分批数据源，不会静默截断。官方 ZIP 单包限制 100 MiB、单次累计下载 500 MiB、最多 31 包；XML 流式解析，单 XML 上限 350 MiB、单 ZIP 总解压上限 512 MiB。达到下载批次边界后，下轮从已成功包日期继续。

## 部署与接口

更新代码、安装 `bit/requirements-server.txt` 后重启中心服务。`BIT_BACKGROUND_SERVICES_DISABLED=1` 与 client 角色不会启动自动同步，手动操作仍可使用。`BIT_WIPO_SYNC_PATH` 可覆盖状态数据库路径；Web 与 worker 需使用相同路径及持久化应用密钥。

公开登录接口：

| 方法 | 地址 | 用途 |
| --- | --- | --- |
| GET / PUT | `/api/infringement-knowledge/wipo/settings` | 查看 / 保存配置 |
| POST | `/api/infringement-knowledge/wipo/sync` | 立即启动任务，JSON 请求体 `{}` |
| GET | `/api/infringement-knowledge/wipo/status` | 查看状态、统计及历史 |

内部 `/api/db/infringement-knowledge/wipo/*` 对应接口沿用内部认证，client 进程不会承接中心同步任务。

数据来源标注为 WIPO；页面显示 WIPO 要求的原文数据免责声明。
