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
        const panStep = event.shiftKey ? 200 : 60;
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
        } else if (event.key === 'ArrowLeft') {
            event.preventDefault();
            window.SwarmDepsIntegrityUI.panDepGraph(panStep, 0);
        } else if (event.key === 'ArrowRight') {
            event.preventDefault();
            window.SwarmDepsIntegrityUI.panDepGraph(-panStep, 0);
        } else if (event.key === 'ArrowUp') {
            event.preventDefault();
            window.SwarmDepsIntegrityUI.panDepGraph(0, panStep);
        } else if (event.key === 'ArrowDown') {
            event.preventDefault();
            window.SwarmDepsIntegrityUI.panDepGraph(0, -panStep);
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
    // pending / failed / completed -- open the edit/detail modal
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
let _sidebarIdleCollapsed = localStorage.getItem('swarm.sidebarIdleCollapsed') !== 'false';

function renderSidebar(projectNames, projectTaskCounts) {
    const container = document.getElementById('sidebar-projects');
    const allCount = Object.values(projectTaskCounts).reduce((s, v) => s + v, 0);

    // Partition into three tiers
    const live = [], active = [], idle = [];
    for (const name of projectNames) {
        if (_activeProjectSet && _activeProjectSet.has(name)) live.push(name);
        else if ((projectTaskCounts[name] || 0) > 0) active.push(name);
        else idle.push(name);
    }

    function sidebarItem(name, count) {
        const isSelected = _selectedProject === name;
        const hasAgent = _activeProjectSet && _activeProjectSet.has(name);
        return `<div class="sidebar-item ${isSelected ? 'active' : ''}" data-project="${escapeHtml(name)}"
                     onclick="selectSidebarProject('${escapeHtml(name).replace(/'/g, "\\'")}')">
            ${hasAgent ? '<span class="active-led"></span>' : ''}
            <span class="sidebar-item-name">${escapeHtml(name)}</span>
            ${count > 0 ? `<span class="sidebar-item-count">${count}</span>` : ''}
        </div>`;
    }

    function sectionLabel(text, extra = '') {
        return `<div class="sidebar-section-label">${text}${extra}</div>`;
    }

    // Recently completed: projects with agent completions in last 24h, not currently live
    const liveSet = new Set(live);
    const recentDone = (_recentlyCompleted || []).filter(r => !liveSet.has(r.name));

    function timeAgo(ms) {
        const diff = Date.now() - ms;
        if (diff < 60000) return 'just now';
        if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
        return Math.floor(diff / 3600000) + 'h ago';
    }

    function recentItem(r) {
        const isSelected = _selectedProject === r.name;
        const count = projectTaskCounts[r.name] || 0;
        return `<div class="sidebar-item ${isSelected ? 'active' : ''}" data-project="${escapeHtml(r.name)}"
                     onclick="selectSidebarProject('${escapeHtml(r.name).replace(/'/g, "\\'")}')">
            <span class="sidebar-item-name">${escapeHtml(r.name)}</span>
            <span style="font-size:10px;color:var(--text-faint);margin-left:auto;white-space:nowrap">${timeAgo(r.ms)}</span>
            ${count > 0 ? `<span class="sidebar-item-count" style="margin-left:4px">${count}</span>` : ''}
        </div>`;
    }

    let html = `<div class="sidebar-item ${_selectedProject === null ? 'active' : ''}"
                     onclick="selectSidebarProject(null)">
        <span class="sidebar-item-name">All Projects</span>
        ${allCount > 0 ? `<span class="sidebar-item-count">${allCount}</span>` : ''}
    </div>`;

    if (recentDone.length) {
        html += sectionLabel('✓ Recently Done');
        html += recentDone.map(r => recentItem(r)).join('');
    }
    if (live.length) {
        html += sectionLabel('● Live');
        html += live.map(n => sidebarItem(n, projectTaskCounts[n] || 0)).join('');
    }
    if (active.length) {
        html += sectionLabel('Active');
        html += active.map(n => sidebarItem(n, projectTaskCounts[n] || 0)).join('');
    }
    if (idle.length) {
        const toggleIcon = _sidebarIdleCollapsed ? '▸' : '▾';
        html += `<div class="sidebar-section-label sidebar-section-toggle"
                      onclick="event.stopPropagation();_sidebarIdleCollapsed=!_sidebarIdleCollapsed;localStorage.setItem('swarm.sidebarIdleCollapsed',_sidebarIdleCollapsed);renderSidebar(_allProjectNames,_sidebarTaskCounts)">
            ${toggleIcon} Idle <span style="font-size:10px;color:var(--text-faint);margin-left:4px">${idle.length}</span>
        </div>`;
        if (!_sidebarIdleCollapsed) {
            html += idle.map(n => sidebarItem(n, 0)).join('');
        }
    }

    container.innerHTML = html;
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
        <div class="card agent-card status-${status}" onclick="showAgentOutput('${agent.id}', '${agent.project}', false, '${agent.task_id || ''}')">
            <div class="card-header">
                <span class="project-name">${agent.project}</span>
                <span class="status ${status}">${status}</span>
            </div>
            <div class="agent-details">
                <div class="stat">Type: <span>${agent.task_type || 'refactor'}</span></div>
                <div class="stat">Finished: <span>${time}</span></div>
                <div class="stat">${exitBadge}${lastLoop ? ' * ' + lastLoop : ''}</div>
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
async function syncLocalFallback() {
    try {
        const data = await fetch(API + '/api/config').then(r => r.json());
        const enabled = data.local_fallback_on_quota || false;
        const btn = document.getElementById('localFallbackBtn');
        if (!btn) return;
        btn.classList.remove('is-on', 'is-off');
        btn.classList.add(enabled ? 'is-on' : 'is-off');
        btn.textContent = enabled ? '⚡ On' : 'Off';
    } catch(e) {}
}

async function toggleLocalFallback() {
    try {
        const data = await fetch(API + '/api/config').then(r => r.json());
        const newVal = !(data.local_fallback_on_quota || false);
        await fetch(API + '/api/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({local_fallback_on_quota: newVal}),
        });
        await syncLocalFallback();
        showToast(`Local fallback ${newVal ? 'enabled' : 'disabled'}`, newVal ? '#3fb950' : '#8b949e');
    } catch(e) { showToast('Error toggling local fallback', '#f85149'); }
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

// ---- Human Review Flag Toggle ----
async function loadHumanReviewFlag() {
    try {
        const data = await fetch(API + '/api/human-review-flag').then(r => r.json());
        const btn = document.getElementById('humanReviewFlagToggle');
        if (!btn) return;
        btn.textContent = data.enabled ? 'On' : 'Off';
        btn.style.color = data.enabled ? '#3fb950' : '#8b949e';
        btn.style.borderColor = data.enabled ? '#3fb950' : '#30363d';
    } catch(e) {}
}
async function toggleHumanReviewFlag() {
    try {
        const current = await fetch(API + '/api/human-review-flag').then(r => r.json());
        const newEnabled = !current.enabled;
        await fetch(API + '/api/human-review-flag', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: newEnabled}),
        });
        await loadHumanReviewFlag();
        showToast(`Human review flag ${newEnabled ? 'enabled' : 'disabled'}`, newEnabled ? '#3fb950' : '#8b949e');
    } catch(e) { showToast('Error toggling human review flag', '#f85149'); }
}

