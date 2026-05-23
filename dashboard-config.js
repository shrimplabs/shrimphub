let maxLines = 2000;

async function loadWebhook() {
    try {
        const res = await fetch(API + '/api/webhook');
        if (res.ok) {
            const data = await res.json();
            document.getElementById('webhookUrl').value = data.url || '';
        }
    } catch(e) {}
}

async function saveWebhook() {
    const url = document.getElementById('webhookUrl').value.trim();
    try {
        await fetch(API + '/api/webhook', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url}),
        });
    } catch(e) {}
}

async function syncVisionProviders() {
    try {
        const res = await fetch(API + '/api/vision-providers');
        if (res.ok) {
            const data = await res.json();
            document.getElementById('visionProvider').value = data.vision_provider || '';
            document.getElementById('visionProviderFast').value = data.vision_provider_fast || '';
            const providers = data.vision_providers || {};
            document.getElementById('visionProvidersJson').value =
                Object.keys(providers).length ? JSON.stringify(providers, null, 2) : '';
        }
    } catch(e) {}
}

async function saveVisionProviders() {
    const statusEl = document.getElementById('visionSaveStatus');
    const visionProvider = document.getElementById('visionProvider').value.trim();
    const visionProviderFast = document.getElementById('visionProviderFast').value.trim();
    const jsonText = document.getElementById('visionProvidersJson').value.trim();
    let visionProviders = {};
    if (jsonText) {
        try { visionProviders = JSON.parse(jsonText); }
        catch(e) { statusEl.textContent = '✗ Invalid JSON'; statusEl.style.color = '#f85149'; return; }
    }
    try {
        const res = await fetch(API + '/api/vision-providers', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                vision_provider: visionProvider || 'claude',
                vision_provider_fast: visionProviderFast,
                vision_providers: visionProviders,
            }),
        });
        if (res.ok) {
            statusEl.textContent = '✓ Saved';
            statusEl.style.color = '#3fb950';
            setTimeout(() => { statusEl.textContent = ''; }, 2000);
        } else {
            statusEl.textContent = '✗ Error';
            statusEl.style.color = '#f85149';
        }
    } catch(e) {
        statusEl.textContent = '✗ ' + e.message;
        statusEl.style.color = '#f85149';
    }
}

async function syncMaxAgents() {
    try {
        const res = await fetch(API + '/api/max-agents');
        const data = await res.json();
        document.getElementById('maxAgents').value = data.max_active_agents;
        if (data.max_lines) maxLines = data.max_lines;
    } catch(e) {}
}

async function syncQaMaxCycles() {
    try {
        const res = await fetch(API + '/api/qa-max-cycles');
        const data = await res.json();
        document.getElementById('qaMaxCycles').value = data.qa_max_cycles;
    } catch(e) {}
}

async function setQaMaxCycles() {
    const value = parseInt(document.getElementById('qaMaxCycles').value);
    if (!value || value < 1) return;
    const status = document.getElementById('spawnStatus');
    try {
        const res = await fetch(API + '/api/qa-max-cycles', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({qa_max_cycles: value})
        });
        const data = await res.json();
        status.textContent = `QA cycles set to ${data.qa_max_cycles}`;
        setTimeout(() => { status.textContent = ''; }, 2000);
    } catch(e) {
        status.textContent = 'Error: ' + e.message;
    }
}

async function setMaxAgents() {
    const value = parseInt(document.getElementById('maxAgents').value);
    if (!value || value < 1) return;
    const status = document.getElementById('spawnStatus');
    try {
        const res = await fetch(API + '/api/max-agents', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({max_active_agents: value})
        });
        const data = await res.json();
        status.textContent = `Cap set to ${data.max_active_agents}`;
        setTimeout(() => { status.textContent = ''; }, 2000);
    } catch(e) {
        status.textContent = 'Error: ' + e.message;
        status.style.color = '#f85149';
        setTimeout(() => { status.textContent = ''; status.style.color = ''; }, 3000);
    }
}

