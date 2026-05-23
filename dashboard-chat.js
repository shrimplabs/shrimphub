
let chatHistory = [];

function toggleChat() {
    const panel = document.getElementById('chatPanel');
    panel.classList.toggle('open');
    if (panel.classList.contains('open')) {
        document.getElementById('chatInput').focus();
    }
}

document.getElementById('chatInput') && document.getElementById('chatInput').addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChat();
    }
});

async function sendChat() {
    const input = document.getElementById('chatInput');
    const msg = input.value.trim();
    if (!msg) return;

    const sendBtn = document.getElementById('chatSendBtn');
    const messages = document.getElementById('chatMessages');

    // Show user message
    appendChatMsg('user', msg);
    input.value = '';
    sendBtn.disabled = true;

    // Show thinking indicator
    const thinking = document.createElement('div');
    thinking.className = 'chat-msg thinking';
    thinking.id = 'chatThinking';
    thinking.textContent = 'Thinking...';
    messages.appendChild(thinking);
    messages.scrollTop = messages.scrollHeight;

    try {
        const resp = await fetch('/api/chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message: msg, history: chatHistory}),
        });
        const data = await resp.json();

        thinking.remove();

        if (data.error) {
            appendChatMsg('assistant', 'Error: ' + data.error);
        } else {
            chatHistory = data.history || [];
            appendChatMsg('assistant', data.response);
        }
    } catch (e) {
        thinking.remove();
        appendChatMsg('assistant', 'Network error — is the server running?');
    }

    sendBtn.disabled = false;
    input.focus();
}

function appendChatMsg(role, text) {
    const messages = document.getElementById('chatMessages');
    const div = document.createElement('div');
    div.className = 'chat-msg ' + role;
    div.innerHTML = role === 'user' ? escapeHtml(text).replace(/\n/g,'<br>') : renderMarkdown(text);
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
}

// ---- Project Debug Chat ----

let _debugProject = '';
let _debugSessionId = '';
let _debugAbortController = null;

function openDebugChat(project) {
    _debugProject = project;
    _debugSessionId = localStorage.getItem('debug_session_' + project) || '';
    document.getElementById('debugChatTitle').textContent = '🔍 ' + project;
    const panel = document.getElementById('debugChatPanel');
    panel.classList.add('open');
    document.getElementById('debugChatInput').focus();
    if (!_debugSessionId) {
        _appendDebugMsg('assistant', 'Hi! I can read files, run commands, inspect logs, and query the task graph for **' + project + '**. What do you need?');
    }
}

function toggleDebugChat() {
    document.getElementById('debugChatPanel').classList.remove('open');
}

function debugNewSession() {
    if (_debugProject) localStorage.removeItem('debug_session_' + _debugProject);
    _debugSessionId = '';
    document.getElementById('debugChatMessages').innerHTML = '';
    _appendDebugMsg('assistant', 'Started a new conversation. What do you need?');
    document.getElementById('debugUndoBtn').style.display = 'none';
}

async function debugStop() {
    if (_debugAbortController) {
        _debugAbortController.abort();
        _debugAbortController = null;
    }
    if (!_debugSessionId) return;
    document.getElementById('debugStopBtn').style.display = 'none';
    document.getElementById('debugSendBtn').disabled = false;
    try {
        const r = await fetch('/api/project-debug/' + _debugSessionId + '/stop', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({project: _debugProject}),
        });
        const data = await r.json();
        const thinking = document.getElementById('debugThinking');
        if (thinking) thinking.remove();
        _appendDebugMsg('stopped', data.response || '⚠️ Stopped. Tell me what to do instead.');
        document.getElementById('debugUndoBtn').style.display = '';
    } catch(e) {
        _appendDebugMsg('stopped', '⚠️ Stopped.');
    }
}

async function debugRollback() {
    if (!_debugSessionId) return;
    try {
        await fetch('/api/project-debug/' + _debugSessionId + '/last', {
            method: 'DELETE',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({project: _debugProject}),
        });
        // Remove last two message elements (user + assistant/stopped)
        const msgs = document.getElementById('debugChatMessages');
        for (let i = 0; i < 2; i++) {
            if (msgs.lastElementChild) msgs.removeChild(msgs.lastElementChild);
        }
        document.getElementById('debugUndoBtn').style.display = 'none';
    } catch(e) { /* ignore */ }
}

document.getElementById('debugChatInput') && document.getElementById('debugChatInput').addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        e.preventDefault();
        const stopBtn = document.getElementById('debugStopBtn');
        if (stopBtn.style.display !== 'none') {
            debugStop();
        } else {
            toggleDebugChat();
        }
        return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendDebugChat();
    }
});

