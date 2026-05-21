// file:// gives origin="null"; fall back to localhost for local dev/testing
const API = (window.location.protocol === 'file:') ? 'http://localhost:5001' : window.location.origin;

/* ─── Theme switcher ─────────────────────────────────────────────── */
const THEMES = ['cyberpunk', 'dark', 'light'];
const THEME_LABELS = { cyberpunk: '🌆', dark: '🌑', light: '☀️' };

function initTheme() {
    const saved = localStorage.getItem('swarm-theme') || 'cyberpunk';
    applyTheme(saved);
}

function applyTheme(theme) {
    document.body.setAttribute('data-theme', theme);
    const btn = document.getElementById('themeToggleBtn');
    if (btn) btn.textContent = THEME_LABELS[theme] || '🎨';
    localStorage.setItem('swarm-theme', theme);
}

function cycleTheme() {
    const current = document.body.getAttribute('data-theme') || 'cyberpunk';
    const idx = THEMES.indexOf(current);
    const next = THEMES[(idx + 1) % THEMES.length];
    applyTheme(next);
}

// Apply before first paint
initTheme();

async function loadData() {
        console.log('Loading data...');
    try {
        const [projectsRes, tasksRes, agentsRes, historyRes, quotaRes, managedRes] = await Promise.all([
            fetch(API + '/api/projects'),
            fetch(API + '/api/tasks'),
            fetch(API + '/api/agents'),
            fetch(API + '/api/history'),
            fetch(API + '/api/quota'),
            fetch(API + '/api/managed-projects'),
        ]);

        const projects = await projectsRes.json();
        const tasks = await tasksRes.json();
        const agentsData = await agentsRes.json();
        const historyData = await historyRes.json();
        if (managedRes.ok) {
            const managedData = await managedRes.json();
            _pausedProjects = new Set(managedData.paused_projects || []);
        }
        const quotaData = await quotaRes.json();

        // Quota meter (use MiniMax-M2.5 entry)
        const model = (quotaData.model_remains || []).find(m => m.model_name === 'MiniMax-M2.5')
                   || (quotaData.model_remains || [])[0];
        if (model) {
            const remaining = model.current_interval_usage_count;
            const total = model.current_interval_total_count;
            const used = total - remaining;
            const pct = total > 0 ? (used / total * 100) : 0;
            const fill = document.getElementById('quotaFill');
            fill.style.width = pct + '%';
            fill.style.background = pct > 90 ? '#da3633' : pct > 70 ? '#f0883e' : '#238636';
            document.getElementById('quotaText').textContent = `${used.toLocaleString()} / ${total.toLocaleString()} (${pct.toFixed(1)}%)`;
            const ms = model.remains_time;
            const h = Math.floor(ms / 3600000);
            const m = Math.floor((ms % 3600000) / 60000);
            document.getElementById('quotaReset').textContent = `resets in ${h}h ${m}m`;
        } else if (quotaData.error) {
            document.getElementById('quotaText').textContent = quotaData.error;
        }
        
        document.getElementById('lastUpdated').textContent = new Date().toLocaleTimeString();
        
        // Summary
        const projectList = projects.projects || {};
        _projectList = projectList; // make available to Add Task modal
        let overflow = 0;
        for (const [name, data] of Object.entries(projectList)) {
            const files = data.files || {};
            if (Object.values(files).some(l => l > maxLines)) overflow++;
        }
        
        const allTasks = tasks.tasks || [];
        const active = allTasks.filter(t => t.status === 'in_progress').length;
        const pending = allTasks.filter(t => t.status === 'pending').length;

        const allAgents = agentsData.agents || [];
        const activeAgents = allAgents.filter(a => a.status === 'active');
        const recentAgents = allAgents.slice(-12); // show last 12

        // Count completed agents from history in the last 24 hours
        const cutoff = Date.now() - 24 * 60 * 60 * 1000;
        const historyAgentsList = historyData.agents || [];
        const completedToday = historyAgentsList.filter(a => {
            if (a.status !== 'completed') return false;
            const t = a.completed_at ? new Date(a.completed_at).getTime() : 0;
            return t > cutoff;
        }).length;

        const totalTokenCount = activeAgents.reduce((sum, a) => sum + (a.input_tokens || 0) + (a.output_tokens || 0), 0);
        const totalTokensLabel = totalTokenCount > 0 ? `${(totalTokenCount/1000).toFixed(1)}k` : '0';

        // Build project-level token totals and active-project set from active agents
        _projectTokenMap = {};
        _activeProjectSet = new Set();
        for (const a of activeAgents) {
            if (a.project) _activeProjectSet.add(a.project);
            const tok = (a.input_tokens || 0) + (a.output_tokens || 0);
            if (tok > 0 && a.project) {
                _projectTokenMap[a.project] = (_projectTokenMap[a.project] || 0) + tok;
            }
        }

        document.getElementById('totalProjects').textContent = Object.keys(projectList).length;
        document.getElementById('activeTasks').textContent = activeAgents.length;
        document.getElementById('pendingTasks').textContent = pending;
        document.getElementById('completedTasks').textContent = completedToday;
        document.getElementById('overflowCount').textContent = overflow;

        // Count active sub-tasks (in_progress with parent_task_id)
        const activeSubTasks = allTasks.filter(t => 
            t.status === 'in_progress' && 
            t.metadata && 
            t.metadata.parent_task_id
        ).length;
        document.getElementById('activeSubTasks').textContent = activeSubTasks;
        document.getElementById('totalTokens').textContent = totalTokensLabel;

        // Agents Grid (from agents.json) — active first, then recent
        _taskDescMap = {};
        _taskDataMap = {};
        _agentByTaskId = {};
        allTasks.forEach(t => { _taskDescMap[t.id] = t.description || ''; _taskDataMap[t.id] = t; });
        allAgents.filter(a => a.status === 'active').forEach(a => { if (a.task_id) _agentByTaskId[a.task_id] = a; });
        const agentsGrid = document.getElementById('activeTasksGrid');
        if (recentAgents.length > 0) {
            agentsGrid.innerHTML = recentAgents.slice().reverse().map(agent => createAgentCard(agent, _taskDescMap[agent.task_id] || '')).join('');
        } else {
            agentsGrid.innerHTML = '<div class="card"><div class="stat">No agents yet</div></div>';
        }
        
        // Pending tasks - sort and group by parent hierarchy
        const pendingGrid = document.getElementById('pendingTasksGrid');
        const pendingTasks = allTasks.filter(t => t.status === 'pending');
        
        // Build parent->children map
        const childrenMap = {};
        const parentIds = new Set();
        
        pendingTasks.forEach(t => {
            if (t.metadata && t.metadata.parent_task_id) {
                const pid = t.metadata.parent_task_id;
                if (!childrenMap[pid]) childrenMap[pid] = [];
                childrenMap[pid].push(t);
                parentIds.add(pid);
            }
        });
        
        // Sort children by task_depth then by creation order
        Object.values(childrenMap).forEach(children => {
            children.sort((a, b) => {
                const depthA = (a.metadata && a.metadata.task_depth) || 0;
                const depthB = (b.metadata && b.metadata.task_depth) || 0;
                if (depthA !== depthB) return depthA - depthB;
                return (a.created_at || '').localeCompare(b.created_at || '');
            });
        });
        
        // Parents first (those with children shown first), then render with hierarchy
        const parents = pendingTasks.filter(t => !t.metadata || !t.metadata.parent_task_id);
        parents.sort((a, b) => (b.priority || 50) - (a.priority || 50));
        
        pendingGrid.innerHTML = parents.map(t => 
            renderTaskWithChildren(t, childrenMap)
        ).join('');
        
        // Projects
        const projectsGrid = document.getElementById('projectsGrid');
        // Projects with active/pending tasks always visible; rest sorted by file size
        const projectTaskCount = {};
        const projectAnyTaskCount = {};
        for (const t of allTasks) {
            if (t.status === 'pending' || t.status === 'in_progress')
                projectTaskCount[t.project] = (projectTaskCount[t.project] || 0) + 1;
            projectAnyTaskCount[t.project] = (projectAnyTaskCount[t.project] || 0) + 1;
        }
        // Sidebar: project list + task counts
        const projectTaskCounts = {};
        for (const t of allTasks) {
            if (t.status === 'pending' || t.status === 'in_progress')
                projectTaskCounts[t.project] = (projectTaskCounts[t.project] || 0) + 1;
        }
        _sidebarTaskCounts = projectTaskCounts;

        // Compute per-project velocity from history
        const now7d = Date.now() - 7 * 24 * 60 * 60 * 1000;
        const nowToday = Date.now() - 24 * 60 * 60 * 1000;
        const velocityMap = {};
        const activityMap = {};
        function markProjectActivity(project, timestamp) {
            if (!project || !timestamp) return;
            const ms = new Date(timestamp).getTime();
            if (!Number.isFinite(ms) || ms <= 0) return;
            activityMap[project] = Math.max(activityMap[project] || 0, ms);
        }
        for (const a of historyAgentsList) {
            if (a.status !== 'completed' || !a.project) continue;
            const t = a.completed_at ? new Date(a.completed_at).getTime() : 0;
            if (!velocityMap[a.project]) velocityMap[a.project] = {week: 0, today: 0};
            if (t > now7d) velocityMap[a.project].week++;
            if (t > nowToday) velocityMap[a.project].today++;
            markProjectActivity(a.project, a.completed_at || a.spawned_at);
        }
        for (const a of allAgents) {
            markProjectActivity(a.project, a.completed_at || a.spawned_at);
        }
        for (const [name, data] of Object.entries(projectList)) {
            markProjectActivity(name, data.last_update);
        }

        const sortedProjects = sortProjectEntries(Object.entries(projectList), projectTaskCount, activityMap);
        const visibleProjects = getVisibleProjectEntries(sortedProjects, activityMap);
        const sidebarProjectNames = sortedProjects.map(([name]) => name);
        _allProjectNames = sidebarProjectNames;
        renderSidebar(sidebarProjectNames, _sidebarTaskCounts);
        applyProjectFilter();
        updateProjectSortControls(visibleProjects.length, sortedProjects.length);

        projectsGrid.innerHTML = visibleProjects
            .map(([name, data]) => createProjectCard(name, data, projectAnyTaskCount[name] || 0, velocityMap[name] || null))
            .join('');

        // Load notes for all visible project cards
        visibleProjects.forEach(([name]) => {
            const id = `notes-${name.replace(/[^a-z0-9]/gi, '_')}`;
            loadProjectNotes(name, id);
        });

        // Fetch health and closure for visible projects, then re-render with full data.
        // Awaited so callers (e.g. openProjectClosure) see the final DOM state.
        await Promise.all(visibleProjects.map(([name]) => Promise.all([
            fetchProjectHealth(name),
            fetchProjectClosure(name),
        ])));
        projectsGrid.innerHTML = visibleProjects
            .map(([name, data]) => createProjectCard(name, data, projectAnyTaskCount[name] || 0, velocityMap[name] || null))
            .join('');
        // Re-load notes after re-render
        visibleProjects.forEach(([name]) => {
            const id = `notes-${name.replace(/[^a-z0-9]/gi, '_')}`;
            loadProjectNotes(name, id);
        });

        const historyAgents = historyData.agents || [];
        const historyGrid = document.getElementById('historyGrid');
        historyGrid.innerHTML = historyAgents.length
            ? historyAgents.map(a => createHistoryCard(a)).join('')
            : '<div class="card"><div class="stat">No history yet</div></div>';

        loadIntegrityPanel();

    } catch (e) {
        console.error('Error loading data:', e);
    }
}