let _depsVisible = true;
let _viz = null;
let _taskDataMap = {};
let _agentByTaskId = {};
let _depHistoryLimit = 2;

async function toggleDeps() {
    const section = document.getElementById('depsSection');
    const toggle = document.getElementById('depsToggle');
    _depsVisible = !_depsVisible;
    section.style.display = _depsVisible ? 'block' : 'none';
    toggle.textContent = _depsVisible ? '▲ hide' : '▼ show';
    if (_depsVisible) await renderDepsGraph();
}

// Dependency graph state lives in dashboard_deps_integrity.js.

function setDepHistoryLimit(value) {
    if (!window.SwarmDepsIntegrityUI) return;
    window.SwarmDepsIntegrityUI.setDepHistoryLimit(value);
    _depHistoryLimit = window.SwarmDepsIntegrityUI.getDepHistoryLimit();
    if (_depsVisible) renderDepsGraph();
}

function resetDepGraphView() {
    if (window.SwarmDepsIntegrityUI) return window.SwarmDepsIntegrityUI.resetDepGraphView();
}

function zoomDepGraph(factor, clientX = null, clientY = null) {
    if (window.SwarmDepsIntegrityUI) return window.SwarmDepsIntegrityUI.zoomDepGraph(factor, clientX, clientY);
}

function fitDepGraphView(boundsOverride = null) {
    if (window.SwarmDepsIntegrityUI) return window.SwarmDepsIntegrityUI.fitDepGraphView(boundsOverride);
}

function focusActiveDepGraphView() {
    if (window.SwarmDepsIntegrityUI) return window.SwarmDepsIntegrityUI.focusActiveDepGraphView();
}

window.addEventListener('keydown', (event) => {
    if (window.SwarmDepsIntegrityUI && _depsVisible) {
        const el = document.activeElement;
        const tag = (el?.tagName || '').toLowerCase();
        const shortcutEnabled = !el || (tag !== 'input' && tag !== 'textarea' && !el.isContentEditable);
        if (!shortcutEnabled) return;
        if (event.key === '+' || event.key === '=') {
            event.preventDefault();
            zoomDepGraph(1.15);
        } else if (event.key === '-' || event.key === '_') {
            event.preventDefault();
            zoomDepGraph(1 / 1.15);
        } else if (event.key === '0') {
            event.preventDefault();
            fitDepGraphView();
        } else if (event.key === 'r' || event.key === 'R') {
            event.preventDefault();
            resetDepGraphView();
        } else if (event.key === 'f' || event.key === 'F') {
            event.preventDefault();
            focusActiveDepGraphView();
        }
    }
});

async function renderDepsGraph() {
    if (!window.SwarmDepsIntegrityUI) return;
    _depHistoryLimit = window.SwarmDepsIntegrityUI.getDepHistoryLimit();
    return window.SwarmDepsIntegrityUI.renderDepsGraph();
}


function openNodeDetail(taskId) {
    const task = _taskDataMap[taskId];
    if (!task) return;
    if (task.status === 'in_progress') {
        const agent = _agentByTaskId[taskId];
        if (agent) {
            showAgentOutput(agent.id, task.project, true, taskId);
            return;
        }
    }
    // pending / failed / completed — open the edit/detail modal
    openEditTaskModal(task);
}

function toggleProjects() {
    const section = document.getElementById('projectsSection');
    const toggle = document.getElementById('projectsToggle');
    const visible = section.style.display !== 'none';
    section.style.display = visible ? 'none' : 'block';
    toggle.textContent = visible ? '▼ show' : '▲ hide';
}

function toggleHistory() {
    const section = document.getElementById('historySection');
    const toggle = document.getElementById('historyToggle');
    const visible = section.style.display !== 'none';
    section.style.display = visible ? 'none' : 'block';
    toggle.textContent = visible ? '▼ show' : '▲ hide';
}

