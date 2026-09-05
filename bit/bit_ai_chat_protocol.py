"""DOM message identity and bounded waits for both supported assistant layouts."""

import re

from bit.bit_appeal_state import AppealExecutionError


class ChatMessages(list):
    def __init__(self, snapshot):
        super().__init__(m["text"] for m in snapshot["messages"] if m["role"] == "assistant")
        self.snapshot = snapshot


def normalized_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def read_snapshot(driver):
    snapshot = driver.execute_script(r"""
        const all = (root) => {
            const nodes = [];
            if (root.shadowRoot) nodes.push(...all(root.shadowRoot));
            for (const el of root.querySelectorAll('*')) {
                nodes.push(el);
                if (el.shadowRoot) nodes.push(...all(el.shadowRoot));
            }
            return nodes;
        };
        const visible = el => {
            const r = el.getBoundingClientRect(), s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
        };
        const nodes = all(document);
        const shell = nodes.find(el => el.id === 'sa-assistant-chat');
        const scope = shell ? all(shell) : nodes;
        const assistant = '.message-item--assistant, .message-item--agent, .chat-ui-message-bubble--from-agent, [class*="message-bubble--from-agent"], [data-role="assistant"], [data-role="agent"], [data-author="assistant"], [data-author="agent"], [data-sender="assistant"], [data-sender="agent"], [data-message-author="assistant"], [data-message-author="agent"], .assistant-message, .agent-message';
        const user = '.message-item--user, .message-item--seller, .chat-ui-message-bubble--from-user, [class*="message-bubble--from-user"], [data-role="user"], [data-role="seller"], [data-author="user"], [data-author="seller"], [data-sender="user"], [data-sender="seller"], [data-message-author="user"], [data-message-author="seller"], .user-message, .seller-message';
        if (!window.__bitAppealMessageIdentity) {
            window.__bitAppealMessageIdentity = { epoch: String(Date.now()) + '-' + Math.random(), ids: new WeakMap(), next: 1 };
        }
        const identity = window.__bitAppealMessageIdentity;
        const messages = scope.filter(el => visible(el) && (el.matches(user) || el.matches(assistant)))
            .filter(el => !all(el).some(child => visible(child) && (child.matches(user) || child.matches(assistant))))
            .map(el => {
                if (!identity.ids.has(el)) identity.ids.set(el, String(identity.next++));
                const owner = el.closest('[data-message-id], [data-id], [id]') || el;
                return {
                    id: owner.getAttribute('data-message-id') || owner.getAttribute('data-id') || el.id || identity.ids.get(el),
                    role: el.matches(user) ? 'user' : 'assistant',
                    text: (el.innerText || '').trim()
                };
            }).filter(m => m.text);
        const url = new URL(location.href);
        return {
            conversation_id: url.searchParams.get('conversation_id') || '',
            epoch: identity.epoch,
            messages,
            busy: scope.some(el => visible(el) && (el.matches('[aria-busy="true"], .thinking-indicator, [class*="typing-indicator"]')))
        };
    """)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("messages"), list):
        raise RuntimeError("无法读取 AI 客服消息结构")
    return snapshot


def new_messages(before, after, role):
    """Stable IDs survive list truncation; identical replies with new IDs stay new."""
    if before.get("epoch") != after.get("epoch"):
        raise AppealExecutionError("等待期间客服会话已更换", "sent_unknown", sent=True)
    if before.get("conversation_id") != after.get("conversation_id"):
        raise AppealExecutionError("等待期间客服会话编号已更换", "sent_unknown", sent=True)
    old = {(m["role"], m["id"]): normalized_text(m["text"]) for m in before["messages"]}
    return [m for m in after["messages"] if m["role"] == role
            and old.get((role, m["id"])) != normalized_text(m["text"])]
