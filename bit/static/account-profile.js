(() => {
    'use strict';
    const profileForm = document.getElementById('profile-form');
    const passwordForm = document.getElementById('password-form');
    const profileFields = document.getElementById('profile-fields');
    const passwordFields = document.getElementById('password-fields');
    const retry = document.getElementById('retry-load');
    let csrf = document.querySelector('meta[name="csrf-token"]').content;
    const fields = ['personal_name', 'email', 'department', 'wechat_id', 'phone'];
    const message = (id, text, error = false) => {
        const element = document.getElementById(id);
        element.textContent = text;
        element.className = `message ${error ? 'error' : 'success'}`;
    };
    async function request(path, method = 'GET', data) {
        let response;
        try {
            response = await fetch(path, {
                method, credentials: 'same-origin', cache: 'no-store',
                headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
                ...(data ? {body: JSON.stringify(data)} : {}),
            });
        } catch (_) { throw new Error('网络连接失败，请稍后重试'); }
        let payload;
        try { payload = await response.json(); }
        catch (_) { throw new Error('服务暂时不可用，请稍后重试'); }
        if (!response.ok || payload.status !== 'success') {
            throw new Error(payload.message || '操作失败，请稍后重试');
        }
        if (payload.csrf_token) csrf = payload.csrf_token;
        return payload;
    }
    function fill(data) {
        document.getElementById('username').value = data.username || '';
        document.getElementById('salesperson').value = data.salesperson || '';
        fields.forEach(field => { profileForm.elements[field].value = data[field] || ''; });
    }
    async function load() {
        retry.hidden = true;
        message('load-message', '正在读取个人资料…');
        try {
            fill((await request('/api/account/profile')).data);
            profileFields.disabled = false;
            passwordFields.disabled = false;
            message('load-message', '');
        } catch (error) {
            message('load-message', error.message, true);
            retry.hidden = false;
        }
    }
    profileForm.addEventListener('submit', async event => {
        event.preventDefault();
        const data = Object.fromEntries(fields.map(field => [field, profileForm.elements[field].value]));
        profileFields.disabled = true;
        passwordFields.disabled = true;
        message('profile-message', '正在保存…');
        try {
            fill((await request('/api/account/profile', 'PUT', data)).data);
            message('profile-message', '个人资料已保存');
        } catch (error) { message('profile-message', error.message, true); }
        finally {
            profileFields.disabled = false;
            passwordFields.disabled = false;
        }
    });
    passwordForm.addEventListener('submit', async event => {
        event.preventDefault();
        const data = Object.fromEntries(['current_password', 'new_password', 'confirm_password']
            .map(field => [field, passwordForm.elements[field].value]));
        if (data.new_password !== data.confirm_password) {
            message('password-message', '两次输入的新密码不一致', true);
            return;
        }
        if (data.new_password === data.current_password) {
            message('password-message', '新密码不能与原密码相同', true);
            return;
        }
        profileFields.disabled = true;
        passwordFields.disabled = true;
        message('password-message', '正在修改…');
        try {
            await request('/api/account/password', 'POST', data);
            passwordForm.reset();
            message('password-message', '密码已修改，正在前往登录页…');
            window.location.assign('/login');
        } catch (error) {
            message('password-message', error.message, true);
            profileFields.disabled = false;
            passwordFields.disabled = false;
        }
    });
    retry.addEventListener('click', load);
    load();
})();