// ── Sidebar ─────────────────────────────────────────────────────────────────
function renderSidebar(projectNames, projectTaskCounts) {
    const container = document.getElementById('sidebar-projects');
    const allCount = Object.values(projectTaskCounts).reduce((s, v) => s + v, 0);
    const items = [
        { name: 'All Projects', key: null, count: allCount },
        ...projectNames
            .map(n => ({ name: n, key: n, count: projectTaskCounts[n] || 0 })),
    ];
    container.innerHTML = items.map(item => {
        const active = _selectedProject === item.key;
        const dataAttr = item.key !== null ? `data-project="${escapeHtml(item.name)}"` : '';
        const hasAgent = item.key !== null && _activeProjectSet.has(item.key);
        return `
            <div class="sidebar-item ${active ? 'active' : ''}" ${dataAttr}
                 onclick="selectSidebarProject(${item.key === null ? 'null' : "'" + item.name + "'"})">
                ${hasAgent ? '<span class="active-led"></span>' : ''}
                <span class="sidebar-item-name">${escapeHtml(item.name)}</span>
                <span class="sidebar-item-count">${item.count}</span>
            </div>`;
    }).join('');
}

function selectSidebarProject(name) {
    _selectedProject = name;
    renderSidebar(_allProjectNames, _sidebarTaskCounts);
    applyProjectFilter();
    if (_depsVisible) renderDepsGraph();

    const banner = document.getElementById('project-focus-banner');
    const focusName = document.getElementById('project-focus-name');
    const focusBlurb = document.getElementById('project-focus-blurb');
    const descBar = document.getElementById('project-description-bar');
    const descName = document.getElementById('project-desc-name');
    const descBlurb = document.getElementById('project-desc-blurb');
    if (!name) {
        banner.style.display = 'none';
        descBar.style.display = 'none';
        return;
    }
    focusName.textContent = name;
    focusBlurb.textContent = '';
    banner.style.display = '';
    descName.textContent = name;
    descBlurb.textContent = '';
    descBar.style.display = '';
    fetch(`${API}/api/projects/${encodeURIComponent(name)}/notes`).then(r => r.json()).then(data => {
        let blurb = '';
        if (data.notes) {
            const lines = data.notes.split('\n').map(l => l.trim()).filter(l => l && !l.startsWith('#') && !l.startsWith('-'));
            blurb = lines[0] || '';
        }
        if (!blurb && data.concept) blurb = data.concept;
        focusBlurb.textContent = blurb;
        descBlurb.textContent = blurb;
    }).catch(() => {});
}

function applyProjectFilter() {
    const sel = _selectedProject;
    document.querySelectorAll('[data-project]').forEach(el => {
        if (el.closest('#sidebar')) return;  // never hide sidebar nav items
        el.style.display = (!sel || el.dataset.project === sel) ? '' : 'none';
    });
    document.querySelectorAll('[data-has-project]').forEach(el => {
        if (el.closest('#sidebar')) return;
        el.style.display = (!sel || el.dataset.hasProject === sel) ? '' : 'none';
    });
}

let _sidebarTaskCounts = {};

