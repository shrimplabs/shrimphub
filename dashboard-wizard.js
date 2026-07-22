let _npHistory = [];
let _npCreated = false;
let _npTasksPreview = [];
let _npProjectName = '';
let _npOverview = '';
let _npQualityGates = [];
let _npDesignDoc = '';
let _npProjectType = 'godot';
const NP_DRAFT_STORAGE_KEY = 'swarm.newProjectDraft.v1';

function normalizeNpProjectType(value) {
    if (value === 'python') return 'python';
    if (value === 'xogot') return 'xogot';
    return 'godot';
}

function getNpProjectType() {
    const select = document.getElementById('npProjectType');
    return normalizeNpProjectType(select?.value || _npProjectType);
}

function setNpProjectType(value) {
    _npProjectType = normalizeNpProjectType(value);
    const select = document.getElementById('npProjectType');
    if (select) select.value = _npProjectType;
}

function handleNpProjectTypeChange() {
    setNpProjectType(getNpProjectType());
    saveNpDraft();
}

function saveNpDraft() {
    try {
        const payload = {
            history: _npHistory,
            created: _npCreated,
            tasksPreview: _npTasksPreview,
            projectName: _npProjectName,
            projectType: getNpProjectType(),
            overview: _npOverview,
            qualityGates: _npQualityGates,
            designDoc: _npDesignDoc,
            input: document.getElementById('npInput')?.value || '',
        };
        localStorage.setItem(NP_DRAFT_STORAGE_KEY, JSON.stringify(payload));
    } catch (e) {}
}