// ---- Remove Project ----
async function removeProject(event, name) {
    event.stopPropagation();
    if (!confirm(`Remove project "${name}" from the swarm?\n\nThis will delete all its tasks from the database. The git repo on disk is NOT deleted.\n\nThis cannot be undone.`)) return;
    const btn = event.target;
    btn.disabled = true; btn.textContent = '…';
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(name)}`, {method: 'DELETE'});
        const data = await res.json();
        if (data.error) {
            showToast(data.error, '#f85149');
            btn.disabled = false; btn.textContent = '✕ Remove';
            return;
        }
        showToast(`"${name}" removed (${data.tasks_deleted} tasks deleted)`, '#8b949e');
        loadData();
    } catch(e) { showToast('Remove failed', '#f85149'); btn.disabled = false; btn.textContent = '✕ Remove'; }
}

// ---- Snapshot Modal ----
let _snapshotProject = '';

async function openSnapshotModal(event, name) {
    event.stopPropagation();
    _snapshotProject = name;
    document.getElementById('snapshotModalProject').textContent = name;
    document.getElementById('snapshotTagInput').value = '';
    document.getElementById('cloneNewName').value = '';
    document.getElementById('snapshotModal').style.display = 'flex';
    await _refreshSnapshotList();
}

function closeSnapshotModal() {
    document.getElementById('snapshotModal').style.display = 'none';
}

async function _refreshSnapshotList() {
    const listEl = document.getElementById('snapshotList');
    const cloneSel = document.getElementById('cloneSnapshotTag');
    // Only show loading state if fetch takes more than 150ms
    const loadingTimer = setTimeout(() => {
        listEl.innerHTML = '<span style="color:#8b949e;font-size:12px">Loading…</span>';
    }, 150);
    try {
        const data = await fetch(`${API}/api/projects/${encodeURIComponent(_snapshotProject)}/snapshots`).then(r => r.json());
        clearTimeout(loadingTimer);
        const snaps = data.snapshots || [];
        cloneSel.innerHTML = '<option value="">— pick snapshot —</option>' +
            snaps.map(s => `<option value="${escapeHtml(s.tag)}">${escapeHtml(s.tag)} (${new Date(s.created_at).toLocaleString()})</option>`).join('');
        if (!snaps.length) {
            listEl.innerHTML = '<span style="color:#8b949e;font-size:12px">No snapshots yet.</span>';
            return;
        }
        listEl.innerHTML = snaps.map(s => `
            <div style="display:flex;align-items:center;gap:8px;background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:8px 10px">
                <div style="flex:1">
                    <span style="color:#e6edf3;font-size:13px;font-weight:600">${escapeHtml(s.tag)}</span>
                    <span style="color:#8b949e;font-size:11px;margin-left:8px">${new Date(s.created_at).toLocaleString()}</span>
                </div>
                <button onclick="restoreSnapshot('${escapeHtml(s.tag)}')"
                    style="background:transparent;color:#f0883e;border:1px solid #f0883e;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer">Restore</button>
                <button onclick="deleteSnapshotEntry('${escapeHtml(s.tag)}')"
                    style="background:transparent;color:#f85149;border:1px solid #f85149;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer">✕</button>
            </div>`).join('');
    } catch(e) {
        clearTimeout(loadingTimer);
        listEl.innerHTML = '<span style="color:#f85149;font-size:12px">Failed to load snapshots.</span>';
    }
}

async function saveSnapshot() {
    const tag = document.getElementById('snapshotTagInput').value.trim();
    if (!tag) { showToast('Enter a tag name', '#f85149'); return; }
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(_snapshotProject)}/snapshot`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({tag}),
        });
        const data = await res.json();
        if (data.error) { showToast(data.error, '#f85149'); return; }
        showToast(`Snapshot '${tag}' saved (${data.tasks} tasks)`, '#3fb950');
        document.getElementById('snapshotTagInput').value = '';
        await _refreshSnapshotList();
    } catch(e) { showToast('Save failed', '#f85149'); }
}