function createHistoryCard(agent) {
    const status = agent.status || 'unknown';
    const time = agent.completed_at
        ? new Date(agent.completed_at).toLocaleString()
        : (agent.spawned_at ? new Date(agent.spawned_at).toLocaleString() : '-');
    const loops = (agent.output || '').match(/loop (\d+)\/\d+/g);
    const lastLoop = loops ? loops[loops.length - 1] : null;
    const exitBadge = agent.exit_code === 0
        ? '<span style="color:#3fb950">✓ exit 0</span>'
        : `<span style="color:#f85149">✗ exit ${agent.exit_code ?? '?'}</span>`;
    const snippet = agent.output
        ? escapeHtml(agent.output.split('\n').filter(l => l.trim()).slice(-4).join('\n'))
        : 'No output captured';
    const meta = agent.metadata || {};
    const diffStat = meta.diff_stat ? meta.diff_stat.split('\n').pop() : '';
    return `
        <div class="card agent-card" onclick="showAgentOutput('${agent.id}', '${agent.project}', false, '${agent.task_id || ''}')">
            <div class="card-header">
                <span class="project-name">${agent.project}</span>
                <span class="status ${status}">${status}</span>
            </div>
            <div class="agent-details">
                <div class="stat">Type: <span>${agent.task_type || 'refactor'}</span></div>
                <div class="stat">Finished: <span>${time}</span></div>
                <div class="stat">${exitBadge}${lastLoop ? ' · ' + lastLoop : ''}</div>
            </div>
            ${diffStat ? `<div class="diff-stat">± ${escapeHtml(diffStat)}</div>` : ''}
            <div class="output-log" style="max-height:70px;font-size:10px;overflow:hidden;margin-top:8px">${snippet}</div>
            ${agent.exit_code !== 0 ? `
            <div style="margin-top:8px;text-align:right">
                <button onclick="event.stopPropagation();openRequeueModal('${agent.id}','${escapeHtml((agent.task_id||'').replace(/'/g,"\\'"))}','${escapeHtml(agent.project)}')" style="font-size:11px;padding:2px 8px;background:transparent;color:#e3b341;border:1px solid #e3b341;border-radius:4px;cursor:pointer" title="Re-queue this task">↺ Re-queue</button>
            </div>` : ''}
        </div>`;
}

// ---- Provider management ----
let _allProviders = {};

async function syncProviders() {
    try {
        const res = await fetch(API + '/api/providers');
        if (!res.ok) return;
        const data = await res.json();
        _allProviders = data.providers || {};
        const current = data.current || 'minimax';
        const sel = document.getElementById('providerSelect');
        sel.innerHTML = Object.keys(_allProviders).map(name =>
            `<option value="${name}" ${name === current ? 'selected' : ''}>${name}</option>`
        ).join('');
        updateProviderUI(current);
    } catch(e) {}
}

function updateProviderUI(name) {
    const cfg = _allProviders[name] || {};
    document.getElementById('providerModel').placeholder = cfg.model || '';
    const keyEl = document.getElementById('providerKeyStatus');
    if (cfg.api_key_set) {
        keyEl.className = 'provider-key-status provider-key-ok';
        keyEl.textContent = `✓ ${cfg.api_key_env} set`;
    } else {
        keyEl.className = 'provider-key-status provider-key-missing';
        keyEl.textContent = `✗ ${cfg.api_key_env} not set`;
    }
}

function onProviderSelectChange() {
    const name = document.getElementById('providerSelect').value;
    document.getElementById('providerModel').value = '';
    updateProviderUI(name);
}

async function saveProvider() {
    const provider = document.getElementById('providerSelect').value;
    const model = document.getElementById('providerModel').value.trim();
    const body = {provider};
    if (model) body.model = model;
    try {
        const res = await fetch(API + '/api/provider', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.error) {
            document.getElementById('providerKeyStatus').textContent = '✗ ' + data.error;
            return;
        }
        await syncProviders();
    } catch(e) {
        document.getElementById('providerKeyStatus').textContent = 'Error: ' + e.message;
    }
}

// ---- Thinking Toggle ----
async function loadThinking() {
    try {
        const data = await fetch(API + '/api/thinking').then(r => r.json());
        const btn = document.getElementById('thinkingToggle');
        btn.textContent = data.enabled ? `On (${data.budget_tokens})` : 'Off';
        btn.style.color = data.enabled ? '#3fb950' : '#8b949e';
        btn.style.borderColor = data.enabled ? '#3fb950' : '#30363d';
    } catch(e) {}
}
async function toggleThinking() {
    try {
        const current = await fetch(API + '/api/thinking').then(r => r.json());
        const newEnabled = !current.enabled;
        await fetch(API + '/api/thinking', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: newEnabled, budget_tokens: 10000}),
        });
        await loadThinking();
        showToast(`Thinking ${newEnabled ? 'enabled' : 'disabled'}`, newEnabled ? '#3fb950' : '#8b949e');
    } catch(e) { showToast('Error toggling thinking', '#f85149'); }
}

