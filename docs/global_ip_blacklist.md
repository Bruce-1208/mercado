# 品牌 / IP 公开资料线索

侵权知识库的“收集公开资料”按钮启动后台任务，复用自动分析的权限、互斥锁、日志和状态轮询。POST `/api/infringement-knowledge/analysis/start`，JSON 参数 `global_ip: true`。此模式不调用 AI、不读取商品记录，收集结果作为待复核线索。

数据源为 Wikidata Query Service（https://www.wikidata.org/wiki/Wikidata:Data_access），类别为品牌 Q431289、媒体系列 Q196600、角色 Q95074，包含子类。按实体分页，每类本次最多获取前 1000 个实体的英语、中文、西班牙语、葡萄牙语名称。不保证全球完整覆盖；再次执行重新获取该范围，当前不包含定时调度或增量游标。

新线索进入待复核；历史自动写入的黑白名单在读取时也视为待复核，不能自动拦截或放行商品。source_detail 以 global_ip:wikidata 标识，记录来源链接；来源界面显示 Wikidata。自动同步保护人工规则和已删除记录，只追加资料依据。同名条目沿用名称唯一约束，复核后可维护品牌别名及适用站点、类目、店铺。完整工作流程见 [美客多侵权功能说明](mercado-infringement.md)。

请求失败显示任务错误，已写入的批次保留；可重新获取。部署环境须允许访问 query.wikidata.org。接口单次连接超时 10 秒、读取超时 90 秒。
