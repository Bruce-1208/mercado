













































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































































def list_orders(
    country="", status="", salesperson="", group_name="", search="", start_date="", end_date="",
    origin="", freight_variance="", page=1, page_size=50, store_ids=None, salespeople=None,
    freight_checked_from="", freight_checked_to="", remark_search="",
    sort_by="ordered_at", sort_dir="desc", allowed_store_ids=None, transfer_warehouse=None,
    include_summary=True, summary_only=False, sync_identifiers_only=False, purchase_platform="",
    freight_difference_min=None, freight_difference_max=None,
):
    """分页查询当前已授权店铺的 Token 同步订单。"""
    from bit.order_purchase_platform import validate_platform, platform_filter_sql
    purchase_platform = validate_platform(purchase_platform)
    from bit.order_freight_filters import difference_range, difference_predicate
    freight_difference_min, freight_difference_max = difference_range(freight_difference_min, freight_difference_max)
    difference_active = freight_difference_min is not None or freight_difference_max is not None
    if summary_only:
        include_summary = True
    if transfer_warehouse is not None:
        transfer_warehouse = normalize_warehouse(transfer_warehouse)
    page = max(1, int(page or 1))
    page_size = max(10, min(2000 if sync_identifiers_only else 200, int(page_size or 50)))
    country, status, salesperson, group_name, search = (
        str(value or "").strip()
        for value in (country, status, salesperson, group_name, search)
    )
    start_date, end_date = (str(value or "").strip() for value in (start_date, end_date))
    freight_checked_from = str(freight_checked_from or "").strip()
    freight_checked_to = str(freight_checked_to or "").strip()
    sort_by = str(sort_by or "ordered_at").strip().lower()
    if sort_by not in {"ordered_at", "amount", "balance", "cost", "profit", "profit_rate"}:
        raise ValueError("订单排序字段无效")
    sort_dir = str(sort_dir or "desc").strip().lower()
    if sort_dir not in {"asc", "desc"}:
        raise ValueError("订单排序方向无效")
    sort_sql_dir = "ASC" if sort_dir == "asc" else "DESC"
    start_at, end_exclusive = _filter_datetime_bounds(start_date, end_date)
    freight_start_at, freight_end_exclusive = _filter_datetime_bounds(
        freight_checked_from, freight_checked_to,
    )
    origin = str(origin or "").strip().lower()
    if origin not in ("", "token", "zying", "yandex"):
        origin = ""
    freight_variance = str(freight_variance or "").strip().lower()
    if freight_variance not in (
        "", "different", "different_any", "actual_higher", "actual_lower",
        "pending_actual", "pending_quote", "dimensions_changed",
    ):
        raise ValueError("运费差异筛选参数无效")

    normalized_store_ids = []
    for value in store_ids or ():
        try:
            store_id = int(value)
        except (TypeError, ValueError):
            continue
        if store_id != 0 and store_id not in normalized_store_ids:
            normalized_store_ids.append(store_id)
    if len(normalized_store_ids) > 200:
        raise ValueError("单次最多筛选 200 个店铺")

    normalized_salespeople = []
    salesperson_values = salespeople if salespeople is not None else ([salesperson] if salesperson else [])
    if isinstance(salesperson_values, str):
        salesperson_values = [salesperson_values]
    for value in salesperson_values or ():
        name = str(value or "").strip()
        if not name or name in normalized_salespeople:
            continue
        if len(name) > 100:
            raise ValueError("业务员名称不能超过 100 个字符")
        normalized_salespeople.append(name)
    if len(normalized_salespeople) > 100:
        raise ValueError("单次最多筛选 100 个业务员")

    if sync_identifiers_only:
        if (country or group_name or search or remark_search or origin or freight_variance or purchase_platform or difference_active or
                freight_checked_from or freight_checked_to or transfer_warehouse is not None or
                summary_only or sort_by != "ordered_at"):
            raise ValueError("智赢订单编号查询仅支持日期、店铺、业务员和状态筛选")
        return _list_zying_sync_identifiers(start_at, end_exclusive,
            normalized_store_ids or None, allowed_store_ids, normalized_salespeople,
            page, page_size, sort_sql_dir, status=status)

    def build_where(
        include_country=True, include_status=True, include_origin=True,
        include_salesperson=True, include_group=True, include_store=True,
        table_alias="",
    ):
        clauses, params = [], []
        prefix = f"{table_alias}." if table_alias else ""

        def column(name):
            return f"{prefix}`{name}`"

        if purchase_platform:
            clause, values = platform_filter_sql(column("purchase_order"), purchase_platform)
            clauses.append(clause)
            params.extend(values)
        if include_country and country:
            clauses.append(f"{column('country')} = %s")
            params.append(country)
        if include_status and status:
            status_groups = {'@' + name: values for name, values in ORDER_WORKFLOW_GROUPS.items()}
            selected_statuses = status_groups.get(status, [status])
            clauses.append(f"{column('status')} IN ({','.join(['%s'] * len(selected_statuses))})")
            params.extend(selected_statuses)
        if include_origin and origin:
            clauses.append(f"{column('data_origin')} = %s")
            params.append(origin)
        if include_store and normalized_store_ids:
            clauses.append(
                f"{column('store_id')} IN ({','.join(['%s'] * len(normalized_store_ids))})"
            )
            params.extend(normalized_store_ids)
        if include_salesperson and normalized_salespeople:
            named_salespeople = [
                value for value in normalized_salespeople if value != "__unassigned__"
            ]
            salesperson_clauses = []
            if "__unassigned__" in normalized_salespeople:
                salesperson_clauses.append(f"COALESCE({column('salesperson')}, '') = ''")
            if named_salespeople:
                salesperson_clauses.append(
                    f"{column('salesperson')} IN "
                    f"({','.join(['%s'] * len(named_salespeople))})"
                )
                params.extend(named_salespeople)
            clauses.append(f"({' OR '.join(salesperson_clauses)})")
        if include_group and group_name:
            if group_name == "__ungrouped__":
                clauses.append(f"COALESCE({column('group_name')}, '') = ''")
            else:
                clauses.append(f"{column('group_name')} = %s")
                params.append(group_name)
        if difference_active:
            clause, values = difference_predicate(column("actual_freight_usd"), column("quoted_freight_usd"),
                                                  freight_difference_min, freight_difference_max)
            clauses.append(clause)
            params.extend(values)
        if freight_variance:
            quoted = column("quoted_freight_usd")
            actual = column("actual_freight_usd")
            if freight_variance == "dimensions_changed":
                clauses.append(
                    f"COALESCE((SELECT {dimension_adjustment_sql('dimension_costs.payload_json')} "
                    "FROM mercado_shipment_costs AS dimension_costs "
                    f"WHERE dimension_costs.shipping_id = {column('platform_shipping_id')} "
                    f"AND dimension_costs.token_id = {column('store_id')}), 0) <> 0"
                )
            elif freight_variance == "different":
                clauses.append(
                    f"{quoted} IS NOT NULL AND {actual} IS NOT NULL "
                    f"AND ABS({actual} - {quoted}) > 0.01"
                )
            elif freight_variance == "different_any":
                clauses.append(
                    f"{quoted} IS NOT NULL AND {actual} IS NOT NULL "
                    f"AND ABS({actual} - {quoted}) > 0"
                )
            elif freight_variance == "actual_higher":
                clauses.append(
                    f"{quoted} IS NOT NULL AND {actual} IS NOT NULL "
                    f"AND {actual} - {quoted} > 0.01"
                )
            elif freight_variance == "actual_lower":
                clauses.append(
                    f"{quoted} IS NOT NULL AND {actual} IS NOT NULL "
                    f"AND {quoted} - {actual} > 0.01"
                )
            elif freight_variance == "pending_actual":
                clauses.append(f"{column('freight_local')} IS NULL")
            elif freight_variance == "pending_quote":
                clauses.append(f"{column('quoted_freight_local')} IS NULL")
        if start_at:
            clauses.append(f"{column('ordered_at')} >= %s")
            params.append(start_at.strftime("%Y-%m-%d %H:%M:%S"))
        if end_exclusive:
            clauses.append(f"{column('ordered_at')} < %s")
            params.append(end_exclusive.strftime("%Y-%m-%d %H:%M:%S"))
        if freight_start_at:
            clauses.append(f"{column('freight_checked_at')} >= %s")
            params.append(freight_start_at.strftime("%Y-%m-%d %H:%M:%S"))
        if freight_end_exclusive:
            clauses.append(f"{column('freight_checked_at')} < %s")
            params.append(freight_end_exclusive.strftime("%Y-%m-%d %H:%M:%S"))
        if remark_search:
            clauses.append(f"{column('order_remark')} LIKE %s")
            params.append(f"%{str(remark_search).strip()}%")
        if search:
            pattern = f"%{search}%"
            clauses.append(
                f"(CAST({column('id')} AS CHAR) LIKE %s OR "
                f"{column('order_number')} LIKE %s OR {column('purchase_order')} LIKE %s OR "
                f"{column('purchase_tracking')} LIKE %s OR "
                f"{column('platform_tracking_number')} LIKE %s OR {column('product_id')} LIKE %s OR "
                f"{column('title')} LIKE %s OR {column('buyer')} LIKE %s OR "
                f"{column('purchase_remark')} LIKE %s OR {column('remark')} LIKE %s OR "
                f"{column('shop_name')} LIKE %s OR {column('order_remark')} LIKE %s)"
            )
            params.extend([pattern] * 12)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def json_value(value):
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            from erp.mercadolibre_profitability_cache import (
                DAILY_EXCHANGE_RATE_TABLE,
                EXCHANGE_RATE_TABLE,
                ensure_profitability_cache_tables,
            )
            from erp.mercadolibre_store_link_store import (
                STORE_LINK_TABLE,
                ensure_store_link_table,
            )

            _ensure_mercado_synced_orders_table(cursor)
            _ensure_mercado_store_tokens_table(cursor)
            from bit.yandex_order_sync import store_source_sql
            unified_stores = store_source_sql(cursor)
            _ensure_mercado_store_site_settings_table(cursor)
            ensure_profitability_cache_tables(cursor)
            ensure_store_link_table(cursor)
            connection.commit()
            # Keep session-local temporary tables on one physical pooled connection.
            # DBUtils otherwise recycles the session at maxusage between statements.
            connection.begin()
            order_date_sql = "DATE(DATE_ADD(synced.`date_created`, INTERVAL 8 HOUR))"
            currency_sql = (
                "UPPER(COALESCE(NULLIF(synced.`amount_currency_id`, ''), "
                "CASE UPPER(COALESCE(synced.`site_id`, '')) "
                "WHEN 'MLM' THEN 'MXN' WHEN 'MLB' THEN 'BRL' WHEN 'MLA' THEN 'ARS' "
                "WHEN 'MLC' THEN 'CLP' WHEN 'MCO' THEN 'COP' WHEN 'MLU' THEN 'UYU' "
                "ELSE NULLIF(synced.`currency_id`, '') END, 'USD'))"
            )
            platform_status_sql = (
                "CASE WHEN LOWER(COALESCE(synced.`status`, '')) = 'ready_to_ship' "
                "THEN '待打印' ELSE synced.`status_label` END"
            )
            display_status_sql = order_workflow_sql('synced')
            # Resolve historical rates per distinct currency/day instead of once per
            # order.  A full order set contains tens of thousands of rows but only a
            # small number of currency/day pairs, so this removes most correlated
            # history lookups without changing the nearest-previous-date rule.
            rate_key_table = "`tmp_mercado_order_rate_keys`"
            order_rate_sql = (
                f"CASE WHEN {currency_sql} <> 'USD' "
                "AND UPPER(COALESCE(synced.`currency_id`, '')) = 'USD' "
                "AND COALESCE(synced.`total_amount`, 0) > 0 "
                "AND COALESCE(synced.`paid_amount`, 0) > 0 "
                "THEN synced.`paid_amount` / synced.`total_amount` ELSE NULL END"
            )
            rate_sql = (
                f"CASE WHEN {currency_sql} = 'USD' THEN 1 "
                f"WHEN ({order_rate_sql}) IS NOT NULL THEN ({order_rate_sql}) "
                "WHEN daily_rate.`rate` IS NOT NULL THEN daily_rate.`rate` "
                "ELSE current_rate.`rate` END"
            )
            cny_rate_sql = "COALESCE(cny_daily_rate.`rate`, cny_current_rate.`rate`)"
            usd_amount_sql = f"CASE WHEN synced.`site_id` = 'YANDEX' THEN NULL ELSE synced.`total_amount` * ({rate_sql}) END"
            fee_usd_sql = "CASE WHEN synced.`site_id` = 'YANDEX' THEN NULL ELSE COALESCE(synced.`sale_fee`, 0) END"
            shipped_sql = _mercado_shipped_sql()
            freight_local_sql = (
                f"CASE WHEN {shipped_sql} "
                "AND synced.`freight_source` = 'shipment_costs' "
                "AND actual_costs.`shipping_id` IS NOT NULL "
                "THEN synced.`freight` ELSE NULL END"
            )
            freight_currency_sql = (
                f"CASE WHEN ({freight_local_sql}) IS NOT NULL "
                f"THEN UPPER(COALESCE(NULLIF(synced.`freight_currency_id`, ''), "
                f"{currency_sql})) ELSE NULL END"
            )
            freight_usd_sql = (
                f"CASE WHEN ({freight_local_sql}) IS NULL THEN NULL "
                f"WHEN ({freight_currency_sql}) = 'USD' THEN ({freight_local_sql}) "
                f"ELSE ({freight_local_sql}) * ({rate_sql}) END"
            )
            quoted_local_sql = (
                "CASE WHEN synced.`quoted_freight_source` IN ('shipment_costs', 'order_shipping_cost') "
                "THEN COALESCE(synced.`quoted_freight_local`, synced.`quoted_freight_usd`) ELSE NULL END"
            )
            quoted_currency_sql = (
                "UPPER(COALESCE(NULLIF(synced.`quoted_freight_currency_id`, ''), 'USD'))"
            )
            quoted_usd_sql = (
                f"CASE WHEN ({quoted_local_sql}) IS NULL THEN NULL "
                f"WHEN ({quoted_currency_sql}) = 'USD' THEN ({quoted_local_sql}) "
                f"WHEN ({quoted_currency_sql}) = ({currency_sql}) THEN ({quoted_local_sql}) * ({rate_sql}) "
                "ELSE NULL END"
            )
            balance_usd_sql = (
                f"({usd_amount_sql}) - ({fee_usd_sql}) "
                f"- COALESCE(({freight_usd_sql}), ({quoted_usd_sql}))"
            )
            cny_income_sql = f"({usd_amount_sql}) * ({cny_rate_sql})"
            cny_balance_sql = f"ROUND(ROUND({balance_usd_sql}, 2) * ({cny_rate_sql}), 2)"
            source_sql = f"""
                (
                    SELECT synced.`order_id` AS `id`,
                           NULLIF(
                               JSON_UNQUOTE(JSON_EXTRACT(synced.`raw_json`, '$.pack_id')),
                               'null'
                           ) AS `order_number`,
                           synced.`token_id` AS `store_id`,
                           DATE_ADD(synced.`date_created`, INTERVAL 8 HOUR) AS `ordered_at`,
                           site_settings.`salesperson`,
                           site_settings.`discount_rate`, site_settings.`group_name`,
                           synced.`shop_name`, CASE WHEN synced.`site_id` = 'YANDEX' THEN 'Yandex Market' ELSE '美客多 Token' END AS `source`,
                           CASE WHEN synced.`site_id` = 'YANDEX' THEN 'yandex' ELSE 'token' END AS `data_origin`,
                           COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(synced.`raw_json`, '$.pack_id')), 'null'), '')
                               AS `pack_id`,
                           {display_status_sql} AS `status`,
                           {platform_status_sql} AS `platform_status`, synced.`workflow_status`,
                           ROUND({usd_amount_sql}, 2) AS `amount`,
                           synced.`total_amount` AS `amount_local`,
                           ROUND({fee_usd_sql}, 2) AS `fee`, synced.`sale_fee_source`,
                           0 AS `refund`, ROUND({cny_income_sql}, 2) AS `income`,
                           synced.`paid_amount` AS `paid_amount_usd`,
                           synced.`purchase_cost` AS `cost`, synced.`purchase_order`,
                           synced.`purchase_tracking`, synced.`logistics_company`, synced.`transfer_warehouse`,
                           synced.`platform_tracking_number`,
                           DATE_ADD(synced.`shipping_deadline`, INTERVAL 8 HOUR)
                               AS `shipping_deadline`,
                           synced.`purchase_remark`, synced.`shipping_id` AS `platform_shipping_id`,
                           CASE WHEN synced.`site_id` = 'YANDEX' THEN NULL ELSE ROUND({cny_balance_sql} - COALESCE(synced.`purchase_cost`, 0), 2) END
                               AS `profit`,
                           synced.`product_id`, '' AS `category`, synced.`title`,
                           synced.`image_url`, synced.`quantity`,
                           ROUND({freight_usd_sql}, 2) AS `freight`,
                           ROUND({balance_usd_sql}, 2) AS `balance`,
                           {cny_balance_sql} AS `balance_cny`,
                           ({freight_local_sql}) AS `freight_local`,
                           ({freight_currency_sql}) AS `freight_currency_id`,
                           synced.`freight_source`, synced.`freight_checked_at`,
                           ROUND({quoted_usd_sql}, 2) AS `quoted_freight_usd`,
                           ({quoted_local_sql}) AS `quoted_freight_local`,
                           ({quoted_currency_sql}) AS `quoted_freight_currency_id`,
                           synced.`quoted_freight_weight_g`,
                           synced.`quoted_freight_source`,
                           synced.`quoted_freight_checked_at`,
                           ROUND(({freight_usd_sql}) - ({quoted_usd_sql}), 2)
                               AS `freight_variance_usd`,
                           ROUND({freight_usd_sql}, 2) AS `actual_freight_usd`,
                           CASE WHEN ({freight_local_sql}) IS NULL THEN 1 ELSE 0 END
                               AS `freight_missing`,
                           CASE WHEN {shipped_sql} THEN 0 ELSE 1 END AS `freight_pending_shipment`,
                           synced.`order_remark`, synced.`status_detail` AS `remark`,
                           synced.`country`,
                           synced.`buyer_name` AS `buyer`, {currency_sql} AS `currency_id`,
                           UPPER(COALESCE(NULLIF(synced.`currency_id`, ''), {currency_sql}))
                               AS `platform_currency_id`,
                           ({rate_sql}) AS `exchange_rate_to_usd`,
                           CASE
                               WHEN {currency_sql} = 'USD' THEN DATE_FORMAT({order_date_sql}, '%%Y-%%m-%%d')
                               WHEN ({order_rate_sql}) IS NOT NULL
                                   THEN DATE_FORMAT({order_date_sql}, '%%Y-%%m-%%d')
                               WHEN daily_rate.`rate_date` IS NOT NULL
                                   THEN DATE_FORMAT(daily_rate.`rate_date`, '%%Y-%%m-%%d')
                               WHEN current_rate.`source_created_at` IS NOT NULL
                                   THEN LEFT(current_rate.`source_created_at`, 10)
                               WHEN current_rate.`refreshed_at` IS NOT NULL
                                   THEN DATE_FORMAT(current_rate.`refreshed_at`, '%%Y-%%m-%%d')
                               ELSE NULL
                           END AS `exchange_rate_date`,
                           CASE
                               WHEN {currency_sql} = 'USD' THEN 'same_currency'
                               WHEN ({order_rate_sql}) IS NOT NULL THEN 'order_implied'
                               WHEN daily_rate.`rate_date` = {order_date_sql} THEN 'order_date'
                               WHEN daily_rate.`rate_date` IS NOT NULL THEN 'nearest_previous'
                               WHEN current_rate.`rate` IS NOT NULL THEN 'current_fallback'
                               ELSE 'missing'
                           END AS `exchange_rate_match`,
                           CASE
                               WHEN {currency_sql} = 'USD' OR ({order_rate_sql}) IS NOT NULL
                                    OR daily_rate.`rate` IS NOT NULL
                                    OR current_rate.`rate` IS NOT NULL THEN 0
                               ELSE 1
                           END AS `exchange_rate_missing`,
                           ({cny_rate_sql}) AS `exchange_rate_usd_to_cny`,
                           CASE
                               WHEN cny_daily_rate.`rate_date` IS NOT NULL
                                   THEN DATE_FORMAT(cny_daily_rate.`rate_date`, '%%Y-%%m-%%d')
                               WHEN cny_current_rate.`source_created_at` IS NOT NULL
                                   THEN LEFT(cny_current_rate.`source_created_at`, 10)
                               WHEN cny_current_rate.`refreshed_at` IS NOT NULL
                                   THEN DATE_FORMAT(cny_current_rate.`refreshed_at`, '%%Y-%%m-%%d')
                               ELSE NULL
                           END AS `cny_exchange_rate_date`,
                           CASE
                               WHEN cny_daily_rate.`rate_date` = {order_date_sql} THEN 'order_date'
                               WHEN cny_daily_rate.`rate_date` IS NOT NULL THEN 'nearest_previous'
                               WHEN cny_current_rate.`rate` IS NOT NULL THEN 'current_fallback'
                               ELSE 'missing'
                           END AS `cny_exchange_rate_match`,
                           CASE
                               WHEN cny_daily_rate.`rate` IS NOT NULL
                                    OR cny_current_rate.`rate` IS NOT NULL THEN 0
                               ELSE 1
                           END AS `cny_exchange_rate_missing`,
                           DATE_ADD(synced.`last_updated`, INTERVAL 8 HOUR) AS `last_updated`
                    FROM `tmp_mercado_order_page` AS selected_page
                    STRAIGHT_JOIN `mercado_synced_orders` AS synced ON synced.`order_id` = selected_page.`id`
                    INNER JOIN {unified_stores} AS stores ON stores.`id` = synced.`token_id`
                    LEFT JOIN `mercado_shipment_costs` AS actual_costs
                      ON actual_costs.`shipping_id` = synced.`shipping_id`
                     AND actual_costs.`token_id` = synced.`token_id`
                     AND actual_costs.`shipment_status` IN ('shipped','delivered','not_delivered','returning','returned')
                    LEFT JOIN `mercado_store_site_settings` AS site_settings
                      ON site_settings.`token_id` = synced.`token_id`
                     AND site_settings.`site_id` = synced.`site_id`
                    LEFT JOIN {rate_key_table} AS rate_keys
                      ON rate_keys.`currency_id` = {currency_sql}
                     AND rate_keys.`order_date` = {order_date_sql}
                    LEFT JOIN `{DAILY_EXCHANGE_RATE_TABLE}` AS daily_rate
                      ON daily_rate.`id` = rate_keys.`daily_rate_id`
                    LEFT JOIN `{EXCHANGE_RATE_TABLE}` AS current_rate
                      ON current_rate.`from_currency_id` = {currency_sql}
                     AND current_rate.`to_currency_id` = 'USD'
                    LEFT JOIN `{DAILY_EXCHANGE_RATE_TABLE}` AS cny_daily_rate
                      ON cny_daily_rate.`id` = rate_keys.`cny_daily_rate_id`
                    LEFT JOIN `{EXCHANGE_RATE_TABLE}` AS cny_current_rate
                      ON cny_current_rate.`from_currency_id` = 'USD'
                     AND cny_current_rate.`to_currency_id` = 'CNY'
                ) AS `order_source`
            """
            # Counts and facets only need searchable/filterable fields.  Keep their
            # temporary table deliberately narrow; pricing is calculated once for the
            # summary and only for the 200 rows returned by the current page.
            search_columns = ""
            if search:
                search_columns = """
                    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(synced.`raw_json`, '$.pack_id')), 'null') AS `order_number`,
                    synced.`purchase_order`, synced.`purchase_tracking`, synced.`platform_tracking_number`,
                    synced.`product_id`, synced.`title`, synced.`buyer_name` AS `buyer`,
                    synced.`purchase_remark`, synced.`status_detail` AS `remark`,
                """
            if purchase_platform and not search:
                search_columns += "synced.`purchase_order`,"
            remark_column = "synced.`order_remark`," if search or remark_search else ""
            source_date_clauses, source_date_params = [], []
            if transfer_warehouse is not None:
                source_date_clauses.append("synced.`transfer_warehouse` = %s")
                source_date_params.append(transfer_warehouse)
            if start_at:
                source_date_clauses.append("synced.`date_created` >= %s")
                source_date_params.append((start_at - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"))
            if end_exclusive:
                source_date_clauses.append("synced.`date_created` < %s")
                source_date_params.append((end_exclusive - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"))
            if allowed_store_ids is not None:
                authorized_ids = sorted({int(value) for value in allowed_store_ids})
                if authorized_ids:
                    source_date_clauses.append("synced.`token_id` IN (" + ",".join(["%s"] * len(authorized_ids)) + ")")
                    source_date_params.extend(authorized_ids)
                else:
                    source_date_clauses.append("1 = 0")
            source_date_where = " WHERE " + " AND ".join(source_date_clauses) if source_date_clauses else ""
            filter_quote_usd_sql = (
                f"CASE WHEN ({quoted_currency_sql}) = 'USD' THEN ({quoted_local_sql}) ELSE NULL END"
            )
            filter_actual_usd_sql = (
                f"CASE WHEN ({freight_currency_sql}) = 'USD' THEN ({freight_local_sql}) ELSE NULL END"
            )
            if difference_active or freight_variance in {"different", "different_any", "actual_higher", "actual_lower"}:
                # Comparison filters run before the page's shared rate joins.
                # Resolve rates here only when a comparison actually needs them.
                filter_rate_sql = (
                    f"CASE WHEN {currency_sql} = 'USD' THEN 1 "
                    f"WHEN ({order_rate_sql}) IS NOT NULL THEN ({order_rate_sql}) "
                    "ELSE COALESCE((SELECT historical_rate.`rate` "
                    f"FROM `{DAILY_EXCHANGE_RATE_TABLE}` AS historical_rate "
                    f"WHERE historical_rate.`from_currency_id` = {currency_sql} "
                    "AND historical_rate.`to_currency_id` = 'USD' "
                    f"AND historical_rate.`rate_date` <= {order_date_sql} "
                    "ORDER BY historical_rate.`rate_date` DESC LIMIT 1), "
                    f"(SELECT filter_rate.`rate` FROM `{EXCHANGE_RATE_TABLE}` AS filter_rate "
                    f"WHERE filter_rate.`from_currency_id` = {currency_sql} "
                    "AND filter_rate.`to_currency_id` = 'USD' LIMIT 1)) END"
                )
                filter_quote_usd_sql = (
                    f"CASE WHEN ({quoted_currency_sql}) = 'USD' THEN ({quoted_local_sql}) "
                    f"WHEN ({quoted_currency_sql}) = {currency_sql} "
                    f"THEN ({quoted_local_sql}) * ({filter_rate_sql}) ELSE NULL END"
                )
                filter_actual_usd_sql = (
                    f"CASE WHEN ({freight_currency_sql}) = 'USD' THEN ({freight_local_sql}) "
                    f"WHEN ({freight_currency_sql}) = {currency_sql} "
                    f"THEN ({freight_local_sql}) * ({filter_rate_sql}) ELSE NULL END"
                )
            # The interactive page needs exact counts/facets but not a full
            # financial aggregation. Detail pricing still uses the real joins.
            needs_filter_finance = include_summary or sort_by not in {"ordered_at", "cost"} or bool(freight_variance) or difference_active
            filter_freight_local = freight_local_sql if needs_filter_finance else "NULL"
            filter_freight_currency = freight_currency_sql if needs_filter_finance else "NULL"
            filter_cost_join = """
                    LEFT JOIN `mercado_shipment_costs` AS actual_costs
                      ON actual_costs.`shipping_id` = synced.`shipping_id`
                     AND actual_costs.`token_id` = synced.`token_id`
                     AND actual_costs.`shipment_status` IN ('shipped','delivered','not_delivered','returning','returned')
            """ if needs_filter_finance else ""
            filter_store_join = "STRAIGHT_JOIN" if not source_date_clauses else "INNER JOIN"
            filter_source_sql = f"""
                (
                    SELECT synced.`order_id` AS `id`,
                           {search_columns}
                           synced.`token_id` AS `store_id`,
                           DATE_ADD(synced.`date_created`, INTERVAL 8 HOUR) AS `ordered_at`,
                           site_settings.`salesperson`, site_settings.`group_name`,
                           synced.`shop_name`, CASE WHEN synced.`site_id` = 'YANDEX' THEN 'yandex' ELSE 'token' END AS `data_origin`,
                           {display_status_sql}
                               AS `status`,
                           {remark_column}
                           synced.`country`,
                           synced.`freight_checked_at`,
                           synced.`shipping_id` AS `platform_shipping_id`,
                           {currency_sql} AS `currency_id`, {order_date_sql} AS `order_date`,
                           synced.`currency_id` AS `platform_currency_id`,
                           COALESCE(synced.`total_amount`, 0) AS `total_amount`,
                           COALESCE(synced.`paid_amount`, 0) AS `paid_amount`,
                           CASE WHEN synced.`site_id` = 'YANDEX' THEN NULL ELSE COALESCE(synced.`sale_fee`, 0) END AS `sale_fee`,
                           ({filter_freight_local}) AS `freight_local`,
                           ({filter_freight_currency}) AS `freight_currency_id`,
                           CASE WHEN ({filter_freight_local}) IS NULL
                                THEN 1 ELSE 0 END AS `freight_missing`,
                           ({quoted_local_sql}) AS `quoted_freight_local`,
                           ({quoted_currency_sql}) AS `quoted_freight_currency_id`,
                           ({filter_quote_usd_sql}) AS `quoted_freight_usd`,
                           ({filter_actual_usd_sql if needs_filter_finance else "NULL"}) AS `actual_freight_usd`,
                           synced.`purchase_cost` AS `purchase_cost`
                    FROM `mercado_synced_orders` AS synced
                    {filter_store_join} {unified_stores} AS stores
                      ON stores.`id` = synced.`token_id`
                    {filter_cost_join}
                    LEFT JOIN `mercado_store_site_settings` AS site_settings
                      ON site_settings.`token_id` = synced.`token_id`
                     AND site_settings.`site_id` = synced.`site_id`
                    {source_date_where}
                ) AS `order_filter_source`
            """
            base_where_sql, base_params = build_where(
                include_country=False,
                include_status=False,
                include_origin=False,
                include_salesperson=False,
                include_group=False,
                include_store=False,
            )
            order_filter_table = "`tmp_mercado_order_filter_source`"
            cursor.execute(f"DROP TEMPORARY TABLE IF EXISTS {order_filter_table}")
            cursor.execute(
                f"CREATE TEMPORARY TABLE {order_filter_table} AS "
                f"SELECT * FROM {filter_source_sql}{base_where_sql}",
                [*source_date_params, *base_params],
            )
            cursor.execute(
                f"ALTER TABLE {order_filter_table} "
                f"ADD PRIMARY KEY (`id`), ADD KEY `idx_ordered_at` (`ordered_at`, `id`), "
                f"ADD KEY `idx_status_ordered` (`status`, `ordered_at`, `id`)"
            )
            where_sql, params = build_where()
            cursor.execute(f"DROP TEMPORARY TABLE IF EXISTS {rate_key_table}")
            cursor.execute(
                f"""
                CREATE TEMPORARY TABLE {rate_key_table} AS
                SELECT pairs.`currency_id`, pairs.`order_date`,
                       (
                           SELECT historical_rate.`id`
                           FROM `{DAILY_EXCHANGE_RATE_TABLE}` AS historical_rate
                           WHERE historical_rate.`from_currency_id` = pairs.`currency_id`
                             AND historical_rate.`to_currency_id` = 'USD'
                             AND historical_rate.`rate_date` <= pairs.`order_date`
                           ORDER BY historical_rate.`rate_date` DESC
                           LIMIT 1
                       ) AS `daily_rate_id`,
                       (
                           SELECT historical_cny_rate.`id`
                           FROM `{DAILY_EXCHANGE_RATE_TABLE}` AS historical_cny_rate
                           WHERE historical_cny_rate.`from_currency_id` = 'USD'
                             AND historical_cny_rate.`to_currency_id` = 'CNY'
                             AND historical_cny_rate.`rate_date` <= pairs.`order_date`
                           ORDER BY historical_cny_rate.`rate_date` DESC
                           LIMIT 1
                       ) AS `cny_daily_rate_id`
                FROM (
                    SELECT DISTINCT `currency_id`, `order_date`
                    FROM {order_filter_table}{where_sql}
                ) AS pairs
                WHERE pairs.`order_date` IS NOT NULL
                """,
                params,
            )
            cursor.execute(
                f"ALTER TABLE {rate_key_table} "
                f"ADD PRIMARY KEY (`currency_id`, `order_date`)"
            )
            where_sql, params = build_where()
            cursor.execute(
                f"SELECT COUNT(*) AS `total` FROM {order_filter_table}{where_sql}", params
            )
            total = int((cursor.fetchone() or {}).get("total") or 0)
            summary_where_sql, summary_params = build_where(table_alias="scoped")
            summary_order_rate_sql = (
                "CASE WHEN scoped.`currency_id` <> 'USD' "
                "AND UPPER(COALESCE(scoped.`platform_currency_id`, '')) = 'USD' "
                "AND scoped.`total_amount` > 0 AND scoped.`paid_amount` > 0 "
                "THEN scoped.`paid_amount` / scoped.`total_amount` ELSE NULL END"
            )
            summary_rate_sql = (
                "CASE WHEN scoped.`currency_id` = 'USD' THEN 1 "
                f"WHEN ({summary_order_rate_sql}) IS NOT NULL "
                f"THEN ({summary_order_rate_sql}) "
                "WHEN daily_rate.`rate` IS NOT NULL THEN daily_rate.`rate` "
                "ELSE current_rate.`rate` END"
            )
            summary_cny_rate_sql = (
                "COALESCE(cny_daily_rate.`rate`, cny_current_rate.`rate`)"
            )
            summary_amount_sql = (
                f"CASE WHEN scoped.`data_origin` = 'yandex' THEN NULL ELSE scoped.`total_amount` * ({summary_rate_sql}) END"
            )
            summary_freight_rate_sql = (
                "CASE WHEN scoped.`freight_currency_id` = 'USD' THEN 1 "
                f"ELSE ({summary_rate_sql}) END"
            )
            summary_freight_sql = (
                f"scoped.`freight_local` * ({summary_freight_rate_sql})"
            )
            summary_quoted_freight_sql = (
                "CASE WHEN scoped.`quoted_freight_currency_id` = 'USD' "
                "THEN scoped.`quoted_freight_local` "
                "WHEN scoped.`quoted_freight_currency_id` = scoped.`currency_id` "
                f"THEN scoped.`quoted_freight_local` * ({summary_rate_sql}) ELSE NULL END"
            )
            summary_balance_freight_sql = (
                f"COALESCE(({summary_freight_sql}), ({summary_quoted_freight_sql}))"
            )
            summary_income_sql = (
                f"({summary_amount_sql}) * ({summary_cny_rate_sql})"
            )
            summary_cny_balance_sql = (
                f"ROUND(ROUND(({summary_amount_sql}) - COALESCE(scoped.`sale_fee`, 0) "
                f"- ({summary_balance_freight_sql}), 2) * ({summary_cny_rate_sql}), 2)"
            )
            summary = None
            if include_summary:
                cursor.execute(
                    f"SELECT COALESCE(SUM(ROUND({summary_amount_sql}, 2)),0) AS `amount`, "
                    f"COALESCE(SUM(ROUND(scoped.`sale_fee`, 2)),0) AS `fee`, "
                    f"COALESCE(SUM(ROUND({summary_freight_sql}, 2)),0) AS `freight`, "
                    f"COALESCE(SUM(ROUND(({summary_amount_sql}) - scoped.`sale_fee` "
                    f"- ({summary_balance_freight_sql}), 2)),0) AS `balance`, "
                    f"COALESCE(SUM(ROUND({summary_income_sql}, 2)),0) AS `income`, "
                    f"COALESCE(SUM({summary_cny_balance_sql}),0) AS `balance_cny`, "
                    f"COALESCE(SUM(scoped.`purchase_cost`),0) AS `cost`, "
                    f"COALESCE(SUM(CASE WHEN scoped.`data_origin` = 'yandex' THEN NULL ELSE ROUND(({summary_cny_balance_sql}) - COALESCE(scoped.`purchase_cost`, 0), 2) END),0) "
                    f"AS `profit`, "
                    f"SUM(CASE WHEN scoped.`purchase_cost` IS NULL THEN 1 ELSE 0 END) AS `cost_missing_count`, "
                    f"SUM(CASE WHEN scoped.`data_origin` = 'yandex' THEN 1 ELSE 0 END) AS `settlement_missing_count`, "
                    f"COALESCE(SUM(CASE WHEN scoped.`data_origin` = 'yandex' OR scoped.`currency_id` = 'USD' "
                    f"OR ({summary_order_rate_sql}) IS NOT NULL OR daily_rate.`rate` IS NOT NULL "
                    f"OR current_rate.`rate` IS NOT NULL THEN 0 ELSE 1 END),0) "
                    f"AS `exchange_rate_missing_count`, "
                    f"COALESCE(SUM(CASE WHEN scoped.`data_origin` = 'yandex' OR cny_daily_rate.`rate` IS NOT NULL "
                    f"OR cny_current_rate.`rate` IS NOT NULL THEN 0 ELSE 1 END),0) "
                    f"AS `cny_exchange_rate_missing_count`, "
                    f"COALESCE(SUM(scoped.`freight_missing`),0) AS `freight_missing_count` "
                    f"FROM {order_filter_table} AS scoped "
                    f"LEFT JOIN {rate_key_table} AS rate_keys "
                    f"ON rate_keys.`currency_id` = scoped.`currency_id` "
                    f"AND rate_keys.`order_date` = scoped.`order_date` "
                    f"LEFT JOIN `{DAILY_EXCHANGE_RATE_TABLE}` AS daily_rate "
                    f"ON daily_rate.`id` = rate_keys.`daily_rate_id` "
                    f"LEFT JOIN `{EXCHANGE_RATE_TABLE}` AS current_rate "
                    f"ON current_rate.`from_currency_id` = scoped.`currency_id` "
                    f"AND current_rate.`to_currency_id` = 'USD' "
                    f"LEFT JOIN `{DAILY_EXCHANGE_RATE_TABLE}` AS cny_daily_rate "
                    f"ON cny_daily_rate.`id` = rate_keys.`cny_daily_rate_id` "
                    f"LEFT JOIN `{EXCHANGE_RATE_TABLE}` AS cny_current_rate "
                    f"ON cny_current_rate.`from_currency_id` = 'USD' "
                    f"AND cny_current_rate.`to_currency_id` = 'CNY'{summary_where_sql}",
                    summary_params,
                )
                summary = {key: json_value(value or 0) for key, value in (cursor.fetchone() or {}).items()}

            if summary_only:
                return {"total": total, "summary": summary, "rows": [], "summary_pending": False}

            status_where, status_params = build_where(include_status=False)
            cursor.execute(
                f"SELECT COALESCE(`status`,'未分类') AS `status`, COUNT(*) AS `count` "
                f"FROM {order_filter_table}{status_where} "
                f"GROUP BY COALESCE(`status`,'未分类') ORDER BY `count` DESC",
                status_params,
            )
            status_counts = {str(row["status"]): int(row["count"]) for row in cursor.fetchall()}

            country_where, country_params = build_where(include_country=False, include_status=False)
            cursor.execute(
                f"SELECT COALESCE(`country`,'未分类') AS `country`, COUNT(*) AS `count` "
                f"FROM {order_filter_table}{country_where} "
                f"GROUP BY COALESCE(`country`,'未分类') ORDER BY `count` DESC",
                country_params,
            )
            country_counts = {str(row["country"]): int(row["count"]) for row in cursor.fetchall()}

            origin_where, origin_params = build_where(include_origin=False)
            cursor.execute(
                f"SELECT `data_origin`, COUNT(*) AS `count` "
                f"FROM {order_filter_table}{origin_where} GROUP BY `data_origin`",
                origin_params,
            )
            origin_counts = {str(row["data_origin"]): int(row["count"]) for row in cursor.fetchall()}

            store_where, store_params = build_where(include_store=False)
            cursor.execute(
                f"SELECT `store_id`, `shop_name`, COUNT(*) AS `count` "
                f"FROM {order_filter_table}{store_where} GROUP BY `store_id`, `shop_name` "
                f"ORDER BY `shop_name` ASC, `store_id` ASC",
                store_params,
            )
            store_counts = [
                {
                    "id": int(row["store_id"]),
                    "name": str(row.get("shop_name") or f"店铺 {row['store_id']}"),
                    "count": int(row["count"]),
                }
                for row in cursor.fetchall()
            ]

            salesperson_where, salesperson_params = build_where(include_salesperson=False)
            cursor.execute(
                f"SELECT COALESCE(NULLIF(`salesperson`,''),'未分配') AS `salesperson`, COUNT(*) AS `count` "
                f"FROM {order_filter_table}{salesperson_where} "
                f"GROUP BY COALESCE(NULLIF(`salesperson`,''),'未分配') ORDER BY `salesperson` ASC",
                salesperson_params,
            )
            salesperson_counts = {
                str(row["salesperson"]): int(row["count"]) for row in cursor.fetchall()
            }

            group_where, group_params = build_where(include_group=False)
            cursor.execute(
                f"SELECT COALESCE(NULLIF(`group_name`,''),'未分组') AS `group_name`, COUNT(*) AS `count` "
                f"FROM {order_filter_table}{group_where} "
                f"GROUP BY COALESCE(NULLIF(`group_name`,''),'未分组') ORDER BY `group_name` ASC",
                group_params,
            )
            group_counts = {
                str(row["group_name"]): int(row["count"]) for row in cursor.fetchall()
            }

            offset = (page - 1) * page_size
            # Limit the indexed, narrow rows before calculating page details.
            page_table = "`tmp_mercado_order_page`"
            cursor.execute(f"DROP TEMPORARY TABLE IF EXISTS {page_table}")
            page_where_sql, page_params = build_where(table_alias="scoped")
            sort_expressions = {
                "ordered_at": "scoped.`ordered_at`",
                "amount": f"ROUND({summary_amount_sql}, 2)",
                "balance": f"ROUND({summary_amount_sql} - scoped.`sale_fee` - ({summary_balance_freight_sql}), 2)",
                "cost": "scoped.`purchase_cost`",
                "profit": f"CASE WHEN scoped.`data_origin` = 'yandex' THEN NULL ELSE ROUND({summary_cny_balance_sql} - COALESCE(scoped.`purchase_cost`, 0), 2) END",
                "profit_rate": (
                    f"CASE WHEN scoped.`data_origin` = 'yandex' THEN NULL WHEN ({summary_income_sql}) <> 0 "
                    f"THEN (({summary_cny_balance_sql} - COALESCE(scoped.`purchase_cost`, 0)) / ({summary_income_sql})) "
                    "ELSE 0 END"
                ),
            }
            page_sort_joins = (
                f"LEFT JOIN {rate_key_table} AS rate_keys "
                "ON rate_keys.`currency_id` = scoped.`currency_id` "
                "AND rate_keys.`order_date` = scoped.`order_date` "
                f"LEFT JOIN `{DAILY_EXCHANGE_RATE_TABLE}` AS daily_rate "
                "ON daily_rate.`id` = rate_keys.`daily_rate_id` "
                f"LEFT JOIN `{EXCHANGE_RATE_TABLE}` AS current_rate "
                "ON current_rate.`from_currency_id` = scoped.`currency_id` "
                "AND current_rate.`to_currency_id` = 'USD' "
                f"LEFT JOIN `{DAILY_EXCHANGE_RATE_TABLE}` AS cny_daily_rate "
                "ON cny_daily_rate.`id` = rate_keys.`cny_daily_rate_id` "
                f"LEFT JOIN `{EXCHANGE_RATE_TABLE}` AS cny_current_rate "
                "ON cny_current_rate.`from_currency_id` = 'USD' "
                "AND cny_current_rate.`to_currency_id` = 'CNY'"
            )
            cursor.execute(
                f"CREATE TEMPORARY TABLE {page_table} AS "
                f"SELECT scoped.`id` FROM {order_filter_table} AS scoped "
                f"{page_sort_joins if sort_by not in {'ordered_at', 'cost'} else ''}{page_where_sql} "
                f"ORDER BY {sort_expressions[sort_by]} {sort_sql_dir}, "
                f"scoped.`ordered_at` DESC, scoped.`id` DESC LIMIT %s OFFSET %s",
                [*page_params, page_size, offset],
            )
            cursor.execute(f"ALTER TABLE {page_table} ADD PRIMARY KEY (`id`)")
            source_sort_expressions = {
                "ordered_at": "order_source.`ordered_at`",
                "amount": "order_source.`amount`",
                "balance": "order_source.`balance`",
                "cost": "order_source.`cost`",
                "profit": "order_source.`profit`",
                "profit_rate": (
                    "CASE WHEN order_source.`income` <> 0 "
                    "THEN order_source.`profit` / order_source.`income` ELSE 0 END"
                ),
            }
            cursor.execute(
                f"SELECT order_source.* FROM {source_sql} "
                f"ORDER BY {source_sort_expressions[sort_by]} {sort_sql_dir}, "
                "order_source.`ordered_at` DESC, order_source.`id` DESC"
            )
            rows = [{key: json_value(value) for key, value in row.items()} for row in cursor.fetchall()]
            order_ids = [str(row.get("id") or "") for row in rows if row.get("id")]
            raw_orders = {}
            if order_ids:
                placeholders = ",".join(["%s"] * len(order_ids))
                cursor.execute(
                    f"SELECT `order_id`, `raw_json` FROM `mercado_synced_orders` "
                    f"WHERE `order_id` IN ({placeholders})",
                    order_ids,
                )
                raw_orders = {
                    str(row.get("order_id") or ""): row.get("raw_json") or "{}"
                    for row in (cursor.fetchall() or [])
                }

            token_ids = set()
            product_ids = set()
            for row in rows:
                order_id = str(row.get("id") or "")
                sku_items = _mercado_order_sku_items(raw_orders.get(order_id, "{}"))
                if row.get("store_id"):
                    token_ids.add(int(row["store_id"]))
                product_ids.update(
                    str(item.get("product_id") or "")
                    for item in sku_items
                    if str(item.get("product_id") or "")
                )
                if row.get("product_id"):
                    product_ids.add(str(row["product_id"]))

            product_assets = {}
            if token_ids and product_ids:
                token_placeholders = ",".join(["%s"] * len(token_ids))
                product_placeholders = ",".join(["%s"] * len(product_ids))
                cursor.execute(
                    f"""
                    SELECT `token_id`, `site_id`, `item_id`, `permalink`, `thumbnail_url`
                    FROM `{STORE_LINK_TABLE}`
                    WHERE `is_current` = 1
                      AND `token_id` IN ({token_placeholders})
                      AND `item_id` IN ({product_placeholders})
                    """,
                    [*sorted(token_ids), *sorted(product_ids)],
                )
                product_assets = {
                    (str(asset.get("token_id") or ""), str(asset.get("item_id") or "")): asset
                    for asset in (cursor.fetchall() or [])
                }

            for row in rows:
                order_id = str(row.get("id") or "")
                store_id = str(row.get("store_id") or "")
                row_assets = {
                    product_id: product_assets[(store_id, product_id)]
                    for product_id in product_ids
                    if (store_id, product_id) in product_assets
                }
                sku_items = _mercado_order_sku_items(
                    raw_orders.get(order_id, "{}"),
                    row.get("image_url") or "",
                    row_assets,
                )
                for item in sku_items:
                    item["order_id"] = order_id
                row["sku_items"] = sku_items
                primary_asset = row_assets.get(str(row.get("product_id") or ""), {})
                row["image_url"] = (
                    _mercado_https_url(row.get("image_url"))
                    or _mercado_https_url(primary_asset.get("thumbnail_url"))
                    or (sku_items[0].get("image_url") if sku_items else "")
                )
                row["product_url"] = (
                    _mercado_https_url(primary_asset.get("permalink"))
                    or (sku_items[0].get("product_url") if sku_items else "")
                    or _mercado_public_item_url(row.get("product_id"), row.get("site_id"))
                )
                if row.get("data_origin") == "yandex":
                    row["product_url"] = ""
                    row["paid_amount_usd"] = None
                    row["amount"] = row["income"] = None
                    from bit.yandex_order_sync import display_prices
                    payload = json.loads(raw_orders.get(order_id, "{}"))
                    row["yandex_prices"] = display_prices(payload.get("yandex_order") or {}, payload.get("yandex_finance"))
                    row["fee"] = row["balance"] = row["profit"] = None
                    for item in row["sku_items"]:
                        item["product_url"] = ""
                row["merged_order_ids"] = [order_id] if order_id else []
            return {
                "rows": rows, "total": total, "page": page, "page_size": page_size,
                "pages": max(1, (total + page_size - 1) // page_size),
                "status_counts": status_counts, "country_counts": country_counts,
                "origin_counts": origin_counts, "store_counts": store_counts,
                "salesperson_counts": salesperson_counts,
                "group_counts": group_counts,
                "summary": summary, "summary_pending": not include_summary,
            }
    finally:
        connection.close()