// ---- Project Wizard ----
let _wzTasks = [];  // current task list in step 2

function openWizard() {
    document.getElementById('wzProjectName').value = '';
    document.getElementById('wzProjectType').value = 'godot';
    document.getElementById('wzDescription').value = '';
    document.getElementById('wzNotes').value = '';
    document.getElementById('wzMinTasks').value = '10';
    document.getElementById('wzMaxTasks').value = '80';
    document.getElementById('wzScope').value = 'medium';
    document.getElementById('wizardStep1Error').style.display = 'none';
    document.getElementById('wizardStep1').style.display = 'block';
    document.getElementById('wizardStep2').style.display = 'none';
    document.getElementById('wizardModal').classList.add('active');
    document.getElementById('wzProjectName').focus();
}

function closeWizard() {
    document.getElementById('wizardModal').classList.remove('active');
}

function wizardBack() {
    document.getElementById('wizardStep2').style.display = 'none';
    document.getElementById('wizardStep1').style.display = 'block';
    document.getElementById('wizardTitle').textContent = '🧙 New Project — Wizard';
}

function wzApplyScope(val) {
    const presets = {tiny:[5,15], small:[15,30], medium:[30,80], large:[60,120], epic:[100,200]};
    if (presets[val]) {
        document.getElementById('wzMinTasks').value = presets[val][0];
        document.getElementById('wzMaxTasks').value = presets[val][1];
    }
}

async function wizardGeneratePlan() {
    const name = document.getElementById('wzProjectName').value.trim();
    const type = document.getElementById('wzProjectType').value;
    const desc = document.getElementById('wzDescription').value.trim();
    const minTasks = parseInt(document.getElementById('wzMinTasks').value) || 10;
    const maxTasks = parseInt(document.getElementById('wzMaxTasks').value) || 80;
    const errEl = document.getElementById('wizardStep1Error');
    if (!name) { errEl.textContent = 'Project name is required.'; errEl.style.display = 'block'; return; }
    if (!desc) { errEl.textContent = 'Description is required.'; errEl.style.display = 'block'; return; }
    errEl.style.display = 'none';

    const btn = document.getElementById('wzGenerateBtn');
    btn.disabled = true; btn.textContent = '✨ Thinking…';

    document.getElementById('wizardStep1').style.display = 'none';
    document.getElementById('wizardStep2').style.display = 'block';
    document.getElementById('wizardTitle').textContent = `Plan: ${name}`;
    document.getElementById('wzLoadingMsg').style.display = 'block';
    document.getElementById('wzTaskList').style.display = 'none';
    document.getElementById('wzAddTaskRow').style.display = 'none';
    document.getElementById('wzCreateBtn').style.display = 'none';
    document.getElementById('wizardStep2Error').style.display = 'none';

    try {
        const res = await fetch(API + '/api/wizard/plan', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({project_name: name, project_type: type, description: desc, min_tasks: minTasks, max_tasks: maxTasks}),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error + (data.raw ? '\n\nRaw: ' + data.raw : ''));
        _wzTasks = data.tasks || [];
        renderWzTasks();
        document.getElementById('wzLoadingMsg').style.display = 'none';
        document.getElementById('wzTaskList').style.display = 'flex';
        document.getElementById('wzAddTaskRow').style.display = 'block';
        document.getElementById('wzCreateBtn').style.display = 'inline-block';
    } catch(e) {
        document.getElementById('wzLoadingMsg').style.display = 'none';
        const e2 = document.getElementById('wizardStep2Error');
        e2.textContent = 'Error: ' + e.message; e2.style.display = 'block';
    }
    btn.disabled = false; btn.textContent = '✨ Generate Plan →';
}