function loadNpDraft() {
    try {
        const raw = localStorage.getItem(NP_DRAFT_STORAGE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (!parsed || !Array.isArray(parsed.history)) return null;
        return parsed;
    } catch (e) {
        return null;
    }
}

function clearNpDraft() {
    try { localStorage.removeItem(NP_DRAFT_STORAGE_KEY); } catch (e) {}
}

function npGreetingMessage() {
    if (getNpProjectType() === 'python') {
        return "Let's design your software project. I'll ask a few questions, then generate a task list for you to review and confirm.\n\nWhat's the project idea?";
    }
    return "Let's design your game. I'll ask a few questions, then generate a task list for you to review and confirm.\n\nWhat's the game idea?";
}

function resetNpState() {
    _npHistory = [];
    _npCreated = false;
    _npTasksPreview = [];
    _npProjectName = '';
    _npOverview = '';
    _npQualityGates = [];
    _npDesignDoc = '';
    setNpProjectType('godot');
    document.getElementById('npMessages').innerHTML = '';
    document.getElementById('npInput').value = '';
    document.getElementById('npInput').disabled = false;
    document.getElementById('npSendBtn').disabled = false;
    document.getElementById('npIdeasBtn').disabled = false;
    const typeSelect = document.getElementById('npProjectType');
    if (typeSelect) typeSelect.disabled = false;
    document.getElementById('npCreatedBanner').style.display = 'none';
}

function seedNewProjectGreeting() {
    const greeting = npGreetingMessage();
    appendNpMessage('assistant', greeting);
    _npHistory = [{role: 'assistant', content: greeting}];
    saveNpDraft();
}

function renderNpHistory() {
    const messagesEl = document.getElementById('npMessages');
    messagesEl.innerHTML = '';
    for (const msg of _npHistory) {
        if (msg?.role === 'user' || msg?.role === 'assistant') {
            appendNpMessage(msg.role, msg.content || '');
        }
    }
}

function openNewProjectPanel() {
    const draft = loadNpDraft();
    resetNpState();
    document.getElementById('newProjectPanel').style.display = 'flex';
    if (draft && (draft.history.length || (draft.tasksPreview || []).length)) {
        _npHistory = draft.history || [];
        _npCreated = Boolean(draft.created);
        _npTasksPreview = draft.tasksPreview || [];
        _npProjectName = draft.projectName || '';
        setNpProjectType(draft.projectType || 'godot');
        _npOverview = draft.overview || '';
        _npQualityGates = Array.isArray(draft.qualityGates) ? draft.qualityGates : [];
        _npDesignDoc = typeof draft.designDoc === 'string' ? draft.designDoc : '';
        renderNpHistory();
        document.getElementById('npInput').value = draft.input || '';
        if (_npCreated && _npTasksPreview.length) {
            showNpTaskPreview(_npProjectName, _npTasksPreview);
            document.getElementById('npInput').disabled = true;
            document.getElementById('npSendBtn').disabled = true;
            const typeSelect = document.getElementById('npProjectType');
            if (typeSelect) typeSelect.disabled = true;
        } else if (_npHistory.length) {
            appendNpMessage('assistant', 'Resumed previous project chat draft.');
        }
    } else {
        // Show greeting
        seedNewProjectGreeting();
    }
    setTimeout(() => document.getElementById('npInput').focus(), 50);
}

function startFreshNewProjectChat() {
    clearNpDraft();
    resetNpState();
    seedNewProjectGreeting();
    setTimeout(() => document.getElementById('npInput').focus(), 50);
}

function closeNewProjectPanel() {
    saveNpDraft();
    document.getElementById('newProjectPanel').style.display = 'none';
}

document.getElementById('newProjectPanel').addEventListener('click', function(e) {
    if (e.target === this) closeNewProjectPanel();
});

function renderMarkdown(text) {
    // Escape HTML first
    let s = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    // Code blocks (```...```)
    s = s.replace(/```[\w]*\n?([\s\S]*?)```/g, '<pre style="background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px 10px;margin:6px 0;overflow-x:auto;font-size:12px">$1</pre>');
    // Inline code
    s = s.replace(/`([^`]+)`/g, '<code style="background:#0d1117;padding:1px 5px;border-radius:4px;font-size:12px">$1</code>');
    // Bold
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // Italic
    s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    // Split into paragraphs on double newline
    const parts = s.split(/\n\n+/);
    return parts.map(para => {
        const lines = para.split('\n');
        // Detect list block (lines starting with - or A. B. etc.)
        const isBullet = lines.every(l => /^\s*[-*]\s/.test(l) || l.trim() === '');
        const isLettered = lines.every(l => /^\s*[A-Z]\.\s/.test(l) || l.trim() === '');
        const isNumbered = lines.every(l => /^\s*\d+\.\s/.test(l) || l.trim() === '');
        if (isBullet) {
            const items = lines.filter(l => l.trim()).map(l => `<li style="margin:2px 0">${l.replace(/^\s*[-*]\s/, '')}</li>`).join('');
            return `<ul style="margin:4px 0 4px 16px;padding:0">${items}</ul>`;
        }
        if (isLettered) {
            const items = lines.filter(l => l.trim()).map(l => `<li style="margin:2px 0;list-style:none">${l.replace(/^\s*/, '')}</li>`).join('');
            return `<ul style="margin:4px 0 4px 4px;padding:0">${items}</ul>`;
        }
        if (isNumbered) {
            const items = lines.filter(l => l.trim()).map(l => `<li style="margin:2px 0">${l.replace(/^\s*\d+\.\s/, '')}</li>`).join('');
            return `<ol style="margin:4px 0 4px 16px;padding:0">${items}</ol>`;
        }
        return `<p style="margin:4px 0">${lines.join('<br>')}</p>`;
    }).join('');
}

function appendNpMessage(role, text) {
    const el = document.getElementById('npMessages');
    const isUser = role === 'user';
    const div = document.createElement('div');
    div.style.cssText = `display:flex;${isUser ? 'justify-content:flex-end' : ''}`;
    const content = isUser ? escapeHtml(text).replace(/\n/g,'<br>') : renderMarkdown(text);
    const encoded = encodeURIComponent(text || '');
    div.innerHTML = `<div style="position:relative;max-width:88%;padding:8px 12px 8px 12px;border-radius:10px;font-size:13px;line-height:1.5;${
        isUser
            ? 'background:#1f4068;color:#c9d1d9;border-bottom-right-radius:2px'
            : 'background:#21262d;color:#e6edf3;border-bottom-left-radius:2px'
    }">
        <div>${content}</div>
        <div style="margin-top:8px;display:flex;justify-content:flex-end">
            <button
                onclick="copyNpMessage(this)"
                data-copy="${encoded}"
                title="Copy message"
                style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:1px 6px;font-size:11px;cursor:pointer"
            >Copy</button>
        </div>
    </div>`;
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
}

async function copyNpMessage(btn) {
    const raw = btn?.dataset?.copy || '';
    const text = decodeURIComponent(raw);
    try {
        await navigator.clipboard.writeText(text);
        const prev = btn.textContent;
        btn.textContent = 'Copied';
        setTimeout(() => { btn.textContent = prev; }, 1200);
    } catch (e) {
        btn.textContent = 'Failed';
        setTimeout(() => { btn.textContent = 'Copy'; }, 1200);
    }
}

function appendNpTyping() {
    const el = document.getElementById('npMessages');
    const div = document.createElement('div');
    div.id = 'npTyping';
    div.innerHTML = `<div style="padding:8px 12px;border-radius:10px;background:#21262d;color:#8b949e;font-size:13px">...</div>`;
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
}

async function sendNpMessage() {
    if (_npCreated) return;
    const input = document.getElementById('npInput');
    const text = input.value.trim();
    if (!text) return;

    input.value = '';
    input.style.height = '';
    appendNpMessage('user', text);
    appendNpTyping();
    document.getElementById('npSendBtn').disabled = true;
    saveNpDraft();

    try {
        const res = await fetch(`${API}/api/project-chat`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message: text, history: _npHistory, project_type: getNpProjectType()}),
        });
        const data = await res.json();
        document.getElementById('npTyping')?.remove();

        if (data.error) {
            appendNpMessage('assistant', 'Error: ' + data.error);
        } else {
            _npHistory = data.history;
            setNpProjectType(data.project_type || getNpProjectType());
            appendNpMessage('assistant', data.response);

            if (data.tasks_preview && data.tasks_preview.length > 0) {
                _npCreated = true;
                _npTasksPreview = data.tasks_preview;
                _npProjectName = data.project_name;
                setNpProjectType(data.project_type || getNpProjectType());
                _npOverview = data.overview || '';
                 _npQualityGates = Array.isArray(data.quality_gates) ? data.quality_gates : [];
                 _npDesignDoc = typeof data.design_doc === 'string' ? data.design_doc : '';
                showNpTaskPreview(data.project_name, data.tasks_preview);
                document.getElementById('npInput').disabled = true;
                document.getElementById('npSendBtn').disabled = true;
                const typeSelect = document.getElementById('npProjectType');
                if (typeSelect) typeSelect.disabled = true;
            }
            saveNpDraft();
        }
    } catch(e) {
        document.getElementById('npTyping')?.remove();
        appendNpMessage('assistant', 'Network error — please try again.');
        saveNpDraft();
    }

    document.getElementById('npSendBtn').disabled = false;
    document.getElementById('npInput').focus();
}

async function generateNpIdeas() {
    if (_npCreated) return;
    const btn = document.getElementById('npIdeasBtn');
    const sendBtn = document.getElementById('npSendBtn');
    const input = document.getElementById('npInput');
    const label = btn.textContent;
    const projectType = getNpProjectType();
    const ideaKind = projectType === 'python' ? 'programming' : 'game';
    const requestText = projectType === 'python'
        ? 'Generate programming project ideas.'
        : 'Generate game project ideas.';

    appendNpMessage('user', requestText);
    appendNpTyping();
    btn.disabled = true;
    sendBtn.disabled = true;
    btn.textContent = 'Thinking...';

    try {
        const res = await fetch(`${API}/api/project-ideas`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({kind: ideaKind, history: _npHistory}),
        });
        const data = await res.json();
        document.getElementById('npTyping')?.remove();

        if (data.error) {
            appendNpMessage('assistant', 'Error: ' + data.error);
        } else {
            _npHistory = data.history || [
                ..._npHistory,
                {role: 'user', content: requestText},
                {role: 'assistant', content: data.ideas || ''},
            ];
            appendNpMessage('assistant', data.ideas || 'No ideas returned.');
            saveNpDraft();
        }
    } catch (e) {
        document.getElementById('npTyping')?.remove();
        appendNpMessage('assistant', 'Network error — please try again.');
        saveNpDraft();
    } finally {
        if (!_npCreated) {
            btn.disabled = false;
            sendBtn.disabled = false;
        }
        btn.textContent = label;
        input.focus();
    }
}

document.getElementById('npInput').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendNpMessage(); }
});

function showNpTaskPreview(projectName, tasks) {
    const previewValidation = validateNpTaskPreview(tasks);
    const taskIdSet = new Set((tasks || []).map(t => t.id).filter(Boolean));
    const taskLabelMap = new Map((tasks || []).map(t => [t.id, (t.description || '').split('\n')[0] || t.id]));
    const banner = document.getElementById('npCreatedBanner');
    banner.style.display = 'block';
    banner.style.background = '#0d1117';
    banner.style.borderTop = '1px solid #30363d';
    banner.style.color = '#e6edf3';
    banner.style.padding = '12px 16px';
    banner.innerHTML = `
        <div style="font-size:13px;font-weight:600;margin-bottom:8px;color:#58a6ff">
            📋 ${tasks.length} tasks ready for <strong>${escapeHtml(projectName)}</strong>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;font-size:11px;color:#8b949e">
            <div style="padding:4px 8px;background:#161b22;border:1px solid #30363d;border-radius:999px">roots: ${previewValidation.stats.roots}</div>
            <div style="padding:4px 8px;background:#161b22;border:1px solid #30363d;border-radius:999px">branches: ${previewValidation.stats.branches}</div>
            <div style="padding:4px 8px;background:#161b22;border:1px solid #30363d;border-radius:999px">convergences: ${previewValidation.stats.convergences}</div>
        </div>
        ${previewValidation.errors.length ? `
            <div style="margin-bottom:10px;padding:8px 10px;background:#2d1517;border:1px solid #f85149;border-radius:6px;color:#fdaeb7;font-size:12px">
                <div style="font-weight:600;color:#f85149;margin-bottom:6px">Preview graph is invalid</div>
                <div style="margin-bottom:6px">${escapeHtml(previewValidation.summary)}</div>
                ${previewValidation.errors.map(err => `<div>• ${escapeHtml(err)}</div>`).join('')}
            </div>
        ` : `
            <div style="margin-bottom:10px;padding:8px 10px;background:#10241a;border:1px solid #238636;border-radius:6px;color:#9be9a8;font-size:12px">
                <div style="font-weight:600;color:#3fb950;margin-bottom:4px">Preview graph looks valid</div>
                <div>${escapeHtml(previewValidation.summary)}</div>
            </div>
        `}
        <div style="max-height:180px;overflow-y:auto;display:flex;flex-direction:column;gap:4px;margin-bottom:10px">
            ${tasks.map((t, i) => `
                <div style="font-size:12px;padding:5px 8px;background:#161b22;border-radius:4px;border:1px solid #30363d">
                    <div>
                        <span style="color:#8b949e;margin-right:6px">${i+1}.</span>
                        ${escapeHtml((t.description || '').split('\n')[0])}
                    </div>
                    <div style="margin-top:4px;color:#8b949e;font-size:11px;padding-left:18px">
                        deps: ${formatNpDependencies(t, taskIdSet, taskLabelMap)}
                    </div>
                </div>
            `).join('')}
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end">
            <button onclick="closeNewProjectPanel()" style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:6px;padding:6px 14px;font-size:12px;cursor:pointer">Cancel</button>
            <button onclick="confirmNpTasks()" style="background:${previewValidation.errors.length ? '#6e1a1a' : '#238636'};color:#fff;border:none;border-radius:6px;padding:6px 14px;font-size:12px;cursor:${previewValidation.errors.length ? 'not-allowed' : 'pointer'}" id="npConfirmBtn" ${previewValidation.errors.length ? 'disabled' : ''}>
                ✓ Create ${tasks.length} Tasks
            </button>
        </div>`;
}

function formatNpDependencies(task, taskIdSet, taskLabelMap) {
    const deps = Array.isArray(task?.dependencies) ? task.dependencies.filter(Boolean) : [];
    if (!deps.length) return 'none';
    return deps.map(dep => {
        if (taskIdSet.has(dep)) {
            const label = taskLabelMap?.get(dep) || dep;
            return `${escapeHtml(label)} <span style="color:#6e7681">(${escapeHtml(dep)})</span>`;
        }
        return `<span style="color:#d29922">${escapeHtml(dep)}</span>`;
    }).join(', ');
}

function validateNpTaskPreview(tasks) {
    const errors = [];
    const list = Array.isArray(tasks) ? tasks : [];
    const ids = list.map(t => t?.id).filter(Boolean);
    const idSet = new Set(ids);
    const externalDeps = new Set();
    const internalDepSets = [];
    let rootCount = 0;
    const indegree = new Map(ids.map(id => [id, 0]));
    const outdegree = new Map(ids.map(id => [id, 0]));

    for (const task of list) {
        const tid = task?.id;
        const deps = Array.isArray(task?.dependencies) ? task.dependencies.filter(Boolean) : [];
        const deduped = [...new Set(deps)];
        const internalDeps = deduped.filter(dep => idSet.has(dep));
        const unknownDeps = deduped.filter(dep => !idSet.has(dep));
        if (!deduped.length || !internalDeps.length) rootCount += 1;
        if (deduped.length !== deps.length) {
            errors.push(`Task ${tid || '(missing id)'} has duplicate dependencies.`);
        }
        if (tid && deduped.includes(tid)) {
            errors.push(`Task ${tid} depends on itself.`);
        }
        for (const dep of unknownDeps) externalDeps.add(dep);
        internalDepSets.push(internalDeps);
        if (tid) indegree.set(tid, internalDeps.length);
        for (const dep of internalDeps) {
            outdegree.set(dep, (outdegree.get(dep) || 0) + 1);
        }
    }

    if (!list.length) {
        errors.push('No tasks were generated.');
    }
    if (new Set(ids).size !== ids.length) {
        errors.push('Task IDs must be unique.');
    }
    if (!rootCount && list.length) {
        errors.push('Graph must have at least one root task.');
    }
    if (list.length >= 6) {
        const nonEmptyInternal = internalDepSets.filter(deps => deps.length > 0);
        if (nonEmptyInternal.length && nonEmptyInternal.every(deps => deps.length === 1)) {
            const dep0 = nonEmptyInternal[0][0];
            if (nonEmptyInternal.every(deps => deps[0] === dep0)) {
                errors.push('Preview graph is a trivial star: every task depends only on the same single task.');
            }
        } else if (!nonEmptyInternal.length && externalDeps.size === 1) {
            errors.push('Preview graph has no inter-task dependencies: every task depends only on the same external anchor.');
        }
    }

    const stats = {
        roots: rootCount,
        branches: [...outdegree.values()].filter(v => v > 1).length,
        convergences: [...indegree.values()].filter(v => v > 1).length,
    };
    const summary = errors.length
        ? 'This preview needs repair before creation. A healthy project graph should show a few early roots, multiple parallel branches, and later convergence points.'
        : `This plan has ${stats.roots} root task${stats.roots === 1 ? '' : 's'}, ${stats.branches} branching point${stats.branches === 1 ? '' : 's'}, and ${stats.convergences} convergence point${stats.convergences === 1 ? '' : 's'}.`;

    return {errors, stats, summary};
}

function reopenNewProjectChatForRetry(assistantMessage) {
    _npCreated = false;
    _npTasksPreview = [];
    if (assistantMessage) {
        _npHistory = [..._npHistory, {role: 'assistant', content: assistantMessage}];
        appendNpMessage('assistant', assistantMessage);
    }
    document.getElementById('npInput').disabled = false;
    document.getElementById('npSendBtn').disabled = false;
    const typeSelect = document.getElementById('npProjectType');
    if (typeSelect) typeSelect.disabled = false;
    saveNpDraft();
    document.getElementById('npInput').focus();
}

function summarizeClosureProposalForNp(proposal, projectName) {
    if (!proposal || typeof proposal !== 'object') return '';
    const spec = proposal.closure_spec || {};
    const criticalFlow = (Array.isArray(spec.critical_flows) && spec.critical_flows[0]) || {};
    const smokeCheck = (spec.verification && Array.isArray(spec.verification.smoke_checks) && spec.verification.smoke_checks[0]) || {};
    const mode = spec.mode || 'build';
    const profile = proposal.profile || 'unknown';
    const source = proposal.source || 'unknown';
    const flowId = criticalFlow.id || 'main-flow';
    const flowDesc = criticalFlow.description || 'No description provided.';
    const smokeId = smokeCheck.id || 'none';
    const docName = 'PROJECT_CLOSURE.md';
    return [
        `Closure contract created for ${projectName}.`,
        ``,
        `- source: ${source}`,
        `- profile: ${profile}`,
        `- mode: ${mode}`,
        `- primary flow: ${flowId} — ${flowDesc}`,
        `- smoke check: ${smokeId}`,
        `- repo doc: ${docName}`,
        ``,
        `You can inspect the live contract on the project card under "Live Contract" or edit it under "Edit Live Contract JSON".`,
    ].join('\n');
}

async function confirmNpTasks() {
    const btn = document.getElementById('npConfirmBtn');
    const previewValidation = validateNpTaskPreview(_npTasksPreview);
    if (previewValidation.errors.length) {
        showNpTaskPreview(_npProjectName, _npTasksPreview);
        return;
    }
    btn.disabled = true;
    btn.textContent = 'Creating...';
    try {
        // Use the dependency graph from the LLM (depends_on indices already resolved to IDs by the server)
        const res = await fetch(`${API}/api/create-project-tasks`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                project_name: _npProjectName,
                project_type: getNpProjectType(),
                tasks: _npTasksPreview,
                overview: _npOverview,
                quality_gates: _npQualityGates,
                design_doc: _npDesignDoc,
            }),
        });
        const data = await res.json();
        if (data.error) {
            const details = Array.isArray(data.details) && data.details.length
                ? `<div style="margin-top:8px;font-size:12px;color:#fdaeb7;text-align:left">
                    ${data.details.map(d => `<div>• ${escapeHtml(String(d))}</div>`).join('')}
                   </div>`
                : '';
            btn.textContent = 'Create failed';
            btn.style.background = '#6e1a1a';
            btn.disabled = false;
            const banner = document.getElementById('npCreatedBanner');
            banner.style.display = 'block';
            banner.style.background = '#2d1517';
            banner.style.borderTop = '1px solid #f85149';
            banner.style.color = '#fdaeb7';
            banner.style.padding = '12px 16px';
            banner.innerHTML = `
                <div style="color:#f85149;font-size:13px;font-weight:600">
                    Project creation failed: ${escapeHtml(data.error)}
                </div>${details}
                ${data.retryable ? `
                    <div style="margin-top:10px;display:flex;justify-content:flex-end">
                        <button onclick="reopenNewProjectChatForRetry(window.__npRetryAssistantMessage || '')" style="background:#1f6feb;color:#fff;border:none;border-radius:6px;padding:6px 14px;font-size:12px;cursor:pointer">
                            Continue in Chat
                        </button>
                    </div>` : ''}`;
            window.__npRetryAssistantMessage = data.chat_recovery_assistant || '';
            if (data.retryable && data.chat_recovery_assistant) {
                reopenNewProjectChatForRetry(data.chat_recovery_assistant);
            }
        } else {
            const gitLine = data.gitea_url
                ? `<div style="margin-top:4px;font-size:11px;color:#8b949e">Repo: <a href="${escapeHtml(data.gitea_url)}" target="_blank" style="color:#58a6ff">${escapeHtml(data.gitea_url)}</a></div>`
                : '';
            const proposal = data.closure_proposal || null;
            const proposalSpec = proposal && proposal.closure_spec ? proposal.closure_spec : {};
            const primaryFlow = (Array.isArray(proposalSpec.critical_flows) && proposalSpec.critical_flows[0]) || {};
            const smokeCheck = (proposalSpec.verification && Array.isArray(proposalSpec.verification.smoke_checks) && proposalSpec.verification.smoke_checks[0]) || {};
            const closureHtml = proposal ? `
                <div style="margin-top:10px;padding:10px 12px;background:#11161d;border:1px solid #30363d;border-radius:8px;text-align:left">
                    <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Closure Contract</div>
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;font-size:12px">
                        <div><span style="color:#8b949e">source</span><div style="color:#e6edf3">${escapeHtml(proposal.source || 'unknown')}</div></div>
                        <div><span style="color:#8b949e">profile</span><div style="color:#e6edf3">${escapeHtml(proposal.profile || 'unknown')}</div></div>
                        <div><span style="color:#8b949e">mode</span><div style="color:#e6edf3">${escapeHtml(proposalSpec.mode || 'build')}</div></div>
                        <div><span style="color:#8b949e">repo doc</span><div style="color:#e6edf3">PROJECT_CLOSURE.md</div></div>
                    </div>
                    <div style="margin-top:8px;font-size:12px;color:#e6edf3">
                        <strong>${escapeHtml(primaryFlow.id || 'main-flow')}</strong>
                        <span style="color:#8b949e">${escapeHtml(primaryFlow.description || 'No description provided.')}</span>
                    </div>
                    <div style="margin-top:4px;font-size:11px;color:#8b949e">
                        smoke check: ${escapeHtml(smokeCheck.id || 'none')}
                    </div>
                    <div style="margin-top:8px;font-size:11px;color:#8b949e">
                        Inspect it on the project card under <strong style="color:#e6edf3">Live Contract</strong> or edit it under <strong style="color:#e6edf3">Edit Live Contract JSON</strong>.
                    </div>
                </div>
            ` : '';
            document.getElementById('npCreatedBanner').innerHTML = `
                <div style="color:#3fb950;font-size:13px">
                    ✓ Created ${data.created} tasks for <strong>${escapeHtml(data.project)}</strong>. Add it to managed projects to start the swarm.
                </div>${gitLine}${closureHtml}`;
            if (proposal) {
                appendNpMessage('assistant', summarizeClosureProposalForNp(proposal, data.project));
            }
            if (getNpProjectType() === 'xogot' && data.project) {
                fetch(`${API}/api/xogot-projects/${encodeURIComponent(data.project)}`, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({enabled: true}),
                }).catch(() => {});
            }
            clearNpDraft();
            loadData();
        }
    } catch(e) {
        btn.textContent = 'Network error';
        btn.disabled = false;
    }
}

function openNewProjectModal() { openNewProjectPanel(); }

function openInstantModal() {
    document.getElementById('instantHint').value = '';
    document.getElementById('instantCount').value = '1';
    document.getElementById('instantType').value = 'godot';
    document.getElementById('instantScope').value = 'medium';
    document.getElementById('instantStatus').textContent = '';
    document.getElementById('instantRunBtn').disabled = false;
    document.getElementById('instantRunBtn').textContent = '⚡ Create';
    document.getElementById('instantModal').style.display = 'flex';
}

function closeInstantModal() {
    document.getElementById('instantModal').style.display = 'none';
}

async function runInstantProject() {
    const btn = document.getElementById('instantRunBtn');
    const status = document.getElementById('instantStatus');
    const count = parseInt(document.getElementById('instantCount').value) || 1;
    const type = document.getElementById('instantType').value;
    const hint = document.getElementById('instantHint').value.trim();
    const scopeMap = { tiny: [5, 12], small: [15, 25], medium: [30, 50], large: [50, 80] };
    const [min_tasks, max_tasks] = scopeMap[document.getElementById('instantScope').value] || scopeMap.medium;

    btn.disabled = true;
    btn.textContent = count === 1 ? '⏳ Working…' : `⏳ Creating ${count}…`;
    status.textContent = 'Starting…';

    try {
        const resp = await fetch('/api/wizard/create-instant', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project_type: type, hint, count, min_tasks, max_tasks }),
        });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            status.textContent = `Error: ${data.error || resp.statusText}`;
            btn.disabled = false;
            btn.textContent = '⚡ Create';
            return;
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            const lines = buf.split('\n\n');
            buf = lines.pop(); // keep incomplete chunk
            for (const chunk of lines) {
                const line = chunk.trim();
                if (!line.startsWith('data:')) continue;
                let evt;
                try { evt = JSON.parse(line.slice(5).trim()); } catch { continue; }

                if (evt.type === 'progress') {
                    status.textContent = evt.message;
                } else if (evt.type === 'done') {
                    status.textContent = `✓ ${evt.project_name} — ${evt.tasks_created} tasks queued`;
                    btn.textContent = evt.index < evt.count ? `⏳ ${evt.index}/${evt.count} done…` : '✓ Done';
                    if (type === 'xogot' && evt.project_name) {
                        fetch(`/api/xogot-projects/${encodeURIComponent(evt.project_name)}`, {
                            method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({enabled: true}),
                        }).catch(() => {});
                    }
                } else if (evt.type === 'error') {
                    const name = evt.project_name ? `${evt.project_name}: ` : '';
                    status.textContent = `✗ ${name}${evt.message}`;
                } else if (evt.type === 'complete') {
                    const names = (evt.results || []).filter(r => r.success).map(r => r.project_name).join(', ');
                    status.textContent = `✓ ${evt.created}/${evt.requested} created${names ? ': ' + names : ''}`;
                    btn.textContent = '✓ Done';
                    setTimeout(() => { loadData(); }, 1500);
                }
            }
        }
    } catch (e) {
        status.textContent = `Network error: ${e.message}`;
        btn.disabled = false;
        btn.textContent = '⚡ Create';
    }
}
