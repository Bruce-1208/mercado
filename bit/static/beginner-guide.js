/* Reading tools only: this guide never starts a business operation. */
(() => {
    const chapters = [...document.querySelectorAll('[data-chapter]')];
    const search = document.getElementById('guide-search');
    const links = [...document.querySelectorAll('.sidebar nav a')];
    const details = [...document.querySelectorAll('details')];
    let searchSnapshot = null;
    function filterChapters() {
        const query = search.value.trim().toLocaleLowerCase();
        if (query && !searchSnapshot) searchSnapshot = details.map(item => item.open);
        let count = 0;
        chapters.forEach(chapter => {
            const match = !query || chapter.textContent.toLocaleLowerCase().includes(query);
            chapter.hidden = !match;
            if (match) count++;
            if (query) chapter.querySelectorAll('details').forEach(item => {
                item.open = item.textContent.toLocaleLowerCase().includes(query);
            });
        });
        if (!query && searchSnapshot) {
            details.forEach((item, index) => { item.open = searchSnapshot[index]; });
            searchSnapshot = null;
        }
        document.getElementById('no-results').hidden = count > 0;
        document.getElementById('search-status').textContent = query ? `找到 ${count} 个相关章节` : '';
    }
    document.getElementById('search-bar').hidden = false;
    search.addEventListener('input', filterChapters);
    document.getElementById('clear-search').addEventListener('click', () => {
        search.value = ''; filterChapters(); search.focus();
    });
    // Chapter links always work, even if a previous search hid their destination.
    document.querySelectorAll('a[href^="#"]').forEach(link => {
        link.addEventListener('click', () => { search.value = ''; filterChapters(); });
    });
    function selectChapter(id) {
        links.forEach(link => {
            if (link.hash === `#${id}`) link.setAttribute('aria-current', 'location');
            else link.removeAttribute('aria-current');
        });
    }
    let scrollScheduled = false;
    function updateActiveChapter() {
        const visible = chapters.filter(chapter => !chapter.hidden);
        let current = visible[0];
        for (const chapter of visible) {
            if (chapter.getBoundingClientRect().top <= 180) current = chapter;
        }
        if (current) selectChapter(current.id);
        scrollScheduled = false;
    }
    function scheduleActiveChapter() {
        if (scrollScheduled) return;
        scrollScheduled = true;
        window.requestAnimationFrame(updateActiveChapter);
    }
    window.addEventListener('scroll', scheduleActiveChapter, {passive: true});
    window.addEventListener('resize', scheduleActiveChapter);
    updateActiveChapter();
    links.forEach(link => link.addEventListener('click', () => selectChapter(link.hash.slice(1))));
    const key = 'zeshun-beginner-guide-progress-v1';
    const checks = [...document.querySelectorAll('.checklist input')];
    let storageAvailable = true;
    try {
        const saved = JSON.parse(localStorage.getItem(key) || '[]');
        if (Array.isArray(saved)) checks.forEach(check => { check.checked = saved.includes(check.value); });
    } catch (_) { storageAvailable = false; }
    function renderProgress() {
        const count = checks.filter(check => check.checked).length;
        document.getElementById('learning-progress').value = count;
        document.getElementById('progress-text').textContent = count === checks.length ? `入门清单已完成 · ${count} / ${checks.length}` : `已完成 ${count} / ${checks.length}`;
        document.getElementById('progress-percent').textContent = `${Math.round(count / checks.length * 100)}%`;
        document.getElementById('reset-progress').disabled = count === 0;
        document.getElementById('storage-note').textContent = storageAvailable
            ? '学习进度保存在当前浏览器，同一浏览器共用；不会执行业务操作，也不会同步到账号。'
            : '浏览器无法保存进度，本次勾选仅在当前页面有效；不会执行业务操作。';
    }
    function saveProgress() {
        try {
            localStorage.setItem(key, JSON.stringify(checks.filter(check => check.checked).map(check => check.value)));
            storageAvailable = true;
        } catch (_) { storageAvailable = false; }
        renderProgress();
    }
    checks.forEach(check => check.addEventListener('change', saveProgress));
    document.getElementById('reset-progress').addEventListener('click', () => {
        checks.forEach(check => { check.checked = false; }); saveProgress();
    });
    document.getElementById('progress-ui').hidden = false;
    document.getElementById('reset-progress').hidden = false;
    renderProgress();
    let printSnapshot;
    window.addEventListener('beforeprint', () => {
        if (!printSnapshot) printSnapshot = details.map(item => item.open);
        details.forEach(item => { item.open = true; });
    });
    window.addEventListener('afterprint', () => {
        if (printSnapshot) details.forEach((item, index) => { item.open = printSnapshot[index]; });
        printSnapshot = null;
    });
    const printButton = document.getElementById('print-guide');
    printButton.hidden = false;
    printButton.addEventListener('click', () => window.print());
})();