function renderWzTasks() {
    const el = document.getElementById('wzTaskList');
    el.innerHTML = _wzTasks.map((t, i) => `
        <div id="wzTask-${i}" style="background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:10px 12px;display:flex;flex-direction:column;gap:6px">
            <div style="display:flex;gap:8px;align-items:center">
                <span style="font-size:11px;color:#8b949e;min-width:20px">#${i+1}</span>
                <select onchange="_wzTasks[${i}].type=this.value" style="background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:12px">
                    ${['feature','bug','refactor','polish'].map(tp =>
                        `<option value="${tp}" ${t.type===tp?'selected':''}>${tp}</option>`
                    ).join('')}
                </select>
                <input type="number" min="1" max="100" value="${t.priority||50}"
                    onchange="_wzTasks[${i}].priority=parseInt(this.value)"
                    style="width:52px;background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:12px"
                    title="Priority">
                <button onclick="wzRemoveTask(${i})" title="Remove task"
                    style="margin-left:auto;background:transparent;color:#6e7681;border:none;font-size:16px;cursor:pointer;line-height:1">×</button>
            </div>
            <textarea rows="2" onchange="_wzTasks[${i}].description=this.value"
                style="width:100%;background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:5px 7px;font-size:12px;resize:vertical;box-sizing:border-box">${escapeHtml(t.description||'')}</textarea>
            <div style="font-size:11px;color:#6e7681">
                Deps: ${(t.depends_on||[]).length ? (t.depends_on||[]).map(d=>'#'+(d+1)).join(', ') : 'none'}
                <span style="margin-left:8px;cursor:pointer;color:#58a6ff" onclick="wzEditDeps(${i})" title="Edit dependencies">[edit]</span>
            </div>
        </div>
    `).join('');
}

function wzRemoveTask(i) {
    _wzTasks.splice(i, 1);
    renderWzTasks();
}

function wzAddTask() {
    _wzTasks.push({type:'feature', priority:50, description:'', depends_on:[]});
    renderWzTasks();
    // focus last textarea
    const items = document.getElementById('wzTaskList').querySelectorAll('textarea');
    if (items.length) items[items.length-1].focus();
}

function wzEditDeps(i) {
    const current = (_wzTasks[i].depends_on||[]).map(d=>d+1).join(', ');
    const input = prompt(`Task #${i+1} depends on (comma-separated task numbers, e.g. "1, 2"):`, current);
    if (input === null) return;
    _wzTasks[i].depends_on = input.split(',')
        .map(s => parseInt(s.trim()) - 1)
        .filter(n => !isNaN(n) && n >= 0 && n < _wzTasks.length && n !== i);
    renderWzTasks();
}

async function wizardCreate() {
    // Sync textarea values (onchange may not fire if user tabs away)
    document.getElementById('wzTaskList').querySelectorAll('textarea').forEach((ta, i) => {
        if (_wzTasks[i]) _wzTasks[i].description = ta.value.trim();
    });

    const name  = document.getElementById('wzProjectName').value.trim();
    const type  = document.getElementById('wzProjectType').value;
    const notes = document.getElementById('wzNotes').value.trim();
    const errEl = document.getElementById('wizardStep2Error');
    const btn   = document.getElementById('wzCreateBtn');

    if (_wzTasks.length === 0) { errEl.textContent = 'Add at least one task.'; errEl.style.display = 'block'; return; }

    btn.disabled = true; btn.textContent = 'Creating…';
    errEl.style.display = 'none';

    try {
        const res = await fetch(API + '/api/wizard/create', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({project_name: name, project_type: type, notes, tasks: _wzTasks}),
        });
        const data = await res.json();
        if (data.error) {
            const details = Array.isArray(data.details) && data.details.length
                ? '\n- ' + data.details.join('\n- ')
                : '';
            throw new Error(data.error + details);
        }
        closeWizard();
        loadData();
    } catch(e) {
        errEl.textContent = 'Error: ' + e.message; errEl.style.display = 'block';
    }
    btn.disabled = false; btn.textContent = '✓ Create Project & Tasks';
}

