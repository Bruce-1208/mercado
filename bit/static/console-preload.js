/* Each ERP module declares only its initial reads; shared mounted lists stay untouched. */
(() => {
    const start = () => {
        const runtime = window.ZeshunPerformance;
        if (!runtime) return;
        const value = id => document.getElementById(id)?.value?.trim() || '';
        const url = (path, params) => `${path}?${new URLSearchParams(params)}`;
        const module = (id, requests = () => []) => ({id, requests});
        const frame = (id, element) => ({id, frame: () => document.getElementById(element)});
        const list = mode => {
            const params = new URLSearchParams({search: mercadoListSearch.value.trim(), limit: String(mercadoListPageSize), offset: '0', include_total: '1'});
            if (mercadoListMode === mode) {
                [['review_status', mercadoReviewFilter.value], ['risk_status', value('mercado-infringement-filter')],
                    ['publish_status', mercadoPublishFilter.value], ['weight_status', mercadoWeightStatusFilter.value],
                    ['management_category_id', mercadoManagementCategoryFilter.value]].forEach(([key, val]) => {if (val) params.set(key, val);});
                if (mode === 'products') {
                    [['source_type', mercadoProductSource], ['mercado_category', mercadoPlatformCategoryFilter.value.trim()],
                        ['zying_category', mercadoZyingCategoryFilter.value], ['product_developer_id', mercadoProductDeveloperFilter.value]]
                        .forEach(([key, val]) => {if (val) params.set(key, val);});
                }
            }
            return `${mode === 'collection' ? '/api/mercado-collection/items' : '/api/mercado-products'}?${params}`;
        };
        const storeLinks = () => {
            const [sort, direction] = (storeLinkSalesSort.value || 'sold_quantity:desc').split(':');
            const params = new URLSearchParams({search: storeLinkSearch.value.trim(), site_id: storeLinkSiteFilter.value,
                group_name: storeLinkGroupFilter.value, salesperson: storeLinkSalespersonFilter.value, status: storeLinkStatusFilter.value,
                promotion_applied: storeLinkPromotionFilter.value, advertising_enabled: storeLinkAdvertisingFilter.value,
                video_uploaded: storeLinkVideoFilter.value, management_category_id: storeLinkProductCategoryFilter.value,
                mercado_category: storeLinkMercadoCategoryFilter.value, include_categories: '0', sort_by: sort,
                sort_order: direction, sales_sort: direction, page: '1', page_size: storeLinkPageSize.value || '200', current_only: '1'});
            mercadoSelectedValues(storeLinkStoreFilter).forEach(id => params.append('token_id', id));
            return `/api/store-links?${params}`;
        };
        const modules = [
            module('overview'),
            module('orders', () => [`/api/orders?${orderQueryParams(1)}&include_summary=0`, '/api/order-sync/status']),
            module('inventory', () => ['/api/inventory/shelves?include_inactive=1', url('/api/inventory/stocks', {
                inbound_person: value('inventory-stock-inbound-person'), inventory_owner: value('inventory-stock-owner'),
                search: value('inventory-stock-search'), shelf_id: value('inventory-stock-shelf'),
                stock_status: value('inventory-stock-status') || 'positive', page: '1', page_size: '50'}),
                '/api/inventory/skus?include_inactive=1', '/api/inventory/sku-mappings', '/api/inventory/reconciliation',
                url('/api/inventory/movements', {inbound_person: value('inventory-log-inbound-person'),
                    inventory_owner: value('inventory-log-owner'), search: value('inventory-log-search'),
                    movement_type: value('inventory-log-type'), shelf_id: value('inventory-log-shelf'), page: '1', page_size: '50'})]),
            module('1688-products', () => [`/api/1688-products?${products1688Query()}`, '/api/1688-products/options', '/api/mercado-management-categories']),
            module('ai-original-products', () => [url('/api/ai-original-products', {limit: '500', include_cost_summary: '0',
                target_platform: aiOriginalTargetPlatform(), search: value('ai-original-search'),
                generator_salesperson: value('ai-original-generator-filter')}), '/api/ai-original-products/profiles',
                '/api/ai-original-products/cost-summary', '/api/mercado-tokens', '/api/ai-original-products/process/status', '/api/mercado-products/publish/status']),
            module('mercado-collection', () => [list('collection'), '/api/mercado-management-categories']),
            module('mercado-products', () => [list('products')]),
            // /changed starts a refresh, /saved creates a task snapshot. Both remain user initiated.
            module('weight-dimensions-records'),
            module('mercado-shipping-standards', () => [`/api/mercado-shipping-rates?site_id=${encodeURIComponent(mercadoShippingRateSite)}`]),
            module('mercado-publish-records', () => {
                const params = new URLSearchParams({search: publishRecordSearch.value.trim(), status: publishRecordStatus.value,
                    site_id: publishRecordSite.value, group_name: publishRecordGroup?.value || '', start_date: publishRecordStartDate?.value || '',
                    end_date: publishRecordEndDate?.value || '', limit: '500'});
                mercadoSelectedValues(publishRecordStore).forEach(id => params.append('token_id', id));
                return ['/api/mercado-tokens', `/api/mercado-publish-records?${params}`];
            }),
            // Starting the separate Yandex service is a POST. Warm its status, keep startup explicit.
            module('yandex', () => ['/api/yandex-console/status']),
            module('appeal', () => ['/api/appeal-phrases']),
            module('tasks', () => ['/api/tasks/daily/options']),
            module('salesperson-stats', () => [url('/api/salesperson-stats', {start_date: value('salesperson-stats-start'), end_date: value('salesperson-stats-end')})]),
            module('store-analysis', () => window.storeAnalysisPreloadUrls?.() || ['/api/mercado-tokens']),
            module('order-analysis', () => {
                if (!afterSaleDateFrom.value && !afterSaleDateTo.value) setHighAfterSalePeriod(30, {load: false});
                if (!profitDateFrom.value && !profitDateTo.value) setHighProfitPeriod(30, {load: false});
                return [url('/api/order-analysis/high-after-sales', {sort_by: highAfterSaleSortBy, sort_dir: 'desc',
                    search: afterSaleSearch.value.trim(), date_from: afterSaleDateFrom.value, date_to: afterSaleDateTo.value, limit: '100'}),
                url('/api/order-analysis/high-profits', {sort_by: highProfitSortBy, sort_dir: 'desc',
                    search: profitSearch.value.trim(), date_from: profitDateFrom.value, date_to: profitDateTo.value, limit: '100'})];
            }),
            module('window-anomalies', () => ['/api/window-anomalies?active_only=1&limit=500', '/api/window-anomalies/mercado-login/status']),
            module('ai-video', () => ['/api/ai-videos/jobs?limit=20&page=1']),
            module('store-operations', () => [url('/api/store-links/operation-logs', {limit: '30',
                operation_type: value('store-link-audit-type'), salesperson: value('store-link-audit-salesperson'),
                created_after: storeLinkAuditDateBoundary(value('store-link-audit-from')),
                created_before: storeLinkAuditDateBoundary(value('store-link-audit-to'), true)})]),
            module('store-links', () => [storeLinks(), '/api/mercado-management-categories', '/api/store-links/sync/status', '/api/store-links/bulk-update/status']),
            frame('size-charts', 'size-charts-frame'), frame('clearance-alerts', 'clearance-alerts-frame'), frame('promotions', 'promotions-frame'),
            module('ad-analysis', () => ['/api/mercado-tokens', '/api/ad-analysis?force=0']),
            module('prohibited-listings', () => ['/api/prohibited-listings?page=1&page_size=100', '/api/prohibited-listings/sync/status']),
            module('store-tokens', () => ['/api/mercado-applications', '/api/mercado-tokens', '/api/mercado-account-groups']),
            module('pre-sale', () => ['/api/mercado-tokens']), module('customer-service', () => ['/api/mercado-tokens']),
            module('funds', () => ['/api/funds/latest', '/api/funds/collect/status']),
            frame('ai-weight-price', 'ai-weight-price-frame'),
            module('zying-collection', () => ['/api/zying-collection/status', '/api/zying-collection/categories', '/api/zying-collection/developers', '/api/mercado-collection/browser-windows']),
            module('risk-check', () => ['/api/risk-check/categories', '/api/risk-check/options', `/api/risk-check/results?${riskResultQueryParams()}`, '/api/risk-check/status']),
            module('infringement-knowledge', () => ['/api/infringement-knowledge?page=1&page_size=50']),
            frame('infractions', 'infraction-dashboard-frame'), frame('ip-rights', 'ip-rights-dashboard-frame'),
            module('reputation', () => ['/api/reputation/latest', '/api/collections/options']),
            module('api-reputation', () => ['/api/mercado-reputation/status']),
            module('ai-appeals', () => [`/api/ai-appeal-records?limit=500&appeal_copy_mode=${encodeURIComponent(aiAppealCopyModeFilter?.value || '')}`]),
            module('access', () => ['/api/access/catalog', '/api/access/roles', '/api/access/users']),
        ];
        // Evaluate once: an exception in one optional module never prevents other pages warming.
        const paths = new Set();
        modules.forEach(item => {
            try {const requests = (item.requests?.() || []).filter(address => !/^\/api\/(?:store-analysis|order-analysis\/|ad-analysis)/.test(address)); requests.forEach(address => paths.add(new URL(address, location.href).pathname)); item.requests = () => requests;}
            catch (_) {item.requests = () => [];}
        });
        runtime.configure({modules, allowedPaths: [...paths], scopeUrl: enterpriseScopedUrl});
        // Stop scheduled reads when their workspace/browser is hidden. Server jobs keep running.
        const polls = [
            ['orders', () => {clearTimeout(orderSyncPollTimer); orderSyncPollTimer = null;}, loadOrderSyncStatus],
            ['mercado-products', () => {clearTimeout(mercadoPublishPollTimer); mercadoPublishPollTimer = null;}, loadMercadoPublishStatus],
            ['mercado-collection', () => {clearTimeout(mercadoCollectionPollTimer); mercadoCollectionPollTimer = null;}, loadMercadoCollectionStatus],
            ['mercado-shipping-standards', () => {clearTimeout(mercadoShippingRatePollTimer); mercadoShippingRatePollTimer = null;}, loadMercadoShippingRates],
            ['api-reputation', () => {clearTimeout(apiReputationPollTimer); apiReputationPollTimer = null;}, loadApiReputationStatus],
            ['ai-video', () => {clearTimeout(aiVideoPollTimer); aiVideoPollTimer = null;}, () => {
                if (aiVideoJobs.some(job => ['queued', 'submitting', 'generating'].includes(job.status))) loadAiVideoJobs(true);
            }],
            ['prohibited-listings', () => {clearTimeout(prohibitedSyncPollTimer); prohibitedSyncPollTimer = null;}, loadProhibitedListingSyncStatus],
            ['funds', () => {clearTimeout(fundStatusPollTimer); fundStatusPollTimer = null;}, loadFundCollectionStatus],
            ['risk-check', () => {clearTimeout(riskCheckPollTimer); riskCheckPollTimer = null;}, loadRiskCheckStatus],
            ['zying-collection', () => {clearTimeout(zyingCollectionPollTimer); zyingCollectionPollTimer = null;}, loadZyingCollectionStatus],
            ['tasks', () => {clearTimeout(dailyTaskStatusPollTimer); dailyTaskStatusPollTimer = null;}, loadDailyTaskStatus],
            ['store-links', stopStoreLinkStatusPolling, () => {loadStoreLinkSyncStatus(); loadStoreLinkRemoteUpdateStatus();}],
        ];
        const pause = () => polls.forEach(([id, stop]) => {if (!runtime.isActive(id)) stop();});
        document.addEventListener('zeshun:visibility', pause);
        document.addEventListener('visibilitychange', () => {
            pause();
            if (!document.hidden) polls.forEach(([id, , resume]) => {if (runtime.isActive(id)) resume();});
        });
        // Standalone navigation retains browser navigation semantics and warms its document/assets.
        document.querySelectorAll('.tab-button:not([data-tab])').forEach(button => {
            const address = button.getAttribute('href') || button.getAttribute('onclick')?.match(/window\.location\.href=['"]([^'"]+)/)?.[1];
            if (!address || button.hidden || !['/mercado/today', '/employee-tasks', '/billing'].includes(address)) return;
            const link = document.createElement('link'); link.rel = 'prefetch'; link.href = enterpriseScopedUrl(address); link.as = 'document'; document.head.append(link);
        });
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true}); else start();
})();
