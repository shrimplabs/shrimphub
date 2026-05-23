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

// Debug mode — shows internal panels (429 pressure chart, etc.)
let _debugMode = localStorage.getItem('swarm-debug') === '1';
function _applyDebugMode() {
    const panel = document.getElementById('rlHistoryPanel');
    const btn = document.getElementById('debugModeBtn');
    if (panel) panel.style.display = _debugMode ? '' : 'none';
    if (btn) btn.style.color = _debugMode ? '#f0883e' : '#484f58';
}
function toggleDebugMode() {
    _debugMode = !_debugMode;
    localStorage.setItem('swarm-debug', _debugMode ? '1' : '0');
    _applyDebugMode();
}
document.addEventListener('DOMContentLoaded', _applyDebugMode);

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
        applyProjectFilter();
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

function showToast(html, color) {
    color = color || '#3fb950';
    const el = document.createElement('div');
    el.innerHTML = html;
    el.style.cssText = `position:fixed;bottom:24px;right:24px;z-index:9999;background:#161b22;border:1px solid ${color};border-radius:6px;padding:12px 18px;font-size:13px;color:#e6edf3;max-width:380px;line-height:1.5;box-shadow:0 4px 16px rgba(0,0,0,.5);transition:opacity .4s`;
    document.body.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 400); }, 4000);
}