async function restoreSnapshot(tag) {
    if (!confirm(`Restore snapshot '${tag}' to ${_snapshotProject}?\n\nThis will delete all current tasks and reset the git repo. There is no undo.`)) return;
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(_snapshotProject)}/restore`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({tag}),
        });
        const data = await res.json();
        if (data.error) { showToast(data.error, '#f85149'); return; }
        showToast(`Restored '${tag}' — ${data.tasks_restored} tasks reset to pending`, '#3fb950');
        closeSnapshotModal();
        loadData();
    } catch(e) { showToast('Restore failed', '#f85149'); }
}

async function deleteSnapshotEntry(tag) {
    if (!confirm(`Delete snapshot '${tag}'?`)) return;
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(_snapshotProject)}/snapshots/${encodeURIComponent(tag)}`, {method: 'DELETE'});
        const data = await res.json();
        if (data.error) { showToast(data.error, '#f85149'); return; }
        showToast(`Snapshot '${tag}' deleted`, '#8b949e');
        await _refreshSnapshotList();
    } catch(e) { showToast('Delete failed', '#f85149'); }
}

async function cloneSnapshot() {
    const tag = document.getElementById('cloneSnapshotTag').value.trim();
    const newName = document.getElementById('cloneNewName').value.trim();
    const pipeline = document.getElementById('clonePipeline').value.trim();
    if (!tag) { showToast('Pick a snapshot to clone', '#f85149'); return; }
    if (!newName) { showToast('Enter a name for the new project', '#f85149'); return; }
    try {
        const body = {tag, new_name: newName};
        if (pipeline) body.pipeline = pipeline;
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(_snapshotProject)}/clone`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.error) { showToast(data.error, '#f85149'); return; }
        showToast(`Cloned '${tag}' → ${newName} (${data.tasks} tasks)`, '#3fb950');
        document.getElementById('cloneNewName').value = '';
        closeSnapshotModal();
        loadData();
    } catch(e) { showToast('Clone failed', '#f85149'); }
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
    document.getElementById('wizardTitle').textContent = '🧙 New Project -- Wizard';
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
    btn.disabled = true; btn.textContent = '✨ Thinking...';

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

    btn.disabled = true; btn.textContent = 'Creating...';
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
    statusEl.textContent = 'Sending...'; statusEl.style.color = '#8b949e';
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
        const fmtM = v => v >= 1e9 ? (v/1e9).toFixed(1)+'B' : v >= 1e6 ? (v/1e6).toFixed(1)+'M' : v >= 1e3 ? (v/1e3).toFixed(0)+'k' : String(v);
        const items = [
            ['Completed', m.tasks_completed, false, 'success'],
            ['Failed (all-time)', m.tasks_failed, false, 'danger'],
            ['Agents run', m.agents_run, false, null],
            ['1st-try success', m.first_attempt_success_rate, true, null],
            ['Total tokens', fmtM(m.total_tokens || 0), false, null],
            ['Avg input tokens', m.avg_input_tokens, false, null],
            ['Avg output tokens', m.avg_output_tokens, false, null],
            ['Avg loops', m.avg_loops_per_agent, false, 'info'],
            ['web_search calls', m.web_search_calls, false, null],
            ['Knowledge files', m.knowledge_files_written, false, 'success'],
        ];
        document.getElementById('metrics-grid').innerHTML = items.map(([label, val, pct, colorClass]) => {
            const isKnowledge = label === 'Knowledge files';
            return `<div class="metric-cell${isKnowledge ? ' metric-cell-clickable' : ''}"${isKnowledge ? ' onclick="openKnowledgeModal()" title="Click to browse knowledge files"' : ''}>
                <div class="metric-label">${label}${isKnowledge ? ' ↗' : ''}</div>
                <div class="metric-value${colorClass ? ' ' + colorClass : ''}">${fmt(val, pct)}</div>
            </div>`;
        }).join('');
    } catch (e) {}
}

/* ─── Analytics panel (roadmap #7) ───────────────────────────────── */
let _analyticsExpanded = false;

function toggleAnalytics() {
    _analyticsExpanded = !_analyticsExpanded;
    const body = document.getElementById('analytics-body');
    const hint = document.getElementById('analytics-toggle-hint');
    body.style.display = _analyticsExpanded ? '' : 'none';
    if (hint) hint.textContent = _analyticsExpanded ? '' : '(click to expand)';
    if (_analyticsExpanded) loadAnalytics();
}

async function loadAnalytics() {
    if (!_analyticsExpanded) return;
    const fmtUsd = v => '$' + (Number(v) || 0).toFixed(2);
    try {
        const ov = await fetch('/api/analytics/overview').then(r => r.json());
        const cells = [
            ['Completed', ov.tasks_completed, 'success'],
            ['Failed', ov.tasks_failed, 'danger'],
            ['Total cost', fmtUsd(ov.total_cost_usd), null],
            ['Cost / task', fmtUsd(ov.avg_cost_per_completed_task), null],
            ['Avg loops', ov.avg_loops, 'info'],
            ['Agents', ov.agents_counted, null],
        ];
        document.getElementById('analytics-overview-grid').innerHTML = cells.map(([l, v, c]) =>
            `<div class="metric-cell"><div class="metric-label">${l}</div><div class="metric-value${c ? ' ' + c : ''}">${v}</div></div>`
        ).join('');
    } catch (e) {}

    try {
        const vr = await fetch('/api/analytics/value-repair').then(r => r.json());
        const rows = (vr.by_project || []).slice(0, 12);
        document.getElementById('analytics-value-repair').innerHTML = rows.length
            ? `<table class="analytics-table"><tr><th>Project</th><th>Value</th><th>Repair</th><th>Ratio</th></tr>${
                rows.map(r => `<tr><td>${r.project}</td><td>${r.value_tasks}</td><td>${r.repair_tasks}</td><td class="${r.value_repair_ratio >= 1 ? 'success' : 'danger'}">${r.value_repair_ratio}x</td></tr>`).join('')
              }</table>`
            : '<div class="analytics-empty">No completed tasks yet</div>';
    } catch (e) {}

    try {
        const sc = await fetch('/api/analytics/ship-candidates').then(r => r.json());
        const rows = (sc.candidates || []).slice(0, 12);
        document.getElementById('analytics-ship').innerHTML = rows.length
            ? `<table class="analytics-table"><tr><th>Project</th><th>Closure</th><th>Val bugs</th><th>Unverif</th></tr>${
                rows.map(r => `<tr><td>${r.project}</td><td>${r.closure_status}</td><td>${r.validation_bugs_last50}</td><td>${r.unverified_completions}</td></tr>`).join('')
              }</table>`
            : '<div class="analytics-empty">No Godot projects found</div>';
    } catch (e) {}

    try {
        const d = await fetch('/api/analytics/deaths').then(r => r.json());
        if (!d.count) {
            document.getElementById('analytics-deaths').innerHTML = '<div class="analytics-empty">No agent signals yet (needs log_extract_signals + finished agents)</div>';
        } else {
            const ts = Object.entries(d.terminal_status || {}).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('');
            const errs = (d.top_errors || []).slice(0, 5).map(([e, n]) => `<tr><td style="font-family:monospace;font-size:11px">${(e || '').replace(/</g, '&lt;')}</td><td>${n}</td></tr>`).join('');
            document.getElementById('analytics-deaths').innerHTML =
                `<table class="analytics-table"><tr><th>Terminal status</th><th>#</th></tr>${ts}</table>` +
                (errs ? `<table class="analytics-table" style="margin-top:8px"><tr><th>Top errors</th><th>#</th></tr>${errs}</table>` : '');
        }
    } catch (e) {}

    try {
        const m = await fetch('/api/analytics/mechanisms').then(r => r.json());
        const mechs = Object.entries(m.mechanisms || {});
        if (!m.count) {
            document.getElementById('analytics-mechanisms').innerHTML = '<div class="analytics-empty">No agent signals yet</div>';
        } else if (!mechs.length) {
            document.getElementById('analytics-mechanisms').innerHTML = `<div class="analytics-empty">No recovery mechanisms fired. Baseline completion: ${(m.completion_rate_without_any_mechanism ?? 0)}</div>`;
        } else {
            document.getElementById('analytics-mechanisms').innerHTML =
                `<table class="analytics-table"><tr><th>Mechanism</th><th>Fired</th><th>Completion rate</th></tr>${
                    mechs.map(([name, s]) => `<tr><td>${name}</td><td>${s.fired_runs}</td><td>${s.completion_rate ?? '—'}</td></tr>`).join('')
                }</table><div style="font-size:11px;color:var(--text-faint,#8b949e);margin-top:4px">Baseline (no mechanism): ${m.completion_rate_without_any_mechanism ?? '—'}</div>`;
        }
    } catch (e) {}
}