function escapeHtml(str) {
    return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function showToast(html, color) {
    color = color || '#3fb950';
    const el = document.createElement('div');
    el.innerHTML = html;
    el.style.cssText = `position:fixed;bottom:24px;right:24px;z-index:9999;background:#161b22;border:1px solid ${color};border-radius:6px;padding:12px 18px;font-size:13px;color:#e6edf3;max-width:380px;line-height:1.5;box-shadow:0 4px 16px rgba(0,0,0,.5);transition:opacity .4s`;
    document.body.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 400); }, 4000);
}

function _integrityScopeLabel() {
    return _selectedProject ? `Project: ${_selectedProject}` : 'All projects';
}

function _integrityActionButton(label, onClick, extraClass = '') {
    return `<button type="button" class="integrity-action-btn ${extraClass}" onclick="${onClick}">${label}</button>`;
}

function renderIntegrityPanel(data) {
    if (window.SwarmDepsIntegrityUI) return window.SwarmDepsIntegrityUI.renderIntegrityPanel(data);
}

async function loadIntegrityPanel() {
    if (window.SwarmDepsIntegrityUI) return window.SwarmDepsIntegrityUI.loadIntegrityPanel();
}

async function runIntegrityRepair(kind, projectName = null) {
    if (window.SwarmDepsIntegrityUI) return window.SwarmDepsIntegrityUI.runIntegrityRepair(kind, projectName);
}

async function repairProject(event, name) {
    event.stopPropagation();
    const btn = event.target;
    btn.disabled = true; btn.textContent = '…';
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(name)}/repair`, {method: 'POST'});
        const data = await res.json();
        if (data.error) {
            showToast(`Repair failed: ${escapeHtml(data.error)}`, '#f85149');
        } else {
            const parts = [];
            if (data.reset_failed.length)   parts.push(`${data.reset_failed.length} failed → pending`);
            if (data.reset_orphaned.length) parts.push(`${data.reset_orphaned.length} orphaned → pending`);
            if (data.resurrected.length)    parts.push(`${data.resurrected.length} deps resurrected`);
            showToast(`<strong>${escapeHtml(name)}</strong>: ${parts.length ? parts.join(', ') : 'nothing to repair'}`, '#e3b341');
            if (parts.length) loadData();
        }
    } catch(e) { showToast('Network error during repair', '#f85149'); }
    finally { btn.disabled = false; btn.textContent = '🔧 Repair'; }
}

async function loadAutoReplan() {
    try {
        const data = await fetch(API + '/api/auto-replan').then(r => r.json());
        _autoReplanProjects = new Set(data.auto_replan_projects || []);
    } catch(e) {}
}

async function toggleAutoReplan(event, name) {
    const btn = event.target;
    const nowEnabled = !_autoReplanProjects.has(name);
    btn.disabled = true;
    try {
        const res = await fetch(`${API}/api/auto-replan/${encodeURIComponent(name)}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: nowEnabled}),
        });
        const data = await res.json();
        _autoReplanProjects = new Set(data.auto_replan_projects || []);
        showToast(`Auto re-plan ${nowEnabled ? 'enabled' : 'disabled'} for <strong>${escapeHtml(name)}</strong>`, nowEnabled ? '#a371f7' : '#8b949e');
        loadData();
    } catch(e) { showToast('Error toggling auto re-plan', '#f85149'); }
    finally { btn.disabled = false; }
}

async function replanProject(event, name) {
    const btn = event.target;
    if (!confirm(`Spawn a project_plan agent for "${name}"? This will analyse the codebase and create new tasks.`)) return;
    btn.disabled = true; btn.textContent = '⏳ Planning…';
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(name)}/replan`, {method: 'POST'});
        const data = await res.json();
        if (!res.ok) { showToast(`Re-plan failed: ${escapeHtml(data.error)}`, '#f85149'); return; }
        showToast(`Re-plan queued for <strong>${escapeHtml(name)}</strong>`, '#a371f7');
    } catch(e) { showToast('Network error during re-plan', '#f85149'); }
    finally { btn.disabled = false; btn.textContent = '🗺 Re-plan'; }
}

async function restartProject(event, name) {
    event.stopPropagation();
    if (!confirm(`Reset ALL tasks for "${name}" to pending?\n\nThis clears failure history and retries everything from scratch.\nKill any active agents for this project first.`)) return;
    const btn = event.target;
    btn.disabled = true; btn.textContent = '…';
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(name)}/restart`, {method: 'POST'});
        const data = await res.json();
        if (data.error) {
            showToast(`Restart failed: ${escapeHtml(data.error)}`, '#f85149');
        } else {
            showToast(`<strong>${escapeHtml(name)}</strong>: ${data.reset} task(s) reset to pending`, '#f0883e');
            loadData();
        }
    } catch(e) { showToast('Network error during restart', '#f85149'); }
    finally { btn.disabled = false; btn.textContent = '↺ Restart'; }
}

async function verifyProjectClosure(event, name) {
    event.stopPropagation();
    const btn = event.target;
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(name)}/closure/verify`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({}),
        });
        const data = await res.json();
        if (!res.ok || data.error) {
            showToast(`Closure verify failed: ${escapeHtml((data && data.error) || 'unknown error')}`, '#f85149');
            return;
        }
        const status = data.verification_run && data.verification_run.status ? data.verification_run.status : 'queued';
        showToast(`<strong>${escapeHtml(name)}</strong>: closure verification ${escapeHtml(status)}`, '#3fb950');
        loadData();
    } catch(e) {
        showToast('Network error during closure verification', '#f85149');
    } finally {
        btn.disabled = false;
        btn.textContent = original;
    }
}

async function queueClosureRepair(event, name) {
    event.stopPropagation();
    const btn = event.target;
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(name)}/closure/repair`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({}),
        });
        const data = await res.json();
        if (!res.ok || data.error) {
            showToast(`Closure repair failed: ${escapeHtml((data && data.error) || 'unknown error')}`, '#f85149');
            return;
        }
        const count = Array.isArray(data.repair_tasks) ? data.repair_tasks.length : 0;
        showToast(`<strong>${escapeHtml(name)}</strong>: generated ${count} closure repair task${count === 1 ? '' : 's'}`, '#e3b341');
        loadData();
    } catch(e) {
        showToast('Network error during closure repair generation', '#f85149');
    } finally {
        btn.disabled = false;
        btn.textContent = original;
    }
}

async function setClosureMode(event, name, mode) {
    event.stopPropagation();
    const btn = event.target;
    const original = btn.textContent;
    btn.disabled = true;
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(name)}/closure/mode`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({mode}),
        });
        const data = await res.json();
        if (!res.ok || data.error) {
            showToast(`Closure mode update failed: ${escapeHtml((data && data.error) || 'unknown error')}`, '#f85149');
            return;
        }
        showToast(`<strong>${escapeHtml(name)}</strong>: closure mode → ${escapeHtml(mode)}`, '#58a6ff');
        loadData();
    } catch(e) {
        showToast('Network error during closure mode update', '#f85149');
    } finally {
        btn.disabled = false;
        btn.textContent = original;
    }
}

async function previewClosureProposal(event, name) {
    event.stopPropagation();
    const btn = event.target;
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(name)}/closure/proposal`);
        const data = await res.json();
        if (!res.ok || data.error || !data.proposal) {
            showToast(`Closure proposal fetch failed: ${escapeHtml((data && data.error) || 'unknown error')}`, '#f85149');
            return;
        }
        closureProposalCache[name] = data.proposal;
        const flow = (((data.proposal || {}).closure_spec || {}).critical_flows || [])[0] || {};
        showToast(`<strong>${escapeHtml(name)}</strong>: proposal ready (${escapeHtml(flow.id || 'main-flow')})`, '#58a6ff');
        loadData();
    } catch(e) {
        showToast('Network error during closure proposal fetch', '#f85149');
    } finally {
        btn.disabled = false;
        btn.textContent = original;
    }
}

