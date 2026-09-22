/* Short, sequential log requests; automation continues independently on Agent. */
(() => {
    function wait(milliseconds) {
        return new Promise(resolve => {
            let timer;
            const finish = () => {
                window.clearTimeout(timer);
                document.removeEventListener("visibilitychange", visible);
                resolve();
            };
            const visible = () => { if (!document.hidden) finish(); };
            document.addEventListener("visibilitychange", visible);
            timer = window.setTimeout(finish, milliseconds);
        });
    }

    async function follow(taskId, {onLog, onState, onRetry, onRecovered}) {
        let after = 0;
        let failures = 0;
        while (true) {
            if (document.hidden) {
                await wait(30000);
                continue;
            }
            const controller = new AbortController();
            const timeout = window.setTimeout(() => controller.abort(), 15000);
            let data;
            try {
                const response = await fetch(
                    `/api/run_shensu/${encodeURIComponent(taskId)}/events?after=${after}`,
                    {cache: "no-store", signal: controller.signal},
                );
                if ([400, 401, 403, 404].includes(response.status)) {
                    const error = new Error("无法继续读取日志，请检查登录状态和任务权限；任务不会因此自动停止。");
                    error.permanent = true;
                    throw error;
                }
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const payload = await response.json();
                if (payload.status !== "success" || !payload.data) {
                    throw new Error(payload.message || "日志响应无效");
                }
                data = payload.data;
            } catch (error) {
                if (error.permanent) throw error;
                failures += 1;
                if (failures === 1) onRetry?.(error);
            } finally {
                window.clearTimeout(timeout);
            }
            if (!data) {
                await wait(Math.min(30000, 3000 * (2 ** Math.min(failures - 1, 4))) + Math.random() * 1000);
                continue;
            }
            if (failures) onRecovered?.();
            failures = 0;
            if (data.log) onLog(data.log);
            after = data.next_after;
            onState?.(data);
            if (data.done) return data;
            await wait(data.has_more ? 250 : 3000 + Math.random() * 1000);
        }
    }

    window.ZeshunAppealPolling = {follow};
})();