/* ─── Knowledge Files Modal ──────────────────────────────────────── */
let _knowledgeFiles = [];

async function openKnowledgeModal() {
    document.getElementById('knowledgeModal').classList.add('active');
    const sel = document.getElementById('knowledgeSearch');
    sel.innerHTML = '<option value="">All projects</option>';
    const list = document.getElementById('knowledgeList');
    list.innerHTML = '<div style="color:var(--text-faint);font-size:13px">Loading...</div>';
    try {
        const data = await fetch('/api/metrics/knowledge-files').then(r => r.json());
        _knowledgeFiles = data.files || [];
        document.getElementById('knowledgeModalTitle').textContent = `Knowledge Files (${_knowledgeFiles.length})`;
        // Populate dropdown
        _knowledgeFiles.forEach(f => {
            const opt = document.createElement('option');
            opt.value = f.project;
            opt.textContent = f.project;
            sel.appendChild(opt);
        });
        sel.value = '';
        renderKnowledgeList(_knowledgeFiles);
    } catch(e) {
        list.innerHTML = '<div style="color:var(--red,#f85149);font-size:13px">Failed to load</div>';
    }
}

function closeKnowledgeModal() {
    document.getElementById('knowledgeModal').classList.remove('active');
}

function filterKnowledge() {
    const q = document.getElementById('knowledgeSearch').value;
    const list = document.getElementById('knowledgeList');
    if (!q) {
        list.innerHTML = '<div style="color:var(--text-faint);font-size:13px">Select a project above to view its knowledge file.</div>';
        return;
    }
    const f = _knowledgeFiles.find(f => f.project === q);
    if (!f) { list.innerHTML = '<div style="color:var(--text-faint);font-size:13px">Not found.</div>'; return; }
    list.innerHTML = `<div style="font-family:'IBM Plex Sans',sans-serif;font-size:13px;line-height:1.7;color:var(--text)">${renderMarkdown(f.content)}</div>`;
}