async function applyClosureProposal(event, name) {
    event.stopPropagation();
    const btn = event.target;
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(name)}/closure/proposal/apply`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({}),
        });
        const data = await res.json();
        if (!res.ok || data.error || !data.proposal) {
            showToast(`Closure proposal apply failed: ${escapeHtml((data && data.error) || 'unknown error')}`, '#f85149');
            return;
        }
        closureProposalCache[name] = data.proposal;
        if (data.closure) closureCache[name] = data.closure;
        showToast(`<strong>${escapeHtml(name)}</strong>: closure proposal applied`, '#3fb950');
        loadData();
    } catch(e) {
        showToast('Network error during closure proposal apply', '#f85149');
    } finally {
        btn.disabled = false;
        btn.textContent = original;
    }
}

async function copyClosureSpec(event, name, textareaId) {
    event.stopPropagation();
    const textarea = document.getElementById(textareaId);
    if (!textarea) {
        showToast(`Closure copy failed: editor not found for ${escapeHtml(name)}`, '#f85149');
        return;
    }
    try {
        const text = textarea.value || '';
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
        } else {
            textarea.focus();
            textarea.select();
            document.execCommand('copy');
        }
        showToast(`<strong>${escapeHtml(name)}</strong>: closure JSON copied`, '#58a6ff');
    } catch (e) {
        showToast('Network error during closure copy', '#f85149');
    }
}

async function saveClosureSpec(event, name, textareaId) {
    event.stopPropagation();
    const btn = event.target;
    const textarea = document.getElementById(textareaId);
    if (!textarea) {
        showToast(`Closure save failed: editor not found for ${escapeHtml(name)}`, '#f85149');
        return;
    }
    let closureSpec;
    try {
        closureSpec = JSON.parse(textarea.value || '{}');
    } catch (e) {
        showToast(`Closure save failed: invalid JSON (${escapeHtml(e.message)})`, '#f85149');
        return;
    }
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(name)}/closure/spec`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({closure_spec: closureSpec}),
        });
        const data = await res.json();
        if (!res.ok || data.error || !data.closure) {
            showToast(`Closure save failed: ${escapeHtml((data && data.error) || 'unknown error')}`, '#f85149');
            return;
        }
        closureCache[name] = data.closure;
        textarea.value = JSON.stringify((data.closure && data.closure.closure_spec) || closureSpec, null, 2);
        showToast(`<strong>${escapeHtml(name)}</strong>: closure contract saved`, '#3fb950');
        loadData();
    } catch (e) {
        showToast('Network error during closure save', '#f85149');
    } finally {
        btn.disabled = false;
        btn.textContent = original;
    }
}

async function killAgent(agentId, event) {
    event.stopPropagation();
    const btn = event.target;
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const res = await fetch(`${API}/api/agents/${agentId}/kill`, {method: 'POST'});
        const data = await res.json();
        if (data.success) {
            btn.textContent = '✓';
            setTimeout(loadData, 1000);
        } else {
            btn.textContent = '✗';
            btn.disabled = false;
        }
    } catch(e) {
        btn.textContent = '✗';
        btn.disabled = false;
    }
}

async function killAllAgents() {
    if (!confirm('Kill all running agents?')) return;
    const btn = document.getElementById('killAllBtn');
    btn.disabled = true;
    btn.textContent = '⏹ Killing…';
    try {
        const res = await fetch(API + '/api/agents');
        const data = await res.json();
        const active = (data.agents || []).filter(a => a.status === 'active');
        await Promise.all(active.map(a =>
            fetch(`${API}/api/agents/${a.id}/kill`, {method: 'POST'})
        ));
        setTimeout(loadData, 1000);
    } catch(e) {}
    btn.textContent = '⏹ Kill All';
    btn.disabled = false;
}

function createAgentCard(agent, taskDesc) {
    const isActive = agent.status === 'active';
    const statusLabel = agent.status || "unknown";
    const meta = agent.metadata || {};
    const diffStat = meta.diff_stat ? meta.diff_stat.split('\n').pop() : '';
    const descSnippet = taskDesc ? escapeHtml(taskDesc.split('\n')[0].slice(0, 100)) : '';
    const totalTokens = (agent.input_tokens || 0) + (agent.output_tokens || 0);
    const loopHtml = isActive && agent.current_loop
        ? `<div class="agent-stat">Loop <strong>${agent.current_loop}/${agent.max_loops || 120}</strong></div>`
        : '';
    const convEst = (meta.conv_estimate || 0);
    const compactThreshold = (meta.compact_threshold || 120000);
    const compactPct = Math.min(100, Math.round(convEst / compactThreshold * 100));
    const compactColor = compactPct >= 90 ? '#f85149' : compactPct >= 70 ? '#d29922' : '#3fb950';
    const compactMeterHtml = isActive && convEst > 0 ? `
        <div style="margin-top:6px">
            <div style="display:flex;justify-content:space-between;font-size:10px;color:#6e7681;margin-bottom:2px">
                <span>Context</span><span style="color:${compactColor}">${(convEst/1000).toFixed(0)}k / ${(compactThreshold/1000).toFixed(0)}k</span>
            </div>
            <div style="background:#21262d;border-radius:3px;height:4px;overflow:hidden">
                <div style="width:${compactPct}%;height:100%;background:${compactColor};transition:width 0.5s"></div>
            </div>
        </div>` : '';

    const badgeClass = isActive ? 'in_progress' : statusLabel;
    const badgeLabel = isActive ? 'Running' : statusLabel;
    const shortId = agent.id ? agent.id.slice(0, 8) : '';

    return `
        <div class="card agent-card" data-project="${escapeHtml(agent.project)}" onclick="showAgentOutput('${agent.id}', '${agent.project}', ${isActive}, '${agent.task_id || ''}')">
            <div class="card-header">
                <span class="project-name">${escapeHtml(agent.project)}</span>
                <span class="agent-short-id">${shortId}</span>
                <span class="status ${badgeClass}">${badgeLabel}</span>
            </div>
            ${descSnippet ? `<div style="font-size:11px;color:#6e7681;margin:4px 0 2px;line-height:1.3">${descSnippet}</div>` : ''}
            <div class="agent-meta-row">
                <div class="agent-stat">Type <strong>${agent.task_type || 'refactor'}</strong></div>
                <div class="agent-stat">Started <strong>${agent.spawned_at ? new Date(agent.spawned_at).toLocaleTimeString() : '—'}</strong></div>
                ${loopHtml}
                ${agent.exit_code !== undefined ? `<div class="agent-stat">Exit <strong>${agent.exit_code}</strong></div>` : ''}
            </div>
            ${totalTokens > 0 ? `<div style="margin-top:6px"><span class="token-badge">&#x1f4b0; ${(totalTokens/1000).toFixed(1)}k tokens</span></div>` : ''}
            ${compactMeterHtml}
            ${diffStat ? `<div class="diff-stat">\u00b1 ${escapeHtml(diffStat)}</div>` : ''}
            ${agent.output ? `<div class="output-log" style="max-height:70px;font-size:10px;overflow:hidden;margin-top:8px">${escapeHtml(agent.output.slice(-300))}</div>` : ''}
        </div>
    `;
}



// Helper: render a parent task with its children (for pending tasks)
function renderTaskWithChildren(task, childrenMap) {
    const taskId = task.id;
    const children = childrenMap[taskId] || [];
    const isParent = children.length > 0;
    
    // Determine indentation based on task_depth
    const depth = (task.metadata && task.metadata.task_depth) || 0;
    const indentPx = depth * 20;
    
    // Check if this task is itself a child (has parent_task_id)
    const isChild = task.metadata && task.metadata.parent_task_id;
    const wrapperClass = isChild ? 'sub-task' : (isParent ? 'parent-task' : '');
    
    // Count active children for badge
    const activeChildCount = children.filter(c => c.status === 'in_progress').length;
    const activeBadge = activeChildCount > 0 ? '<span class="sub-task-count">' + activeChildCount + ' active</span>' : '';
    
    // Build the task card HTML
    const card = createTaskCard(task, false, isParent, activeBadge, isChild);
    
    // Wrap in a container for hierarchy
    if (isParent) {
        const childCards = children.map(child => renderTaskWithChildren(child, childrenMap)).join('');
        return '<div class="' + wrapperClass + '" style="margin-left:' + indentPx + 'px">' +
            card +
            '<div class="children-wrapper" id="children-' + taskId + '">' +
            childCards +
            '</div></div>';
    } else {
        return '<div class="' + wrapperClass + '" style="margin-left:' + indentPx + 'px">' + card + '</div>';
    }
}

// Toggle collapse state for a parent's children
function toggleChildren(taskId) {
    const wrapper = document.getElementById('children-' + taskId);
    if (!wrapper) return;
    const toggle = document.getElementById('toggle-' + taskId);
    if (wrapper.classList.contains('collapsed')) {
        wrapper.classList.remove('collapsed');
        if (toggle) toggle.textContent = '[-] hide';
    } else {
        wrapper.classList.add('collapsed');
        if (toggle) toggle.textContent = '[+] show';
    }
}

function createTaskCard(task, isActive, isParent, activeBadge, isChild) {
    const lines = task.description.match(/(\d+) lines/);
    const lineCount = lines ? parseInt(lines[1]) : 0;
    const progress = isActive ? Math.min(100, Math.floor((maxLines - lineCount) / maxLines * 100)) : 0;
    const step = task.step || 1;
    const attempts = task.attempts > 0 ? ` · ${task.attempts}/${task.max_attempts || 3} attempts` : '';
    const meta = task.metadata || {};
    const deepChain = meta.deep_chain === true;
    const chainDepth = meta.chain_depth || 0;
    const needsHumanReview = meta.needs_human_review === true;
    const deepChainBadge = deepChain ? `<span title="Bug chain is ${chainDepth} levels deep — needs review" style="background:#f85149;color:#fff;font-size:10px;padding:1px 5px;border-radius:3px;margin-left:6px">⚠ chain:${chainDepth}</span>` : '';
    const humanReviewBadge = needsHumanReview ? `<span title="Automatic recovery stopped — requires human intervention (chain depth ${meta.deep_chain_depth || '?'})" style="background:#da3633;color:#fff;font-size:10px;padding:1px 6px;border-radius:3px;margin-left:6px;font-weight:600">🛑 needs review</span>` : '';
    const delegatedMode = meta.delegation_mode || '';
    const delegatedChildren = Array.isArray(meta.delegated_child_task_ids) ? meta.delegated_child_task_ids.length : 0;
    const helperDelegations = Array.isArray(meta.helper_delegations) ? meta.helper_delegations.length : 0;
    const delegationBadge = delegatedMode
        ? `<span title="Delegated ${delegatedChildren} child task(s) via ${escapeHtml(delegatedMode)}" style="background:#13233a;color:#79c0ff;font-size:10px;padding:1px 6px;border-radius:999px;margin-left:6px;border:1px solid #1f6feb">delegated ${escapeHtml(delegatedMode)}</span>`
        : '';
    const helperBadge = helperDelegations
        ? `<span title="${helperDelegations} helper delegation call(s) recorded" style="background:#1b1f24;color:#d2a8ff;font-size:10px;padding:1px 6px;border-radius:999px;margin-left:6px;border:1px solid #6e40c9">helpers ${helperDelegations}</span>`
        : '';
    const isPendingPhaseGate = task.type === 'phase_gate' && task.status === 'pending';
    const taskIdJs = escapeHtml(task.id).replace(/'/g, "\\'");
    const projectJs = escapeHtml(task.project).replace(/'/g, "\\'");

    // Store task JSON in data attribute to avoid quote-escaping issues in onclick
    const taskJson = isActive ? '' : JSON.stringify(task).replace(/'/g, "&#39;");
    const cardAttr = isActive ? '' : `data-task='${taskJson}' onclick="openEditTaskModalFromCard(this)"`;
    return `
        <div class="card ${deepChain ? 'deep-chain-warning' : ''}" data-project="${escapeHtml(task.project)}" ${cardAttr} style="${isActive ? '' : 'cursor:pointer'}">
            <div class="card-header"${isParent ? ` onclick="toggleChildren('${task.id}')"` : ''}>
                <span class="project-name">${isChild ? '<span class="task-id-prefix">&#8618;</span>' : ''}${escapeHtml(task.project)}</span>
                ${isParent ? '<span class="collapse-toggle" id="toggle-' + task.id + '">[-] hide</span>' : ''}
                ${activeBadge}
                <span class="status ${task.status}">${task.status.replace('_', ' ')}</span>${deepChainBadge}${humanReviewBadge}${delegationBadge}${helperBadge}
            </div>
            <div class="task-desc">${escapeHtml(task.description)}</div>
            <div class="stat">Type: <span>${escapeHtml(task.type)}</span> | Priority: <span>${task.priority}${attempts}</span></div>
            ${isActive ? `
                <div class="progress-bar">
                    <div class="fill working" style="width: ${progress}%"></div>
                </div>
            ` : `
                <div style="margin-top:8px;text-align:right;display:flex;gap:6px;justify-content:flex-end">
                    ${isPendingPhaseGate
                        ? `<button onclick="event.stopPropagation();insertTaskBeforeGate('${taskIdJs}')" style="font-size:11px;padding:2px 8px;background:transparent;color:#e3b341;border:1px solid #e3b341;border-radius:4px;cursor:pointer" title="Add a bug/fix task before this gate unlocks">+ Pre-Gate Fix</button>
                           <button onclick="event.stopPropagation();releasePhaseGate('${projectJs}','${taskIdJs}')" style="font-size:11px;padding:2px 8px;background:transparent;color:#58a6ff;border:1px solid #58a6ff;border-radius:4px;cursor:pointer" title="Complete this manual gate and unlock downstream phase tasks">🔓 Release Gate</button>`
                        : `<button onclick="event.stopPropagation();spawnTask('${taskIdJs}')" style="font-size:11px;padding:2px 8px;background:transparent;color:#3fb950;border:1px solid #3fb950;border-radius:4px;cursor:pointer" title="Run this task now">▶ Run</button>`}
                    ${task.status === 'failed' ? `<button onclick="event.stopPropagation();openResetModal('${task.id}','${escapeHtml(task.description.split('\\n')[0]).replace(/'/g,"\\'")}')" style="font-size:11px;padding:2px 8px;background:transparent;color:#e3b341;border:1px solid #e3b341;border-radius:4px;cursor:pointer" title="Reset and recover this task">↺ Reset</button>` : ''}
                    <button onclick="event.stopPropagation();deleteTask('${taskIdJs}')" style="font-size:11px;padding:2px 8px;background:transparent;color:#6e7681;border:1px solid #30363d;border-radius:4px;cursor:pointer" title="Delete task">✕ Delete</button>
                </div>
            `}
        </div>
    `;
}

