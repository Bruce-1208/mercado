/* A poll is a short request; the expensive computation continues in the backend. */
async function fetchAnalysis(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('X-Analysis-Async', '1');
    const {deadlineMs = 600000, ...requestOptions} = options;
    const deadline = Date.now() + deadlineMs;
    let failures = 0;
    while (true) {
        if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
        if (Date.now() >= deadline) throw new Error('分析仍在计算，请稍后重新查询');
        const controller = new AbortController();
        const abort = () => controller.abort();
        options.signal?.addEventListener('abort', abort, {once: true});
        const timer = setTimeout(abort, Math.min(30000, deadline - Date.now()));
        try {
            const response = await fetch(url, {...requestOptions, headers, signal: controller.signal});
            if ([502, 503, 504].includes(response.status) && failures++ < 2) {
                await response.body?.cancel();
            } else if (response.status !== 202) return response;
            else failures = 0;
        } catch (error) {
            if (options.signal?.aborted) throw error;
            if (++failures > 2) throw new Error('请求超时或连接中断，请重新查询');
        } finally {
            clearTimeout(timer);
            options.signal?.removeEventListener('abort', abort);
        }
        await new Promise((resolve, reject) => {
            if (options.signal?.aborted) return reject(new DOMException('Aborted', 'AbortError'));
            const stop = () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); };
            const timer = setTimeout(() => { options.signal?.removeEventListener('abort', stop); resolve(); }, Math.min(2000, Math.max(0, deadline - Date.now())));
            options.signal?.addEventListener('abort', stop, {once: true});
        });
    }
}