function renderMarkdown(md) {
    // Simple markdown renderer: headings, bold, inline code, code blocks, bullets, horizontal rules
    const lines = md.split('\n');
    let html = '';
    let inCode = false;
    let codeBuf = '';
    let inList = false;

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];

        if (line.startsWith('```')) {
            if (inCode) {
                html += `<pre style="background:var(--bg-input,#0d1117);border:1px solid var(--border-faint);border-radius:5px;padding:10px 12px;overflow-x:auto;font-size:11px;font-family:'IBM Plex Mono',monospace;color:var(--text-muted,#8b949e);margin:8px 0">${escapeHtml(codeBuf)}</pre>`;
                codeBuf = ''; inCode = false;
            } else { inCode = true; }
            continue;
        }
        if (inCode) { codeBuf += line + '\n'; continue; }

        if (inList && !line.match(/^[\-\*] /)) { html += '</ul>'; inList = false; }

        if (line.startsWith('### ')) {
            html += `<div style="font-size:12px;font-weight:700;color:var(--text-faint);text-transform:uppercase;letter-spacing:.06em;margin:18px 0 4px">${escapeHtml(line.slice(4))}</div>`;
        } else if (line.startsWith('## ')) {
            html += `<div style="font-size:14px;font-weight:700;color:var(--cyan,#79c0ff);margin:20px 0 6px;border-bottom:1px solid var(--border-faint);padding-bottom:4px">${escapeHtml(line.slice(3))}</div>`;
        } else if (line.startsWith('# ')) {
            html += `<div style="font-size:16px;font-weight:700;color:var(--text);margin:0 0 12px">${escapeHtml(line.slice(2))}</div>`;
        } else if (line.match(/^[\-\*] /)) {
            if (!inList) { html += '<ul style="margin:4px 0 4px 16px;padding:0;list-style:disc">'; inList = true; }
            html += `<li style="margin:2px 0">${inlineMd(line.slice(2))}</li>`;
        } else if (line.match(/^---+$/)) {
            html += `<hr style="border:none;border-top:1px solid var(--border-faint);margin:12px 0">`;
        } else if (line.trim() === '') {
            html += '<div style="height:6px"></div>';
        } else {
            html += `<div>${inlineMd(line)}</div>`;
        }
    }
    if (inList) html += '</ul>';
    if (inCode) html += `<pre style="background:var(--bg-input,#0d1117);border:1px solid var(--border-faint);border-radius:5px;padding:10px 12px;font-size:11px;font-family:'IBM Plex Mono',monospace;color:var(--text-muted,#8b949e)">${escapeHtml(codeBuf)}</pre>`;
    return html;
}