// Cache for health data to avoid N+1 fetches every refresh
const healthCache = {};
const closureCache = {};
const closureProposalCache = {};

async function fetchProjectHealth(name) {
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(name)}/health`);
        if (res.ok) healthCache[name] = await res.json();
    } catch(e) {}
}

async function fetchProjectClosure(name) {
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(name)}/closure`);
        if (!res.ok) return;
        const data = await res.json();
        if (data && data.closure) closureCache[name] = data.closure;
    } catch(e) {}
}

async function fetchProjectClosureProposal(name) {
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(name)}/closure/proposal`);
        if (!res.ok) return;
        const data = await res.json();
        if (data && data.proposal) closureProposalCache[name] = data.proposal;
    } catch(e) {}
}

let _pausedProjects = new Set();
let _autoReplanProjects = new Set();
let _projectTokenMap = {};
let _activeProjectSet = new Set();
let _selectedProject = null;  // null = all projects
let _allProjectNames = [];
let _integrityData = null;
const PROJECT_SORT_MODES = new Set(['active', 'recent', 'name', 'largest']);
let _projectSortMode = PROJECT_SORT_MODES.has(localStorage.getItem('swarm.projectSortMode'))
    ? localStorage.getItem('swarm.projectSortMode')
    : 'active';
let _recentProjectsOnly = localStorage.getItem('swarm.recentProjectsOnly') === 'true';
let _projectRecentWindowDays = parseInt(localStorage.getItem('swarm.projectRecentWindowDays') || '7', 10);
if (![1, 7, 30].includes(_projectRecentWindowDays)) _projectRecentWindowDays = 7;

function projectLargestFileSize(data) {
    const sizes = Object.values((data && data.files) || {});
    return sizes.length ? Math.max(...sizes) : 0;
}

function sortProjectEntries(entries, projectTaskCount, activityMap) {
    return entries.slice().sort((a, b) => {
        const [aName, aData] = a;
        const [bName, bData] = b;
        const aActive = projectTaskCount[aName] || 0;
        const bActive = projectTaskCount[bName] || 0;

        if (_projectSortMode === 'name') {
            return aName.localeCompare(bName);
        }
        if (_projectSortMode === 'largest') {
            const sizeDiff = projectLargestFileSize(bData) - projectLargestFileSize(aData);
            return sizeDiff || aName.localeCompare(bName);
        }
        if (_projectSortMode === 'recent') {
            const activityDiff = (activityMap[bName] || 0) - (activityMap[aName] || 0);
            return activityDiff || (bActive - aActive) || aName.localeCompare(bName);
        }

        if (bActive !== aActive) return bActive - aActive;
        const activityDiff = (activityMap[bName] || 0) - (activityMap[aName] || 0);
        if (activityDiff) return activityDiff;
        const sizeDiff = projectLargestFileSize(bData) - projectLargestFileSize(aData);
        return sizeDiff || aName.localeCompare(bName);
    });
}

function getVisibleProjectEntries(sortedProjects, activityMap) {
    let entries = _selectedProject
        ? sortedProjects.filter(([name]) => name === _selectedProject)
        : sortedProjects;
    if (!_recentProjectsOnly || _selectedProject) return entries;

    const cutoff = Date.now() - _projectRecentWindowDays * 24 * 60 * 60 * 1000;
    return entries.filter(([name]) =>
        (activityMap[name] || 0) >= cutoff ||
        (_sidebarTaskCounts[name] || 0) > 0
    );
}

function updateProjectSortControls(visibleCount, totalCount) {
    const sortSelect = document.getElementById('projectSortSelect');
    const recentOnly = document.getElementById('projectRecentOnly');
    const recentWindow = document.getElementById('projectRecentWindow');
    const hint = document.getElementById('projectSortHint');

    if (sortSelect) sortSelect.value = _projectSortMode;
    if (recentOnly) recentOnly.checked = _recentProjectsOnly;
    if (recentWindow) recentWindow.value = String(_projectRecentWindowDays);
    if (hint) {
        hint.textContent = _recentProjectsOnly && !_selectedProject
            ? `${visibleCount}/${totalCount} projects shown`
            : `${totalCount} projects`;
    }
}

function setProjectSortMode(value) {
    _projectSortMode = PROJECT_SORT_MODES.has(value) ? value : 'active';
    localStorage.setItem('swarm.projectSortMode', _projectSortMode);
    loadData();
}

function setRecentProjectsOnly(value) {
    _recentProjectsOnly = Boolean(value);
    localStorage.setItem('swarm.recentProjectsOnly', String(_recentProjectsOnly));
    loadData();
}

function setProjectRecentWindow(value) {
    const days = parseInt(value, 10);
    _projectRecentWindowDays = [1, 7, 30].includes(days) ? days : 7;
    localStorage.setItem('swarm.projectRecentWindowDays', String(_projectRecentWindowDays));
    loadData();
}

async function openProjectClosure(event, name) {
    if (event) event.stopPropagation();
    const safeName = String(name || '').replace(/[^a-z0-9_-]/gi, '_');
    let details = document.getElementById(`closure-live-${safeName}`);
    if (!details) {
        try {
            await fetchProjectClosure(name);
            const proposalLoaded = Boolean(closureProposalCache[name]);
            if (!proposalLoaded) {
                await fetchProjectClosureProposal(name);
            }
            await loadData();
            details = document.getElementById(`closure-live-${safeName}`);
        } catch (e) {
            // Fall through to the error toast below.
        }
    }
    if (!details) {
        showToast(`Closure contract not loaded for <strong>${escapeHtml(name)}</strong>`, '#f85149');
        return;
    }
    details.open = true;
    details.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

function createProjectCard(name, data, anyTaskCount, velocity) {
    const files = data.files || {};
    const largest = Object.entries(files).sort((a, b) => b[1] - a[1])[0] || ['-', 0];
    const isOverflow = largest[1] > maxLines;
    const isLocked = data.locked;
    const isPaused = _pausedProjects.has(name);
    const commits = data.recent_commits || [];
    const health = healthCache[name];
    const closure = closureCache[name];

    const commitsHtml = commits.length
        ? commits.map(c => `
            <div class="commit-row">
                <span class="hash">${escapeHtml(c.hash)}</span>
                <span class="message">${escapeHtml(c.message)}</span>
                <span class="age">${escapeHtml(c.age)}</span>
            </div>`).join('')
        : '<div class="commit-row" style="color:#8b949e">No commits yet</div>';

    let healthHtml = '';
    if (health) {
        const score = health.health_score;
        const color = score >= 80 ? '#3fb950' : score >= 50 ? '#f0883e' : '#f85149';
        const age = health.last_commit_age_seconds != null
            ? (health.last_commit_age_seconds < 3600
                ? `${Math.floor(health.last_commit_age_seconds/60)}m ago`
                : `${Math.floor(health.last_commit_age_seconds/3600)}h ago`)
            : '—';
        healthHtml = `
            <div class="health-bar">
                <div class="health-track"><div class="health-fill" style="width:${score}%;background:${color}"></div></div>
                <span class="health-label">${score}/100 · ✓${health.tasks_completed} ✗${health.tasks_failed} · ${age}</span>
            </div>`;
    }

    let velocityHtml = '';
    if (velocity && (velocity.week > 0 || velocity.today > 0)) {
        velocityHtml = `
            <div class="velocity-stats">
                <span title="Completed last 7 days">📈 ${velocity.week} this week</span>
                <span title="Completed today">⚡ ${velocity.today} today</span>
            </div>`;
    }

    const closureProposal = closureProposalCache[name];
    const closureHtml = window.SwarmClosureUI
        ? window.SwarmClosureUI.renderProjectClosureSummary(closure, closureProposal)
        : '';

    const notesId = `notes-${name.replace(/[^a-z0-9]/gi, '_')}`;
    const blurbId = `blurb-${name.replace(/[^a-z0-9]/gi, '_')}`;

    const pauseBtn = `<button onclick="toggleProjectPause(event,'${escapeHtml(name)}')" title="${isPaused ? 'Resume project' : 'Pause project'}"
        style="background:transparent;color:${isPaused ? '#f0883e' : '#8b949e'};border:1px solid ${isPaused ? '#f0883e' : '#30363d'};border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer">
        ${isPaused ? '▶ Resume' : '⏸ Pause'}</button>`;
    const closureBtn = `<button onclick="openProjectClosure(event,'${escapeHtml(name)}')" title="Open closure contract"
        style="background:transparent;color:#58a6ff;border:1px solid #58a6ff;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer">
        📄 Closure</button>`;

    const repairBtns = anyTaskCount > 0 ? `
        <button onclick="repairProject(event,'${escapeHtml(name)}')"
            style="background:transparent;color:#e3b341;border:1px solid #e3b341;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer"
            title="Repair: reset failed/orphaned tasks and resurrect missing dependencies">🔧 Repair</button>
        <button onclick="restartProject(event,'${escapeHtml(name)}')"
            style="background:transparent;color:#f85149;border:1px solid #f85149;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer"
            title="Reset ALL tasks to pending (clears all failure history)">↺ Restart</button>` : '';

    const replanBtn = `<button onclick="replanProject(event,'${escapeHtml(name)}')"
        style="background:transparent;color:#a371f7;border:1px solid #a371f7;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer"
        title="Spawn a project_plan agent to analyse the codebase and generate new tasks">🗺 Re-plan</button>`;

    const isAutoReplan = _autoReplanProjects.has(name);
    const autoReplanBtn = `<button onclick="toggleAutoReplan(event,'${escapeHtml(name)}')"
        style="background:transparent;color:${isAutoReplan ? '#a371f7' : '#8b949e'};border:1px solid ${isAutoReplan ? '#a371f7' : '#30363d'};border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer"
        title="${isAutoReplan ? 'Auto re-plan ON — will spawn new plan when tasks run out' : 'Auto re-plan OFF'}">♻ Auto</button>`;

    const projectTokens = _projectTokenMap[name] || 0;
    const projectTokensHtml = projectTokens > 0
        ? `<span style="font-size:11px;color:#8b949e;margin-left:8px">${(projectTokens/1000).toFixed(1)}k tok</span>`
        : '';

    const isActive = _activeProjectSet.has(name);
    const activeLed = isActive ? '<span class="active-led" title="Agent running"></span>' : '';

    return `
        <div class="card" data-has-project="${escapeHtml(name)}" style="${isPaused ? 'opacity:0.6' : ''}">
            <div class="card-header">
                <span class="project-name">
                    ${activeLed}${isLocked ? '<span class="locked-badge"></span>' : ''}${escapeHtml(name)}
                    ${isPaused ? '<span style="font-size:10px;color:#f0883e;margin-left:6px">PAUSED</span>' : ''}
                    ${projectTokensHtml}
                </span>
                <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap">
                    ${closureBtn}
                    ${pauseBtn}
                    ${repairBtns}
                    ${replanBtn}
                    ${autoReplanBtn}
                    <span class="status ${data.status}" style="margin-left:4px">${data.status}</span>
                </div>
            </div>
            <div id="${blurbId}" style="font-size:11px;color:#8b949e;margin:2px 0 6px 0;font-style:italic;min-height:14px"></div>
            <div class="stat">Largest: <span>${escapeHtml(largest[0])}</span>
                <span style="color: ${isOverflow ? '#f0883e' : '#3fb950'}; margin-left:6px">${largest[1]} lines ${isOverflow ? '⚠️' : '✓'}</span>
            </div>
            ${healthHtml}
            ${closureHtml}
            ${velocityHtml}
            <div class="commit-list">${commitsHtml}</div>
            <details style="margin-top:8px">
                <summary style="font-size:11px;color:#8b949e;cursor:pointer;user-select:none">Project notes (prepended to agent prompts)</summary>
                <textarea id="${notesId}" rows="3"
                    style="width:100%;margin-top:6px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:6px;font-size:12px;resize:vertical;box-sizing:border-box"
                    placeholder="Add context for agents working on this project…"
                    onblur="saveProjectNotes('${escapeHtml(name)}', this.value)"
                ></textarea>
            </details>
        </div>
    `;
}

async function loadProjectNotes(name, elementId) {
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(name)}/notes`);
        if (res.ok) {
            const data = await res.json();
            const el = document.getElementById(elementId);
            if (el) el.value = data.notes || '';
            // Populate blurb: prefer first line of notes, fall back to GAME_DESIGN.md concept
            const blurbEl = document.getElementById(`blurb-${name.replace(/[^a-z0-9]/gi, '_')}`);
            if (blurbEl) {
                let blurb = '';
                if (data.notes) {
                    const lines = data.notes.split('\n').map(l => l.trim()).filter(l => l && !l.startsWith('#') && !l.startsWith('-'));
                    blurb = lines[0] || '';
                }
                if (!blurb && data.concept) {
                    blurb = data.concept;
                }
                blurbEl.textContent = blurb.length > 150 ? blurb.slice(0, 147) + '…' : blurb;
            }
        }
    } catch(e) {}
}