async function sendDebugChat() {
    const input = document.getElementById('debugChatInput');
    const msg = input.value.trim();
    if (!msg || !_debugProject) return;

    const sendBtn = document.getElementById('debugSendBtn');
    const stopBtn = document.getElementById('debugStopBtn');
    const undoBtn = document.getElementById('debugUndoBtn');

    _appendDebugMsg('user', msg);
    input.value = '';
    sendBtn.disabled = true;
    stopBtn.style.display = '';
    undoBtn.style.display = 'none';

    const thinking = document.createElement('div');
    thinking.className = 'debug-msg thinking';
    thinking.id = 'debugThinking';
    thinking.textContent = 'Thinking...';
    document.getElementById('debugChatMessages').appendChild(thinking);
    document.getElementById('debugChatMessages').scrollTop = 99999;

    _debugAbortController = new AbortController();

    try {
        const resp = await fetch('/api/project-debug', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            signal: _debugAbortController.signal,
            body: JSON.stringify({
                project: _debugProject,
                message: msg,
                session_id: _debugSessionId || undefined,
            }),
        });
        const data = await resp.json();
        thinking.remove();

        if (data.session_id) {
            _debugSessionId = data.session_id;
            localStorage.setItem('debug_session_' + _debugProject, _debugSessionId);
        }

        if (data.error) {
            _appendDebugMsg('assistant', 'Error: ' + data.error);
        } else {
            _appendDebugMsgWithTools('assistant', data.response, data.tool_calls || []);
            undoBtn.style.display = '';
        }
    } catch(e) {
        thinking.remove();
        if (e.name !== 'AbortError') {
            _appendDebugMsg('assistant', 'Network error — is the server running?');
        }
    } finally {
        _debugAbortController = null;
        sendBtn.disabled = false;
        stopBtn.style.display = 'none';
        input.focus();
    }
}

function _appendDebugMsg(role, text) {
    const msgs = document.getElementById('debugChatMessages');
    const div = document.createElement('div');
    div.className = 'debug-msg ' + role;
    div.innerHTML = role === 'user'
        ? escapeHtml(text).replace(/\n/g, '<br>')
        : renderMarkdown(text);
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
}

function _appendDebugMsgWithTools(role, text, toolCalls) {
    const msgs = document.getElementById('debugChatMessages');
    const wrapper = document.createElement('div');
    wrapper.className = 'debug-msg ' + role;
    wrapper.innerHTML = renderMarkdown(text);

    for (const tc of toolCalls) {
        const block = document.createElement('div');
        block.className = 'debug-tool-call';
        const header = document.createElement('div');
        header.className = 'debug-tool-call-header';
        header.innerHTML = '<span class="tool-arrow">▶</span> ' +
            escapeHtml(tc.tool) + '(' +
            escapeHtml(JSON.stringify(tc.args || {})).slice(0, 80) + ')';
        header.onclick = function() {
            this.classList.toggle('open');
        };
        const body = document.createElement('div');
        body.className = 'debug-tool-call-body';
        body.textContent = typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2);
        block.appendChild(header);
        block.appendChild(body);
        wrapper.appendChild(block);
    }

    msgs.appendChild(wrapper);
    msgs.scrollTop = msgs.scrollHeight;
}

// ─── Unified Chat ────────────────────────────────────────────────────────────

const _unifiedChat = {
    sessionIds: {},      // scope -> session_id
    currentScope: null,  // null = global, string = project name
    pendingConfirm: null, // {token, resolve} for confirm dialog
    abortCtrl: null,
};

let _unifiedContextDebounce = 0;

function openUnifiedChat(project) {
    const panel = document.getElementById('unifiedChatPanel');
    if (!panel) return;
    const prevScope = _unifiedChat.currentScope;
    const newScope = project || null;
    const scopeChanged = prevScope !== newScope;
    const wasOpen = panel.classList.contains('open');

    _unifiedChat.currentScope = newScope;
    _updateUnifiedChatScope();
    panel.classList.add('open');
    panel.style.display = 'flex';

    // Restore session from localStorage
    const scopeKey = newScope || '_global';
    if (!_unifiedChat.sessionIds[scopeKey]) {
        const stored = localStorage.getItem('unified_session_' + scopeKey);
        if (stored) _unifiedChat.sessionIds[scopeKey] = stored;
    }

    const msgs = document.getElementById('unifiedChatMessages');

    // On scope switch: clear messages and show a fresh greeting for the new scope
    if (scopeChanged && wasOpen) {
        msgs.innerHTML = '';
        _appendUnifiedMsg('assistant',
            newScope
                ? `Swarm — now focused on **${newScope}**. What do you need?`
                : "Swarm — global view. What do you need?");
        // Inject live context bubble (not debounced on explicit switch)
        _unifiedContextDebounce = Date.now();
        _injectScopeContext(newScope);
    } else if (msgs.children.length === 0) {
        // First open ever — show greeting
        _appendUnifiedMsg('assistant',
            newScope
                ? `Swarm — focused on **${newScope}**. What do you need?`
                : "Swarm — watching all projects, tasks, and agents. What do you need?");
    }

    document.getElementById('unifiedChatInput').focus();
}