function inlineMd(text) {
    return escapeHtml(text)
        .replace(/`([^`]+)`/g, '<code style="background:var(--bg-input,#0d1117);border:1px solid var(--border-faint);border-radius:3px;padding:1px 5px;font-size:11px;font-family:\'IBM Plex Mono\',monospace;color:var(--text-accent,#ff7b72)">$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

/* ── Meta Mode ───────────────────────────────────────────────── */
let _metaModeEnabled = false;
let _metaAgentsData = {};

const META_AGENTS = [
    { id: 'gardener',     label: 'Gardener',     statusEndpoint: '/api/gardener/status',     runEndpoint: '/api/gardener/run',     configEndpoint: '/api/gardener/config'     },
    { id: 'librarian',    label: 'Librarian',    statusEndpoint: '/api/librarian/status',    runEndpoint: '/api/librarian/run',    configEndpoint: '/api/librarian/config'    },
    { id: 'cartographer', label: 'Cartographer', statusEndpoint: '/api/cartographer/status',  runEndpoint: '/api/cartographer/run',  configEndpoint: '/api/cartographer/config'  },
    { id: 'archaeologist',label: 'Archaeologist',statusEndpoint: '/api/archaeologist/status',runEndpoint: '/api/archaeologist/run', configEndpoint: '/api/archaeologist/config' },
    { id: 'auditor',      label: 'Auditor',      statusEndpoint: '/api/meta-auditor/status', runEndpoint: '/api/meta-auditor/run', configEndpoint: '/api/meta-auditor/config' },
    { id: 'scheduler',    label: 'Scheduler',    statusEndpoint: '/api/scheduler/status',    runEndpoint: '/api/scheduler/run',    configEndpoint: '/api/scheduler/config'    },
];

async function loadMetaModeState() {
    try {
        const res = await fetch(API + '/api/meta-mode');
        if (!res.ok) return;
        const data = await res.json();
        _metaModeEnabled = data.meta_mode_enabled || false;
        _metaAgentsData = data.agents || {};
        _updateMetaModeToggle();
        _renderMetaAgentsTable();
    } catch(e) {}
}

function _updateMetaModeToggle() {
    const btn = document.getElementById('metaModeToggleBtn');
    if (!btn) return;
    if (_metaModeEnabled) {
        btn.textContent = '⚡ On';
        btn.classList.add('active');
    } else {
        btn.textContent = '⚡ Off';
        btn.classList.remove('active');
    }
    const row = document.getElementById('metaModeStatusRow');
    const text = document.getElementById('metaModeStatusText');
    if (row && text) {
        row.style.display = '';
        const agentCount = Object.keys(_metaAgentsData).length;
        text.textContent = _metaModeEnabled
            ? `${agentCount} agent${agentCount !== 1 ? 's' : ''} available`
            : 'Disabled';
    }
}

function _renderMetaAgentsTable() {
    const section = document.getElementById('metaAgentsSection');
    const table = document.getElementById('metaAgentsTable');
    if (!section || !table) return;
    section.style.display = _metaModeEnabled ? '' : 'none';
    if (!_metaModeEnabled) return;
    table.innerHTML = META_AGENTS.map(agent => {
        const state = _metaAgentsData[agent.id] || {};
        const enabled = state.enabled || false;
        const lastRun = _formatRelativeTime(state.last_run_ts || 0);
        // Librarian gets an extra autonomous-edits toggle
        const isLibrarian = agent.id === 'librarian';
        const autoEdits = state.autonomous_edits || false;
        const autoEditsBtn = isLibrarian
            ? `<button id="mm-auto-${agent.id}" class="autonomous-toggle ${autoEdits ? 'on' : ''}"
                       onclick="toggleLibrarianAutonomous()">
                 Auto ${autoEdits ? 'ON' : 'OFF'}
               </button>`
            : '';
        return `<div class="meta-agent-row">
            <span class="meta-agent-name">${agent.label}</span>
            <span class="meta-agent-last-run">${lastRun}</span>
            <span class="meta-agent-actions">
                ${autoEditsBtn}
                <button id="mm-toggle-${agent.id}"
                        class="meta-agent-toggle-btn ${enabled ? 'enabled' : ''}"
                        onclick="toggleMetaAgent('${agent.id}')">
                    ${enabled ? 'Enabled' : 'Disabled'}
                </button>
                <button id="mm-run-${agent.id}" onclick="runMetaAgent('${agent.id}')">Run</button>
            </span>
        </div>`;
    }).join('');
}

async function toggleMetaMode() {
    const btn = document.getElementById('metaModeToggleBtn');
    if (btn) btn.disabled = true;
    const newState = !_metaModeEnabled;
    try {
        const res = await fetch(API + '/api/meta-mode', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({meta_mode_enabled: newState}),
        });
        if (res.ok) {
            const data = await res.json();
            _metaModeEnabled = data.meta_mode_enabled || false;
            _metaAgentsData = data.agents || {};
            _updateMetaModeToggle();
            _renderMetaAgentsTable();
            showToast('Meta Mode ' + (_metaModeEnabled ? 'enabled' : 'disabled'));
        }
    } catch(e) {
        showToast('Error: ' + e.message, '#f85149');
    }
    if (btn) btn.disabled = false;
}

async function toggleMetaAgent(agentId) {
    const agent = META_AGENTS.find(a => a.id === agentId);
    if (!agent) return;
    const btn = document.getElementById('mm-toggle-' + agentId);
    if (btn) btn.disabled = true;
    const currentlyEnabled = (_metaAgentsData[agentId] || {}).enabled || false;
    const newState = !currentlyEnabled;
    const enabledKey = agentId + '_enabled';
    try {
        const res = await fetch(API + agent.configEndpoint, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({[enabledKey]: newState}),
        });
        if (res.ok) {
            _metaAgentsData[agentId] = _metaAgentsData[agentId] || {};
            _metaAgentsData[agentId].enabled = newState;
            _renderMetaAgentsTable();
            showToast(agent.label + ' ' + (newState ? 'enabled' : 'disabled'));
        } else {
            showToast('Error toggling ' + agent.label, '#f85149');
        }
    } catch(e) {
        showToast('Error: ' + e.message, '#f85149');
    }
}

async function runMetaAgent(agentId) {
    const agent = META_AGENTS.find(a => a.id === agentId);
    if (!agent) return;
    const btn = document.getElementById('mm-run-' + agentId);
    if (btn) { btn.disabled = true; btn.textContent = '...'; }
    try {
        const res = await fetch(API + agent.runEndpoint, { method: 'POST' });
        const data = await res.json();
        const label = agent.label;
        if (data.task_id) {
            showToast(label + ' task: ' + data.task_id);
        } else if (data.error) {
            showToast(label + ': ' + data.error, '#f85149');
        } else {
            showToast(label + ' triggered');
        }
    } catch(e) {
        showToast('Error: ' + e.message, '#f85149');
    }
    if (btn) { btn.disabled = false; btn.textContent = 'Run'; }
}

async function toggleLibrarianAutonomous() {
    const btn = document.getElementById('mm-auto-librarian');
    if (btn) btn.disabled = true;
    try {
        const res = await fetch(API + '/api/librarian/autonomous-edits', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({}),
        });
        if (res.ok) {
            const data = await res.json();
            const isOn = data.autonomous_edits || false;
            if (btn) btn.classList.toggle('on', isOn);
            if (btn) btn.textContent = 'Auto ' + (isOn ? 'ON' : 'OFF');
            showToast('Librarian autonomous edits ' + (isOn ? 'enabled' : 'disabled'));
        } else {
            showToast('Failed to toggle autonomous edits', '#f85149');
        }
    } catch(e) {
        showToast('Error: ' + e.message, '#f85149');
    }
    if (btn) btn.disabled = false;
}

/* ── Gardener ─────────────────────────────────────────────────────────────── */
let gardenerEnabled = false;
async function loadGardenerState() {
    try {
        const res = await fetch(API + '/api/gardener/status');
        if (res.ok) {
            const data = await res.json();
            gardenerEnabled = data.enabled || false;
            _updateGardenerToggle();
            _updateGardenerStatusRow(data);
        }
    } catch(e) {}
}

function _updateGardenerToggle() {
    const btn = document.getElementById('gardenerToggleBtn');
    if (!btn) return;
    if (gardenerEnabled) {
        btn.textContent = '\u27f3 On';
        btn.classList.add('active');
    } else {
        btn.textContent = '\u27f3 Off';
        btn.classList.remove('active');
    }
}

function _updateGardenerStatusRow(data) {
    const row = document.getElementById('gardenerStatusRow');
    if (!row) return;
    row.style.display = '';
    const lastRun = document.getElementById('gardenerLastRun');
    const count = document.getElementById('gardenerKnowledgeCount');
    if (lastRun) lastRun.textContent = _formatRelativeTime(data.last_run_ts);
    if (count) count.textContent = (data.knowledge_count !== undefined) ? data.knowledge_count : '-';
}

async function toggleGardener() {
    const btn = document.getElementById('gardenerToggleBtn');
    if (btn) btn.disabled = true;
    const newState = !gardenerEnabled;
    try {
        const res = await fetch(API + '/api/gardener/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({gardener_enabled: newState}),
        });
        if (res.ok) {
            const data = await res.json();
            gardenerEnabled = data.gardener_enabled || false;
        }
    } catch(e) {
        gardenerEnabled = !newState;
    }
    _updateGardenerToggle();
    if (btn) btn.disabled = false;
}

async function runGardener() {
    const btn = document.getElementById('runGardenerBtn');
    const status = document.getElementById('gardenerRunStatus');
    if (btn) btn.disabled = true;
    if (status) status.textContent = 'Running...';
    try {
        const res = await fetch(API + '/api/gardener/run', {method: 'POST'});
        const data = await res.json();
        if (status) {
            status.textContent = 'Task created: ' + (data.task_id || 'OK');
            status.style.color = 'var(--green)';
        }
        showToast('Gardener task created: ' + (data.task_id || 'OK'));
    } catch(e) {
        if (status) {
            status.textContent = 'Error: ' + e.message;
            status.style.color = 'var(--red)';
        }
    }
    if (btn) btn.disabled = false;
}

async function openGardenerKnowledgePanel() {
    const panel = document.getElementById('gardenerKnowledgePanel');
    if (!panel) return;
    panel.classList.add('active');
    const entriesDiv = document.getElementById('gkEntries');
    const countEl = document.getElementById('gkEntryCount');
    if (!entriesDiv) return;
    entriesDiv.innerHTML = '<div style="color:var(--text-muted);font-size:13px;text-align:center;padding:32px">Loading...</div>';
    try {
        const res = await fetch(API + '/api/gardener/knowledge');
        const data = await res.json();
        const entries = data.entries || [];
        if (countEl) countEl.textContent = entries.length + ' entry' + (entries.length === 1 ? '' : 's');
        if (entries.length === 0) {
            entriesDiv.innerHTML = '<div class="gk-empty">No knowledge entries yet. Run the gardener to collect patterns.</div>';
            return;
        }
        entriesDiv.innerHTML = entries.map(entry => _renderGardenerEntry(entry)).join('');
    } catch(e) {
        entriesDiv.innerHTML = '<div style="color:var(--red);font-size:13px;padding:32px;text-align:center">Failed to load: ' + escapeHtml(e.message) + '</div>';
    }
}

function _renderGardenerEntry(entry) {
    const conf = entry.confidence || 'suspected';
    const confClass = conf === 'confirmed' ? 'confirmed' : conf === 'suspected' ? 'suspected' : 'disputed';
    const projects = (entry.affected_projects || []).join(', ') || 'any project';
    const lastSeen = _formatRelativeTime(entry.last_seen_ts || entry.last_seen);
    const ttl = entry.ttl_days || 0;
    const fix = entry.fix_summary || entry.fix || '(no summary)';
    const sig = escapeHtml(entry.pattern_signature || entry.sig || '(unknown pattern)');
    return `<div class="gk-entry">
        <div class="gk-entry-header">
            <span class="gk-entry-sig">${sig}</span>
            <span class="gk-badge ${confClass}">${conf}</span>
            <span class="gk-entry-meta">${lastSeen} | TTL: ${ttl}d</span>
        </div>
        <div class="gk-entry-fix">${escapeHtml(fix)}</div>
        <div class="gk-entry-projects">Affected: ${escapeHtml(projects)}</div>
    </div>`;
}

/* Relative time helper */
function _formatRelativeTime(ts) {
    if (!ts || ts === 0) return 'never';
    const now = Date.now() / 1000;
    const diff = now - ts;
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return Math.floor(diff / 86400) + 'd ago';
}