async function saveProjectNotes(name, notes) {
    try {
        await fetch(`${API}/api/projects/${encodeURIComponent(name)}/notes`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({notes}),
        });
    } catch(e) {}
}


let _activeStream = null;

function closeModal() {
    document.getElementById('outputModal').classList.remove('active');
    if (_activeStream) { _activeStream.close(); _activeStream = null; }
}

// ---- Add Task modal ----
let _projectList = {};
let _editTaskId = null;

function openEditTaskModalFromCard(el) {
    openEditTaskModal(JSON.parse(el.dataset.task));
}

function openEditTaskModal(task) {
    _editTaskId = task.id;
    document.getElementById('editTaskType').value = task.type || 'feature';
    document.getElementById('editTaskPriority').value = task.priority || 50;
    document.getElementById('editTaskDesc').value = task.description || '';
    document.getElementById('editTaskDeps').value = (task.dependencies || []).join('\n');
    document.getElementById('editTaskError').style.display = 'none';
    const delegationEl = document.getElementById('editTaskDelegation');
    delegationEl.style.display = 'none';
    delegationEl.innerHTML = '';
    document.getElementById('editTaskModal').classList.add('active');
    document.getElementById('editTaskDesc').focus();
    loadTaskDelegationSummary(task.id);
}

function closeEditTaskModal() {
    document.getElementById('editTaskModal').classList.remove('active');
    _editTaskId = null;
}

async function loadTaskDelegationSummary(taskId) {
    const el = document.getElementById('editTaskDelegation');
    if (!el) return;
    try {
        const res = await fetch(`${API}/api/tasks/${taskId}/delegation`);
        if (!res.ok) return;
        const data = await res.json();
        const d = data.delegation || {};
        const hasDelegation = d.batch_id || (d.helper_activity_count || 0) > 0;
        if (!hasDelegation) {
            el.style.display = 'none';
            el.innerHTML = '';
            return;
        }
        const childRows = (d.children || []).slice(0, 6).map(child => {
            const meta = child.metadata || {};
            const files = Array.isArray(meta.delegated_files) ? meta.delegated_files.slice(0, 3).join(', ') : '';
            return `<div style="font-size:12px;color:#8b949e;line-height:1.5">
                <span style="color:#e6edf3">${escapeHtml(child.id)}</span>
                <span style="color:#6e7681">· ${escapeHtml(child.status || 'pending')}</span>
                ${files ? `<div style="color:#6e7681">${escapeHtml(files)}</div>` : ''}
            </div>`;
        }).join('');
        const helperRows = (d.helper_activity || []).slice(-3).reverse().map(entry => `
            <div style="font-size:12px;color:#8b949e;line-height:1.5">
                <span style="color:#e6edf3">${escapeHtml(entry.question || '')}</span>
                ${(entry.files || []).length ? `<div style="color:#6e7681">${escapeHtml((entry.files || []).join(', '))}</div>` : ''}
            </div>
        `).join('');
        el.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <div style="font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:.08em">Delegation</div>
                ${d.mode ? `<span style="font-size:11px;color:#58a6ff;border:1px solid #1f6feb;border-radius:999px;padding:2px 8px">${escapeHtml(d.mode)}</span>` : ''}
            </div>
            ${d.batch_id ? `<div style="font-size:12px;color:#6e7681;margin-bottom:8px">Batch ${escapeHtml(d.batch_id)} · ${d.child_count || 0} child task(s)</div>` : ''}
            ${d.successor_task_id ? `<div style="font-size:12px;color:#8b949e;margin-bottom:8px">Successor: <span style="color:#e6edf3">${escapeHtml(d.successor_task_id)}</span>${d.successor_kind ? ` <span style="color:#6e7681">(${escapeHtml(d.successor_kind)})</span>` : ''}</div>` : ''}
            ${childRows ? `<div style="display:flex;flex-direction:column;gap:6px;margin-bottom:${helperRows ? '10px' : '0'}">${childRows}</div>` : ''}
            ${helperRows ? `<div style="padding-top:10px;border-top:1px solid #21262d;display:flex;flex-direction:column;gap:6px">
                <div style="font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:.08em">Helper Activity</div>
                ${helperRows}
            </div>` : ''}
        `;
        el.style.display = 'block';
    } catch (e) {
        el.style.display = 'none';
        el.innerHTML = '';
    }
}

async function submitEditTask() {
    const type     = document.getElementById('editTaskType').value;
    const priority = parseInt(document.getElementById('editTaskPriority').value) || 50;
    const desc     = document.getElementById('editTaskDesc').value.trim();
    const depsRaw  = document.getElementById('editTaskDeps').value.trim();
    const deps     = depsRaw ? depsRaw.split('\n').map(s => s.trim()).filter(Boolean) : [];
    const errEl    = document.getElementById('editTaskError');

    if (!desc) { errEl.textContent = 'Description is required.'; errEl.style.display = 'block'; return; }
    errEl.style.display = 'none';

    try {
        const res = await fetch(`${API}/api/tasks/${_editTaskId}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({type, priority, description: desc, dependencies: deps}),
        });
        const data = await res.json();
        if (data.error) { errEl.textContent = data.error; errEl.style.display = 'block'; return; }
        closeEditTaskModal();
        loadData();
    } catch(e) {
        errEl.textContent = 'Request failed: ' + e.message;
        errEl.style.display = 'block';
    }
}

