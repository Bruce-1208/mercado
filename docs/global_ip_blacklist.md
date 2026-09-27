# 全球品牌/IP 风险黑名单

侵权知识库的“获取全球品牌/IP 并加入黑名单”按钮启动后台任务，复用自动分析的权限、互斥锁、日志和状态轮询。POST `/api/infringement-knowledge/analysis/start`，JSON 参数 `global_ip: true`。此模式不调用 AI、不读取商品记录、不生成白名单。

数据源为 Wikidata Query Service（https://www.wikidata.org/wiki/Wikidata:Data_access），类别为品牌 Q431289、媒体系列 Q196600、角色 Q95074，包含子类。按实体分页，每类本次最多获取前 1000 个实体的英语、中文、西班牙语、葡萄牙语名称。不保证全球完整覆盖；再次执行重新获取该范围，当前不包含定时调度或增量游标。

自动写入 blacklist，备注注明风险用途及实体来源链接，source_detail 以 global_ip:wikidata 标识。复用现有自动分析写入规则：保护人工记录与人工删除记录，黑名单优先于自动白名单。自动记录 source_type 沿用 analysis，evidence_count 为 0（公开目录不是侵权证据）。同名条目沿用现有名称唯一约束。

请求失败显示任务错误，已写入的批次保留；可重新获取。部署环境须允许访问 query.wikidata.org。接口单次连接超时 10 秒、读取超时 90 秒。