// ---- Strategy selector ----
async function syncStrategy() {
    try {
        const res = await fetch(API + '/api/strategy');
        if (!res.ok) return;
        const data = await res.json();
        const sel = document.getElementById('strategySelect');
        if (sel && data.strategy) sel.value = data.strategy;
    } catch(e) {}
}

async function saveStrategy() {
    const strategy = document.getElementById('strategySelect').value;
    try {
        await fetch(API + '/api/strategy', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({strategy}),
        });
    } catch(e) {}
}

// ---- Project pause/resume ----
async function loadPausedProjects() {
    try {
        const res = await fetch(API + '/api/managed-projects');
        if (!res.ok) return;
        const data = await res.json();
        _pausedProjects = new Set(data.paused_projects || []);
    } catch(e) {}
}

async function toggleProjectPause(event, name) {
    event.stopPropagation();
    const wasPaused = _pausedProjects.has(name);
    if (wasPaused) {
        _pausedProjects.delete(name);
    } else {
        _pausedProjects.add(name);
    }
    try {
        await fetch(API + '/api/managed-projects', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({paused_projects: [..._pausedProjects]}),
        });
        loadData();
    } catch(e) {
        // rollback
        if (wasPaused) _pausedProjects.add(name); else _pausedProjects.delete(name);
    }
}

// ---- Webhook test ----
async function testWebhook() {
    const url = document.getElementById('webhookUrl').value.trim();
    const statusEl = document.getElementById('webhookTestStatus');
    if (!url) { statusEl.textContent = 'Enter a URL first'; statusEl.style.color = '#f85149'; return; }
    statusEl.textContent = 'Sending…'; statusEl.style.color = '#8b949e';
    try {
        await fetch(API + '/api/webhook', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url})});
        const res = await fetch(API + '/api/webhook/test', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url})});
        const data = await res.json();
        if (data.ok) {
            statusEl.textContent = '✓ Sent'; statusEl.style.color = '#3fb950';
        } else {
            statusEl.textContent = '✗ ' + (data.error || 'failed'); statusEl.style.color = '#f85149';
        }
    } catch(e) {
        statusEl.textContent = '✗ ' + e.message; statusEl.style.color = '#f85149';
    }
    setTimeout(() => { statusEl.textContent = ''; }, 4000);
}

async function loadMetrics() {
    try {
        const m = await fetch('/api/metrics').then(r => r.json());
        const fmt = (v, pct) => pct ? (v * 100).toFixed(1) + '%' : (typeof v === 'number' ? v.toLocaleString() : v);
        const items = [
            ['Completed', m.tasks_completed, false, 'success'],
            ['Failed', m.tasks_failed, false, 'danger'],
            ['1st-attempt success', m.first_attempt_success_rate, true, null],
            ['Validation bug rate', m.validation_bug_rate, true, 'warning'],
            ['Avg attempts', m.avg_attempts_per_task, false, 'info'],
            ['Avg input tokens', m.avg_input_tokens, false, null],
            ['Avg output tokens', m.avg_output_tokens, false, null],
            ['Avg loops', m.avg_loops_per_agent, false, 'info'],
            ['web_search calls', m.web_search_calls, false, null],
            ['Knowledge files', m.knowledge_files_written, false, 'success'],
        ];
        document.getElementById('metrics-grid').innerHTML = items.map(([label, val, pct, colorClass]) =>
            `<div class="metric-cell">
                <div class="metric-label">${label}</div>
                <div class="metric-value${colorClass ? ' ' + colorClass : ''}">${fmt(val, pct)}</div>
            </div>`
        ).join('');
    } catch (e) {}
}
