/* One bounded server job per export; ordinary page requests stay short. */
(() => {
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    async function payload(response) {
        const body = await response.json();
        if (!response.ok) throw new Error(body.message || `导出请求失败 (${response.status})`);
        if (!body.data || typeof body.data !== 'object' || Array.isArray(body.data)) {
            throw new Error('导出状态响应格式错误');
        }
        return body.data;
    }
    window.queueWorkbenchExport = async function (url) {
        const job = await payload(await fetch('/api/exports', {
            method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url})
        }));
        let failures = 0;
        for (;;) {
            await sleep(2000 + Math.random() * 500);
            let state;
            try {
                const response = await fetch(`/api/exports/${encodeURIComponent(job.id)}`, {
                    credentials: 'same-origin', cache: 'no-store'
                });
                if ([401, 403, 404, 410].includes(response.status)) {
                    const body = await response.json();
                    throw Object.assign(new Error(body.message || '导出已不可用'), {terminal: true});
                }
                state = await payload(response);
                failures = 0;
            } catch (error) {
                if (error.terminal || ++failures >= 6) throw error;
                await sleep(Math.min(30000, 2000 * 2 ** failures));
                continue;
            }
            if (state.status === 'error') throw new Error(state.message || '导出失败');
            if (state.status === 'ready') {
                const response = await fetch(`/api/exports/${encodeURIComponent(job.id)}/download`, {
                    credentials: 'same-origin', cache: 'no-store'
                });
                if (!response.ok) await payload(response);
                return response;
            }
        }
    };
    window.downloadWorkbenchExport = async function (url) {
        const notice = document.createElement('div');
        notice.setAttribute('role', 'status');
        notice.textContent = '正在后台生成导出文件，请保留此页面…';
        Object.assign(notice.style, {position:'fixed',bottom:'24px',right:'24px',padding:'14px 20px',
            background:'#17324d',color:'#fff',borderRadius:'8px',zIndex:'10000'});
        document.body.appendChild(notice);
        try {
            const response = await window.queueWorkbenchExport(url);
            const disposition = response.headers.get('Content-Disposition') || '';
            const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
            const plain = disposition.match(/filename="?([^";]+)"?/i);
            let name = plain?.[1] || 'export.xlsx';
            if (encoded) { try { name = decodeURIComponent(encoded[1]); } catch (_) {} }
            const objectURL = URL.createObjectURL(await response.blob());
            const link = document.createElement('a');
            link.href = objectURL; link.download = name;
            document.body.appendChild(link); link.click(); link.remove();
            setTimeout(() => URL.revokeObjectURL(objectURL), 1000);
            notice.textContent = '导出完成';
        } catch (error) {
            notice.textContent = `导出失败：${error.message || error}`;
        } finally {
            setTimeout(() => notice.remove(), 8000);
        }
    };
})();