async function _injectScopeContext(project) {
    const scopeLabel = project ? `Project: ${project}` : 'Swarm (global)';
    const contextMsg = document.createElement('div');
    contextMsg.className = 'unified-msg context-inject';
    contextMsg.textContent = `📍 Switched to ${scopeLabel} — loading context…`;
    const msgs = document.getElementById('unifiedChatMessages');
    msgs.appendChild(contextMsg);
    msgs.scrollTop = msgs.scrollHeight;

    try {
        const scope = project || '_global';
        const sessionId = _unifiedChat.sessionIds[scope];
        const body = {
            message: `__context_inject__: Switched scope to ${scopeLabel}. Briefly summarize what you know about the current state without asking any questions.`,
            session_id: sessionId,
        };
        if (project) body.project = project;

        const resp = await fetch('/api/unified-chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        contextMsg.remove();
        const inject = document.createElement('div');
        inject.className = 'unified-msg context-inject';
        inject.innerHTML = '📍 <strong>' + escapeHtml(scopeLabel) + '</strong><br>' +
            renderMarkdown(data.reply || '(no context available)');
        msgs.appendChild(inject);
        msgs.scrollTop = msgs.scrollHeight;

        // Store session id
        if (data.session_id) {
            _unifiedChat.sessionIds[scope] = data.session_id;
            localStorage.setItem('unified_session_' + scope, data.session_id);
        }
    } catch(e) {
        contextMsg.textContent = `📍 Switched to ${scopeLabel}`;
    }
}

function closeUnifiedChat() {
    const panel = document.getElementById('unifiedChatPanel');
    if (panel) { panel.classList.remove('open'); panel.style.display = 'none'; }
}

function _updateUnifiedChatScope() {
    const scopeEl = document.getElementById('unifiedChatScope');
    if (!scopeEl) return;
    scopeEl.textContent = _unifiedChat.currentScope
        ? '💬 ' + _unifiedChat.currentScope
        : '💬 Swarm';
}

function unifiedChatNewSession() {
    const scope = _unifiedChat.currentScope || '_global';
    delete _unifiedChat.sessionIds[scope];
    const msgs = document.getElementById('unifiedChatMessages');
    if (msgs) msgs.innerHTML = '';
    _appendUnifiedMsg('assistant', _unifiedChat.currentScope
        ? `New session — focused on **${_unifiedChat.currentScope}**. What do you need?`
        : 'New session. What do you need?');
    // Persist new session
    const key = 'unified_session_' + scope;
    localStorage.removeItem(key);
}

async function unifiedChatStop() {
    const sessionId = _unifiedChat.sessionIds[_unifiedChat.currentScope || '_global'];
    if (!sessionId) return;
    if (_unifiedChat.abortCtrl) {
        _unifiedChat.abortCtrl.abort();
        _unifiedChat.abortCtrl = null;
    }
    try {
        await fetch('/api/unified-chat/' + sessionId + '/stop', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({}) });
    } catch(e) {}
    document.getElementById('unifiedStopBtn').style.display = 'none';
    _appendUnifiedMsg('stopped', '⚠️ Stopped. Tell me what to do instead.');
}

// Keyboard: Esc to stop when panel is focused
document.addEventListener('keydown', function(e) {
    const panel = document.getElementById('unifiedChatPanel');
    if (e.key === 'Escape' && panel && panel.classList.contains('open')) {
        const stopBtn = document.getElementById('unifiedStopBtn');
        if (stopBtn && stopBtn.style.display !== 'none') {
            e.preventDefault();
            unifiedChatStop();
        } else {
            closeUnifiedChat();
        }
    }
});

document.getElementById('unifiedChatInput') && document.getElementById('unifiedChatInput').addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendUnifiedChat(); }
});