async function deleteTaskFromModal() {
    if (!confirm('Delete this task?')) return;
    await fetch(`${API}/api/tasks/${_editTaskId}`, {method: 'DELETE'});
    closeEditTaskModal();
    loadData();
}

let _addTaskSelectedDeps = new Set();

function openAddTaskModal() {
    const sel = document.getElementById('addTaskProject');
    const hint = document.getElementById('addTaskProjectHint');
    const projects = Object.keys(_projectList).sort();
    const orderedProjects = (_selectedProject && projects.includes(_selectedProject))
        ? [_selectedProject, ...projects.filter(p => p !== _selectedProject)]
        : projects;

    sel.innerHTML = orderedProjects.map(p =>
        `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`
    ).join('');
    if (_selectedProject && projects.includes(_selectedProject)) {
        sel.value = _selectedProject;
        hint.textContent = `Using the currently selected project: ${_selectedProject}`;
        hint.style.display = 'block';
    } else {
        hint.textContent = '';
        hint.style.display = 'none';
    }
    document.getElementById('addTaskDesc').value = '';
    document.getElementById('addTaskError').style.display = 'none';
    _addTaskSelectedDeps = new Set();
    _refreshAddTaskDepChips();
    document.getElementById('addTaskModal').classList.add('active');
    document.getElementById('addTaskDesc').focus();
}

function _refreshAddTaskDepChips() {
    const project = document.getElementById('addTaskProject').value;
    const section = document.getElementById('addTaskDepsSection');
    const container = document.getElementById('addTaskDepChips');
    const tasks = Object.values(_taskDataMap).filter(t =>
        t.project === project && t.status !== 'completed'
    ).sort((a, b) => (b.priority || 50) - (a.priority || 50));

    if (!tasks.length) { section.style.display = 'none'; return; }
    section.style.display = 'block';

    const typeColors = {feature:'#1f6feb', bug:'#da3633', refactor:'#6e40c9', polish:'#d29922', qa:'#238636'};
    container.innerHTML = tasks.map(t => {
        const selected = _addTaskSelectedDeps.has(t.id);
        const color = typeColors[t.type] || '#30363d';
        const desc = t.description.split('\n')[0].slice(0, 55) + (t.description.length > 55 ? '…' : '');
        return `<div onclick="_toggleAddTaskDep('${t.id}')" title="${escapeHtml(t.description)}" style="
            cursor:pointer;padding:4px 8px;border-radius:4px;font-size:11px;line-height:1.4;max-width:200px;
            border:1.5px solid ${selected ? color : '#30363d'};
            background:${selected ? color + '22' : '#0d1117'};
            color:${selected ? '#e6edf3' : '#8b949e'};
            transition:all 0.15s">
            <span style="color:${color};font-weight:600;margin-right:4px">${t.type}</span>${escapeHtml(desc)}
        </div>`;
    }).join('');

    const count = _addTaskSelectedDeps.size;
    document.getElementById('addTaskDepsCount').textContent = count ? `(${count} selected)` : '';
}

function _toggleAddTaskDep(taskId) {
    if (_addTaskSelectedDeps.has(taskId)) _addTaskSelectedDeps.delete(taskId);
    else _addTaskSelectedDeps.add(taskId);
    _refreshAddTaskDepChips();
}

function closeAddTaskModal() {
    document.getElementById('addTaskModal').classList.remove('active');
}

async function submitAddTask() {
    const project  = document.getElementById('addTaskProject').value;
    const type     = document.getElementById('addTaskType').value;
    const priority = parseInt(document.getElementById('addTaskPriority').value) || 50;
    const desc     = document.getElementById('addTaskDesc').value.trim();
    const errEl    = document.getElementById('addTaskError');

    if (!desc) { errEl.textContent = 'Description is required.'; errEl.style.display = 'block'; return; }
    errEl.style.display = 'none';

    try {
        const res = await fetch(`${API}/api/tasks`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({project, type, description: desc, priority, dependencies: [..._addTaskSelectedDeps]}),
        });
        const data = await res.json();
        if (data.error) { errEl.textContent = data.error; errEl.style.display = 'block'; return; }
        closeAddTaskModal();
        loadData();
    } catch(e) {
        errEl.textContent = 'Request failed: ' + e.message;
        errEl.style.display = 'block';
    }
}

// ---- Append Phase modal ----
function openAppendPhaseModal() {
    const sel = document.getElementById('appendPhaseProject');
    const projects = Object.keys(_projectList).sort();
    const orderedProjects = (_selectedProject && projects.includes(_selectedProject))
        ? [_selectedProject, ...projects.filter(p => p !== _selectedProject)]
        : projects;

    sel.innerHTML = orderedProjects.map(p =>
        `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`
    ).join('');
    if (_selectedProject && projects.includes(_selectedProject)) sel.value = _selectedProject;

    document.getElementById('appendPhaseName').value = '';
    document.getElementById('appendPhaseAnchor').value = '';
    document.getElementById('appendPhaseOverview').value = '';
    document.getElementById('appendPhaseTasks').value = '';
    document.getElementById('appendPhaseQa').checked = true;
    document.getElementById('appendPhaseGate').checked = true;
    document.getElementById('appendPhaseError').style.display = 'none';
    document.getElementById('appendPhaseResult').style.display = 'none';
    document.getElementById('appendPhaseSubmitBtn').disabled = false;
    document.getElementById('appendPhaseSubmitBtn').textContent = 'Append Phase';
    refreshAppendPhaseAnchorHints();
    document.getElementById('appendPhaseModal').classList.add('active');
    document.getElementById('appendPhaseName').focus();
}

function closeAppendPhaseModal() {
    document.getElementById('appendPhaseModal').classList.remove('active');
}

function refreshAppendPhaseAnchorHints() {
    const project = document.getElementById('appendPhaseProject').value;
    const datalist = document.getElementById('appendPhaseAnchorOptions');
    const hint = document.getElementById('appendPhaseAnchorHint');
    const tasks = Object.values(_taskDataMap)
        .filter(t => t.project === project)
        .sort((a, b) => String(a.id).localeCompare(String(b.id)));

    datalist.innerHTML = tasks.slice(0, 300).map(t =>
        `<option value="${escapeHtml(t.id)}">${escapeHtml((t.description || '').split('\n')[0].slice(0, 80))}</option>`
    ).join('');
    hint.textContent = tasks.length
        ? `${tasks.length} active task IDs available. Leave blank to append after the stored project head.`
        : 'No active task IDs loaded for this project. You can still paste a completed task ID manually, or leave blank to use the stored project head.';
}

function parseAppendPhasePayload() {
    const raw = document.getElementById('appendPhaseTasks').value.trim();
    if (!raw) throw new Error('Phase tasks JSON is required.');
    let parsed;
    try {
        parsed = JSON.parse(raw);
    } catch (e) {
        throw new Error('Phase tasks must be valid JSON. Paste an array of tasks or an object with a tasks array.');
    }

    const payload = Array.isArray(parsed) ? {tasks: parsed} : parsed;
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        throw new Error('Phase payload must be a JSON array or object.');
    }
    if (!Array.isArray(payload.tasks) || payload.tasks.length === 0) {
        throw new Error('Phase payload must include at least one task in a tasks array.');
    }

    payload.tasks = payload.tasks.map((task, idx) => {
        if (!task || typeof task !== 'object' || Array.isArray(task)) {
            throw new Error(`Task ${idx + 1} must be a JSON object.`);
        }
        const normalized = {...task};
        if (normalized.depends_on && !normalized.dependencies) normalized.dependencies = normalized.depends_on;
        if (!Array.isArray(normalized.dependencies)) normalized.dependencies = [];
        normalized.dependencies = normalized.dependencies.map(String).filter(Boolean);
        if (!normalized.description) {
            const label = normalized.title || normalized.name || normalized.id || `Phase task ${idx + 1}`;
            normalized.description = String(label);
        }
        return normalized;
    });

    return payload;
}

async function submitAppendPhase() {
    const project = document.getElementById('appendPhaseProject').value;
    const phaseName = document.getElementById('appendPhaseName').value.trim();
    const anchor = document.getElementById('appendPhaseAnchor').value.trim();
    const overview = document.getElementById('appendPhaseOverview').value.trim();
    const errEl = document.getElementById('appendPhaseError');
    const resultEl = document.getElementById('appendPhaseResult');
    const btn = document.getElementById('appendPhaseSubmitBtn');

    errEl.style.display = 'none';
    resultEl.style.display = 'none';
    if (!project) { errEl.textContent = 'Choose a project.'; errEl.style.display = 'block'; return; }
    if (!phaseName) { errEl.textContent = 'Phase name is required.'; errEl.style.display = 'block'; return; }

    let payload;
    try {
        payload = parseAppendPhasePayload();
    } catch (e) {
        errEl.textContent = e.message;
        errEl.style.display = 'block';
        return;
    }

    payload.phase_name = phaseName;
    payload.overview = overview || payload.overview || '';
    payload.qa_before_phase = document.getElementById('appendPhaseQa').checked;
    payload.phase_gate = document.getElementById('appendPhaseGate').checked;
    if (anchor) payload.anchor_task_id = anchor;

    btn.disabled = true;
    btn.textContent = 'Appending...';
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(project)}/append-phase`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok || data.error) {
            const details = Array.isArray(data.details) && data.details.length
                ? '\n- ' + data.details.join('\n- ')
                : '';
            throw new Error((data.error || `HTTP ${res.status}`) + details);
        }
        resultEl.textContent = `Created ${data.created} task(s).\nAnchor: ${data.anchor_task_id}\nHead: ${data.head_task_id}`
            + (data.phase_qa_id ? `\nQA: ${data.phase_qa_id}` : '')
            + (data.phase_gate_id ? `\nGate: ${data.phase_gate_id}` : '');
        resultEl.style.display = 'block';
        showToast(`Appended <strong>${escapeHtml(phaseName)}</strong> to <strong>${escapeHtml(project)}</strong>`, '#3fb950');
        loadData();
    } catch (e) {
        errEl.textContent = e.message;
        errEl.style.display = 'block';
    }
    btn.disabled = false;
    btn.textContent = 'Append Phase';
}

async function deleteTask(taskId) {
    if (!confirm('Delete this task?')) return;
    await fetch(`${API}/api/tasks/${taskId}`, {method: 'DELETE'});
    loadData();
}

async function releasePhaseGate(project, gateId) {
    if (!confirm(`Release phase gate ${gateId}?\n\nThis will unlock downstream phase tasks for ${project}.`)) return;
    try {
        const res = await fetch(`${API}/api/projects/${encodeURIComponent(project)}/phase-gates/${encodeURIComponent(gateId)}/release`, {
            method: 'POST',
        });
        const data = await res.json();
        if (!res.ok || data.error) {
            showToast(`Release failed: ${escapeHtml(data.error || `HTTP ${res.status}`)}`, '#f85149');
            return;
        }
        showToast(`Released gate <strong>${escapeHtml(gateId)}</strong>`, '#3fb950');
        loadData();
    } catch (e) {
        showToast(`Release failed: ${escapeHtml(e.message)}`, '#f85149');
    }
}

let _preGateTargetId = null;

function insertTaskBeforeGate(gateId) {
    _preGateTargetId = gateId;
    document.getElementById('preGateLabel').textContent = gateId;
    document.getElementById('preGateType').value = 'bug';
    document.getElementById('preGatePriority').value = '95';
    document.getElementById('preGateDesc').value = 'Fix the blocking issue reported by phase QA before releasing the next phase.';
    document.getElementById('preGateError').style.display = 'none';
    document.getElementById('preGateModal').classList.add('active');
    document.getElementById('preGateDesc').focus();
}

function closePreGateModal() {
    document.getElementById('preGateModal').classList.remove('active');
    _preGateTargetId = null;
}

async function submitPreGateTask() {
    const gateId = _preGateTargetId;
    if (!gateId) return;
    const type = document.getElementById('preGateType').value;
    const priority = parseInt(document.getElementById('preGatePriority').value, 10) || 95;
    const description = document.getElementById('preGateDesc').value.trim();
    const errEl = document.getElementById('preGateError');
    if (!description) {
        errEl.textContent = 'Description is required.';
        errEl.style.display = 'block';
        return;
    }
    const btn = document.getElementById('preGateSubmitBtn');
    btn.disabled = true;
    btn.textContent = 'Inserting…';
    try {
        const res = await fetch(`${API}/api/tasks/${encodeURIComponent(gateId)}/insert-before-gate`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ type, priority, description }),
        });
        const data = await res.json();
        if (!res.ok || data.error) {
            errEl.textContent = data.error || `HTTP ${res.status}`;
            errEl.style.display = 'block';
            return;
        }
        closePreGateModal();
        showToast(`Inserted <strong>${escapeHtml(data.inserted_task.id)}</strong> (${escapeHtml(type)}) before gate`, '#3fb950');
        loadData();
    } catch (e) {
        errEl.textContent = e.message;
        errEl.style.display = 'block';
    } finally {
        btn.disabled = false;
        btn.textContent = '+ Insert Task';
    }
}

let _requeueAgentId = null;
function openRequeueModal(agentId, taskId, project) {
    _requeueAgentId = agentId;
    document.getElementById('requeueTaskLabel').textContent = taskId || `${project} (unknown task)`;
    document.getElementById('requeueNoteInput').value = '';
    document.getElementById('requeueSubmitBtn').textContent = '↺ Re-queue';
    document.getElementById('requeueSubmitBtn').disabled = false;
    document.getElementById('requeueModal').classList.add('active');
}
function closeRequeueModal() {
    document.getElementById('requeueModal').classList.remove('active');
    _requeueAgentId = null;
}
async function submitRequeue() {
    if (!_requeueAgentId) return;
    const btn = document.getElementById('requeueSubmitBtn');
    btn.disabled = true;
    btn.textContent = 'Re-queuing…';
    const note = document.getElementById('requeueNoteInput').value.trim();
    const body = note ? {note} : {};
    try {
        const res = await fetch(`${API}/api/history/${_requeueAgentId}/requeue`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.success) {
            closeRequeueModal();
            loadData();
        } else {
            btn.textContent = 'Error: ' + (data.error || 'unknown');
            btn.disabled = false;
        }
    } catch(e) {
        btn.textContent = 'Network error';
        btn.disabled = false;
    }
}

let _resetTaskId = null;
function openResetModal(taskId, title) {
    _resetTaskId = taskId;
    document.getElementById('resetTaskTitle').textContent = title;
    document.getElementById('resetNoteInput').value = '';
    document.getElementById('resetMaxAttempts').value = '';
    document.getElementById('resetAttemptsCheck').checked = true;
    document.getElementById('resetSubmitBtn').textContent = '↺ Reset Task';
    document.getElementById('resetSubmitBtn').disabled = false;
    document.getElementById('resetTaskModal').classList.add('active');
}
function closeResetModal() {
    document.getElementById('resetTaskModal').classList.remove('active');
    _resetTaskId = null;
}
async function submitReset() {
    if (!_resetTaskId) return;
    const btn = document.getElementById('resetSubmitBtn');
    btn.disabled = true;
    btn.textContent = 'Resetting…';
    const note = document.getElementById('resetNoteInput').value.trim();
    const maxAttempts = document.getElementById('resetMaxAttempts').value;
    const body = {
        reset_attempts: document.getElementById('resetAttemptsCheck').checked,
    };
    if (note) body.note = note;
    if (maxAttempts) body.max_attempts = parseInt(maxAttempts);
    try {
        const res = await fetch(`${API}/api/tasks/${_resetTaskId}/reset`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.success) {
            closeResetModal();
            loadData();
        } else {
            btn.textContent = 'Error: ' + (data.error || 'unknown');
            btn.disabled = false;
        }
    } catch(e) {
        btn.textContent = 'Network error';
        btn.disabled = false;
    }
}

async function spawnTask(taskId) {
    const btn = event.target;
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const res = await fetch(`${API}/api/spawn`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({task_id: taskId})
        });
        const data = await res.json();
        if (data.success) {
            btn.textContent = '✓';
            btn.style.color = '#3fb950';
            setTimeout(() => loadData(), 800);
        } else {
            btn.textContent = '✕';
            btn.style.color = '#f85149';
            alert(data.error || 'Failed to spawn');
            btn.disabled = false;
        }
    } catch(e) {
        btn.textContent = '✕';
        btn.disabled = false;
    }
}

function exportTasks() {
    window.open(`${API}/api/tasks/export`, '_blank');
}

async function importTasks(input) {
    const file = input.files[0];
    if (!file) return;
    const text = await file.text();
    const isYaml = file.name.endsWith('.yaml') || file.name.endsWith('.yml');
    const res = await fetch(`${API}/api/tasks/import`, {
        method: 'POST',
        headers: {'Content-Type': isYaml ? 'text/yaml' : 'application/json'},
        body: text,
    });
    const data = await res.json();
    if (data.error) { alert('Import failed: ' + data.error); return; }
    input.value = '';
    alert(`Imported ${data.imported} task(s).`);
    loadData();
}

let _modalAgentId = null;
let _taskDescMap = {};

async function sendHintFromModal() {
    const input = document.getElementById('modalHintInput');
    const btn = document.getElementById('modalHintBtn');
    const message = input.value.trim();
    if (!message) return;
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const res = await fetch(`${API}/api/agents/${_modalAgentId}/hint`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message})
        });
        const data = await res.json();
        if (data.success) {
            input.value = '';
            btn.textContent = '✓ Sent';
            setTimeout(() => { btn.textContent = '↩ Send Hint'; btn.disabled = false; }, 1500);
        } else {
            btn.textContent = '✗ ' + (data.error || 'Failed');
            setTimeout(() => { btn.textContent = '↩ Send Hint'; btn.disabled = false; }, 2000);
        }
    } catch(e) {
        btn.textContent = '✗ Error';
        setTimeout(() => { btn.textContent = '↩ Send Hint'; btn.disabled = false; }, 2000);
    }
}

async function killAgentFromModal() {
    const btn = document.getElementById('modalKillBtn');
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const res = await fetch(`${API}/api/agents/${_modalAgentId}/kill`, {method: 'POST'});
        const data = await res.json();
        if (data.success) {
            btn.textContent = '✓ Killed';
            btn.style.color = '#8b949e';
            btn.style.borderColor = '#8b949e';
            setTimeout(() => { closeModal(); loadData(); }, 800);
        } else {
            btn.textContent = '✗ Failed';
            btn.disabled = false;
        }
    } catch(e) {
        btn.textContent = '✗ Error';
        btn.disabled = false;
    }
}

async function showAgentOutput(agentId, projectName, isActive, taskId) {
    _modalAgentId = agentId;
    document.getElementById('modalTitle').textContent = projectName + ' - Agent Output' + (isActive ? ' (live)' : '');
    const descEl = document.getElementById('modalTaskDesc');
    const fullDesc = (taskId && _taskDescMap[taskId]) || '';
    if (fullDesc) {
        descEl.textContent = fullDesc.slice(0, 400) + (fullDesc.length > 400 ? '…' : '');
        descEl.style.display = 'block';
    } else {
        descEl.style.display = 'none';
    }
    const killBtn = document.getElementById('modalKillBtn');
    killBtn.style.display = isActive ? 'inline-block' : 'none';
    killBtn.disabled = false;
    killBtn.textContent = '✕ Kill Agent';
    killBtn.style.color = '#f85149';
    killBtn.style.borderColor = '#f85149';
    const hintInput = document.getElementById('modalHintInput');
    const hintBtn = document.getElementById('modalHintBtn');
    hintInput.style.display = isActive ? 'block' : 'none';
    hintBtn.style.display = isActive ? 'inline-block' : 'none';
    hintInput.value = '';
    const logEl = document.getElementById('modalOutput');
    logEl.textContent = 'Loading...';
    document.getElementById('outputModal').classList.add('active');

    // Close any previous stream
    if (_activeStream) { _activeStream.close(); _activeStream = null; }

    if (isActive) {
        // Stream via SSE (#9)
        logEl.textContent = '';
        const es = new EventSource(`${API}/api/agents/${agentId}/stream`);
        _activeStream = es;
        es.onmessage = (ev) => {
            logEl.textContent += ev.data + '\n';
            logEl.scrollTop = logEl.scrollHeight;
        };
        es.addEventListener('done', () => {
            es.close();
            _activeStream = null;
            document.getElementById('modalTitle').textContent = projectName + ' - Agent Output (done)';
        });
        es.onerror = () => { es.close(); _activeStream = null; };
    } else {
        // Static load for finished agents
        try {
            const res = await fetch(API + '/api/agent/' + agentId + '/output');
            const data = await res.json();
            logEl.textContent = data.output || 'No output yet';
            logEl.scrollTop = logEl.scrollHeight;
        } catch (e) {
            logEl.textContent = 'Error: ' + e;
        }
    }
}

async function _spawnBatch(count) {
    const status = document.getElementById('spawnStatus');
    status.textContent = 'Spawning...';
    status.style.color = '';
    try {
        const body = count != null ? {count} : {};
        const res = await fetch(API + '/api/spawn-batch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        const data = await res.json();
        const n = data.count || 0;
        const sk = (data.skipped || []).length;
        status.textContent = n > 0
            ? `✓ ${n} spawned${sk ? ', ' + sk + ' skipped' : ''}`
            : 'Nothing to spawn';
        status.style.color = n > 0 ? '#3fb950' : '#8b949e';
        if (n > 0) loadData();
    } catch (e) {
        status.textContent = 'Error: ' + e.message;
        status.style.color = '#f85149';
    }
    setTimeout(() => { status.textContent = ''; status.style.color = ''; }, 4000);
}

async function spawnAgents() {
    const count = parseInt(document.getElementById('spawnCount').value) || 1;
    document.getElementById('spawnBtn').disabled = true;
    await _spawnBatch(count);
    document.getElementById('spawnBtn').disabled = false;
}

async function spawnAll() {
    await _spawnBatch(null);
}

let autoEnabled = false;
let autoSuspended = false;
async function toggleAuto() {
    autoEnabled = !autoEnabled;
    const btn = document.getElementById('autoBtn');
    btn.disabled = true;
    try {
        const res = await fetch(API + '/api/auto-mode', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: autoEnabled})
        });
        const data = await res.json();
        autoEnabled = data.enabled;
        autoSuspended = data.suspended_for_quota || false;
        updateAutoBtn();
        if (autoEnabled && data.spawned > 0) loadData();
    } catch(e) {
        autoEnabled = !autoEnabled;
    }
    btn.disabled = false;
}

function updateAutoBtn() {
    const btn = document.getElementById('autoBtn');
    if (autoEnabled) {
        btn.textContent = '⟳ Auto: On';
        btn.style.background = '#1a4a1a';
        btn.style.color = '#3fb950';
        btn.style.borderColor = '#3fb950';
    } else if (autoSuspended) {
        btn.textContent = '⟳ Auto: Paused (quota)';
        btn.style.background = '#2d1f00';
        btn.style.color = '#f0883e';
        btn.style.borderColor = '#f0883e';
    } else {
        btn.textContent = '⟳ Auto: Off';
        btn.style.background = '#30363d';
        btn.style.color = '#58a6ff';
        btn.style.borderColor = '#58a6ff';
    }
}

async function syncAutoMode() {
    try {
        const res = await fetch(API + '/api/auto-mode');
        const data = await res.json();
        autoEnabled = data.enabled;
        autoSuspended = data.suspended_for_quota || false;
        updateAutoBtn();
    } catch(e) {}
}

let autoScaleEnabled = false;

function updateAutoScaleBtn() {
    const btn = document.getElementById('autoScaleBtn');
    if (!btn) return;
    if (autoScaleEnabled) {
        btn.textContent = '⚖ Scale: On';
        btn.style.background = '#1a3a1f';
        btn.style.color = '#3fb950';
        btn.style.borderColor = '#3fb950';
    } else {
        btn.textContent = '⚖ Scale: Off';
        btn.style.background = '#30363d';
        btn.style.color = '#3fb950';
        btn.style.borderColor = '#3fb950';
    }
}

async function toggleAutoScale() {
    autoScaleEnabled = !autoScaleEnabled;
    const btn = document.getElementById('autoScaleBtn');
    btn.disabled = true;
    try {
        const res = await fetch(API + '/api/auto-scale', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: autoScaleEnabled})
        });
        const data = await res.json();
        autoScaleEnabled = data.enabled;
        updateAutoScaleBtn();
        if (data.enabled) {
            document.getElementById('spawnStatus').textContent =
                `Auto-scale on — ceiling: ${data.ceiling}, current: ${data.current}`;
        }
    } catch(e) {
        autoScaleEnabled = !autoScaleEnabled;
    }
    btn.disabled = false;
}

async function syncAutoScale() {
    try {
        const res = await fetch(API + '/api/auto-scale');
        const data = await res.json();
        autoScaleEnabled = data.enabled;
        updateAutoScaleBtn();
    } catch(e) {}
}

async function loadRlHistory() {
    try {
        const res = await fetch(API + '/api/auto-scale/history');
        if (!res.ok) return;
        const data = await res.json();
        const byHour = data.by_hour || [];
        const total = data.total || 0;

        const totalEl = document.getElementById('rlHistoryTotal');
        if (totalEl) totalEl.textContent = total > 0 ? `${total.toLocaleString()} total events` : 'no data yet';

        const canvas = document.getElementById('rlHistoryChart');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.getBoundingClientRect();
        canvas.width = rect.width * dpr;
        canvas.height = 48 * dpr;
        ctx.scale(dpr, dpr);

        const W = rect.width, H = 48;
        const counts = byHour.map(b => b.count);
        const maxCount = Math.max(...counts, 1);
        const barW = W / 24;

        ctx.clearRect(0, 0, W, H);

        // Current hour highlight
        const currentHour = new Date().getHours();

        counts.forEach((count, h) => {
            const barH = count > 0 ? Math.max(2, (count / maxCount) * (H - 8)) : 0;
            const x = h * barW;
            const y = H - barH;
            const alpha = h === currentHour ? 1.0 : 0.7;
            const intensity = count > 0 ? Math.max(0.3, count / maxCount) : 0.08;
            ctx.fillStyle = count > 0
                ? `rgba(248, 81, 73, ${intensity * alpha})`
                : `rgba(33, 38, 45, 0.6)`;
            ctx.fillRect(x + 1, y, barW - 2, barH);
        });

        // Hour axis labels (0, 6, 12, 18, 23)
        ctx.fillStyle = '#484f58';
        ctx.font = '9px monospace';
        ctx.textAlign = 'center';
        [0, 6, 12, 18, 23].forEach(h => {
            ctx.fillText(h, h * barW + barW / 2, H - 1);
        });

        // Tooltip on hover
        const tooltip = document.getElementById('rlHistoryTooltip');
        canvas.onmousemove = (e) => {
            const x = e.offsetX;
            const h = Math.min(23, Math.floor(x / barW));
            const c = counts[h];
            const label = h === 0 ? '12am' : h < 12 ? `${h}am` : h === 12 ? '12pm' : `${h-12}pm`;
            if (tooltip) tooltip.textContent = `${label}: ${c} event${c !== 1 ? 's' : ''}`;
        };
        canvas.onmouseleave = () => { if (tooltip) tooltip.textContent = ''; };
    } catch(e) {}
}

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

if (window.SwarmDepsIntegrityUI) {
    window.SwarmDepsIntegrityUI.setContextProvider(() => ({
        API,
        selectedProject: _selectedProject,
        isDepsVisible: () => _depsVisible,
        escapeHtml,
        showToast,
        loadData,
        taskDataMap: () => _taskDataMap,
        agentByTaskId: () => _agentByTaskId,
        openEditTaskModal,
        showAgentOutput,
    }));
    _depHistoryLimit = window.SwarmDepsIntegrityUI.getDepHistoryLimit();
}

loadData().then(() => { if (_depsVisible) renderDepsGraph(); });
// Click outside modals to close
document.getElementById('outputModal').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeModal(); });
document.getElementById('editTaskModal').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeEditTaskModal(); });
syncAutoMode();
syncAutoScale();
loadRlHistory();
syncMaxAgents();
syncQaMaxCycles();
syncProviders();
syncStrategy();
loadThinking();
loadAutoReplan();
loadMetrics();
loadWebhook();
syncVisionProviders();
loadPausedProjects();
setInterval(() => { loadData().then(() => { if (_depsVisible) renderDepsGraph(); }); }, 5000);
setInterval(syncAutoMode, 5000);
setInterval(syncProviders, 30000);
setInterval(loadMetrics, 30000);
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
    return value === 'python' ? 'python' : 'godot';
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

    btn.disabled = true;
    btn.textContent = count === 1 ? '⏳ Working…' : `⏳ Creating ${count} projects…`;
    status.textContent = count === 1
        ? 'Inventing concept, planning tasks, scaffolding repo…'
        : `This may take a while for ${count} projects…`;

    try {
        const resp = await fetch('/api/wizard/create-instant', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project_type: type, hint, count }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            status.textContent = `Error: ${data.error || resp.statusText}`;
            btn.disabled = false;
            btn.textContent = '⚡ Create';
            return;
        }
        const created = data.created || 0;
        const results = data.results || [];
        const names = results.filter(r => r.success).map(r => r.project_name).join(', ');
        status.textContent = `✓ Created ${created}/${data.requested}: ${names}`;
        btn.textContent = '✓ Done';
        setTimeout(() => { loadProjects(); loadTasks(); }, 1000);
    } catch (e) {
        status.textContent = `Network error: ${e.message}`;
        btn.disabled = false;
        btn.textContent = '⚡ Create';
    }
}

let chatHistory = [];

function toggleChat() {
    const panel = document.getElementById('chatPanel');
    panel.classList.toggle('open');
    if (panel.classList.contains('open')) {
        document.getElementById('chatInput').focus();
    }
}

document.getElementById('chatInput').addEventListener('keydown', function(e) {
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