async function sendUnifiedChat() {
    const input = document.getElementById('unifiedChatInput');
    const sendBtn = document.getElementById('unifiedSendBtn');
    const stopBtn = document.getElementById('unifiedStopBtn');
    const msg = (input.value || '').trim();
    if (!msg) return;
    input.value = '';

    const scope = _unifiedChat.currentScope || '_global';
    let sessionId = _unifiedChat.sessionIds[scope];
    if (!sessionId) {
        const stored = localStorage.getItem('unified_session_' + scope);
        sessionId = stored || (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2));
        _unifiedChat.sessionIds[scope] = sessionId;
        localStorage.setItem('unified_session_' + scope, sessionId);
    }

    _appendUnifiedMsg('user', msg);
    sendBtn.disabled = true;
    stopBtn.style.display = '';

    const thinking = document.createElement('div');
    thinking.className = 'unified-msg thinking';
    thinking.textContent = '…';
    document.getElementById('unifiedChatMessages').appendChild(thinking);

    _unifiedChat.abortCtrl = new AbortController();
    try {
        const body = { message: msg, session_id: sessionId };
        if (_unifiedChat.currentScope) body.project = _unifiedChat.currentScope;

        const resp = await fetch('/api/unified-chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            signal: _unifiedChat.abortCtrl.signal,
        });
        thinking.remove();

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            // Check for confirm challenge in tool_calls
            if (err.requires_confirmation) {
                _handleUnifiedConfirmChallenge(err);
                return;
            }
            _appendUnifiedMsg('assistant', 'Error: ' + (err.error || resp.status));
            return;
        }

        const data = await resp.json();
        // Check if any tool call returned a confirmation challenge
        const challenge = (data.tool_calls || []).find(tc => {
            try { return JSON.parse(tc.result || '{}').requires_confirmation; } catch(e) { return false; }
        });
        if (challenge) {
            const parsed = JSON.parse(challenge.result);
            _handleUnifiedConfirmChallenge(parsed, sessionId);
        } else {
            _appendUnifiedMsgWithTools('assistant', data.reply || '', data.tool_calls || []);
        }
    } catch(e) {
        thinking.remove();
        if (e.name !== 'AbortError') {
            _appendUnifiedMsg('assistant', 'Network error — is the server running?');
        }
    } finally {
        _unifiedChat.abortCtrl = null;
        sendBtn.disabled = false;
        stopBtn.style.display = 'none';
        input.focus();
    }
}

function _handleUnifiedConfirmChallenge(challenge, sessionId) {
    const dialog = document.getElementById('unifiedConfirmDialog');
    const desc = document.getElementById('unifiedConfirmDesc');
    if (!dialog || !desc) return;
    desc.textContent = 'Are you sure you want to: ' + (challenge.action || 'this action') + '?';
    _unifiedChat.pendingConfirm = { token: challenge.confirm_token, sessionId };
    dialog.style.display = 'flex';
}

function unifiedConfirmCancel() {
    _unifiedChat.pendingConfirm = null;
    const dialog = document.getElementById('unifiedConfirmDialog');
    if (dialog) dialog.style.display = 'none';
    _appendUnifiedMsg('assistant', 'Action cancelled.');
}

async function unifiedConfirmExecute() {
    const dialog = document.getElementById('unifiedConfirmDialog');
    if (dialog) dialog.style.display = 'none';
    const pending = _unifiedChat.pendingConfirm;
    _unifiedChat.pendingConfirm = null;
    if (!pending) return;

    // Re-send last user message with confirm_token injected
    // The simplest approach: send a synthetic message that includes the token
    const scope = _unifiedChat.currentScope || '_global';
    const sessionId = pending.sessionId || _unifiedChat.sessionIds[scope];
    const body = {
        message: '__confirm__',
        session_id: sessionId,
        confirm_token: pending.token,
    };
    if (_unifiedChat.currentScope) body.project = _unifiedChat.currentScope;

    try {
        const resp = await fetch('/api/unified-chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        _appendUnifiedMsgWithTools('assistant', data.reply || data.error || 'Done.', data.tool_calls || []);
    } catch(e) {
        _appendUnifiedMsg('assistant', 'Confirmation failed: ' + e.message);
    }
}

function _appendUnifiedMsg(role, text) {
    const msgs = document.getElementById('unifiedChatMessages');
    if (!msgs) return;
    const div = document.createElement('div');
    div.className = 'unified-msg ' + role;
    div.innerHTML = role === 'user'
        ? escapeHtml(text).replace(/\n/g, '<br>')
        : renderMarkdown(text);
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
}

function _appendUnifiedMsgWithTools(role, text, toolCalls) {
    const msgs = document.getElementById('unifiedChatMessages');
    if (!msgs) return;
    const wrapper = document.createElement('div');
    wrapper.className = 'unified-msg ' + role;
    wrapper.innerHTML = renderMarkdown(text);

    for (const tc of toolCalls) {
        const block = document.createElement('div');
        block.className = 'unified-tool-call';
        const header = document.createElement('div');
        header.className = 'unified-tool-call-header';
        header.innerHTML = '<span class="tool-arrow">▶</span> ' +
            escapeHtml(tc.tool) + '(' +
            escapeHtml(JSON.stringify(tc.args || {})).slice(0, 80) + ')';
        header.onclick = function() { this.classList.toggle('open'); };
        const body = document.createElement('div');
        body.className = 'unified-tool-call-body';
        body.textContent = typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2);
        block.appendChild(header);
        block.appendChild(body);
        wrapper.appendChild(block);
    }

    msgs.appendChild(wrapper);
    msgs.scrollTop = msgs.scrollHeight;
}
