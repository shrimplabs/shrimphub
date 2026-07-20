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
    localStorage.setItem('swarm-theme', theme);
    // Sync settings panel theme buttons if panel has been rendered
    if (typeof _updateSettingsThemeBtns === 'function') _updateSettingsThemeBtns();
}

// Debug mode — shows internal panels (429 pressure chart, etc.)
// ── Services bar helpers ──────────────────────────────────────────────────
function _setSvcChip(id, ok, tooltip) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('ok', 'warn', 'down', 'unknown');
    if (ok === true)       el.classList.add('ok');
    else if (ok === false) el.classList.add('down');
    else                   el.classList.add('unknown');
    if (tooltip) el.title = tooltip;
}

async function _probeHeadroom() {
    const probes = [
        { id: 'svc-headroom-8888', port: 8888, label: 'Headroom 8888 (MiniMax)' },
        { id: 'svc-headroom-8877', port: 8877, label: 'Headroom 8877 (Codex)' },
    ];
    for (const p of probes) {
        try {
            const res = await fetch(`http://localhost:${p.port}/health`, { signal: AbortSignal.timeout(3000) });
            const data = res.ok ? await res.json() : null;
            const ok = data && data.status === 'healthy';
            _setSvcChip(p.id, ok, ok ? `${p.label} — healthy` : `${p.label} — unhealthy`);
        } catch {
            _setSvcChip(p.id, false, `${p.label} — unreachable`);
        }
    }
}

let _debugMode = localStorage.getItem('swarm-debug') === '1';
function _applyDebugMode() {
    const panel = document.getElementById('rlHistoryPanel');
    if (panel) panel.style.display = _debugMode ? '' : 'none';
    document.querySelectorAll('.debug-only').forEach(el => {
        el.style.display = _debugMode ? '' : 'none';
    });
    // Update settings panel debug button
    const settingsBtn = document.getElementById('debugModeBtnSettings');
    if (settingsBtn) {
        settingsBtn.textContent = _debugMode ? '🐛 Debug: On' : '🐛 Debug: Off';
        settingsBtn.classList.toggle('active', _debugMode);
    }
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

/* ─── Settings panel ─────────────────────────────────────────────── */
function openSettingsPanel() {
    document.getElementById('settingsPanel').classList.add('open');
    document.getElementById('settingsBackdrop').classList.add('open');
    _updateSettingsThemeBtns();
    _applyDebugMode(); // sync debug button state
    if (typeof syncLocalFallback === 'function') syncLocalFallback();
}
function closeSettingsPanel() {
    document.getElementById('settingsPanel').classList.remove('open');
    document.getElementById('settingsBackdrop').classList.remove('open');
}
function setTheme(theme) {
    applyTheme(theme); // reuse existing applyTheme which persists + updates body attr
    _updateSettingsThemeBtns();
}
function _updateSettingsThemeBtns() {
    const current = document.body.getAttribute('data-theme') || 'cyberpunk';
    document.querySelectorAll('.settings-theme-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.themeVal === current);
    });
}

// Apply before first paint
initTheme();

async function loadData() {
        console.log('Loading data...');
    try {
        const [projectsRes, tasksRes, agentsRes, historyRes, quotaRes, managedRes, healthRes] = await Promise.all([
            fetch(API + '/api/projects'),
            fetch(API + '/api/tasks?include_completed=false'),
            fetch(API + '/api/agents'),
            fetch(API + '/api/history'),
            fetch(API + '/api/quota'),
            fetch(API + '/api/managed-projects'),
            fetch(API + '/api/health'),
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

        // Prompt warnings + service health from /api/health
        if (healthRes.ok) {
            const healthData = await healthRes.json();
            const warnings = healthData.prompt_warnings || [];
            window._promptWarnings = warnings;
            const pill = document.getElementById('promptWarnPill');
            if (pill) {
                pill.style.display = warnings.length > 0 ? '' : 'none';
                document.getElementById('promptWarnCount').textContent = warnings.length;
            }
            // Stale-code chip
            const staleChip = document.getElementById('staleCodeChip');
            if (staleChip) {
                if (healthData.code_stale) {
                    staleChip.style.display = '';
                    staleChip.title = `Server is running ${healthData.running_commit} but repo is at ${healthData.repo_commit} — restart to load current code`;
                } else {
                    staleChip.style.display = 'none';
                }
            }
            // Services bar
            _setSvcChip('svc-swarm', true, `Swarm :5001 — monitor lag ${healthData.monitor_lag_seconds}s, uptime ${Math.round((healthData.uptime_seconds||0)/60)}m`);
            const sr = healthData.shrimp_router;
            if (sr != null) {
                _setSvcChip('svc-shrimp', sr.ok, sr.ok ? `Shrimp-router :8090 — ${Object.keys(sr.backends||{}).length} backends` : 'Shrimp-router :8090 DOWN');
                const mm = sr.backends && sr.backends['minimax'];
                _setSvcChip('svc-minimax', mm === true, mm === true ? 'MiniMax reachable' : mm === false ? 'MiniMax unreachable' : null);
                const vlm = sr.backends && sr.backends['vlm-local'];
                _setSvcChip('svc-vlm', vlm === true, vlm === true ? 'VLM local reachable' : vlm === false ? 'VLM local down' : null);
            } else {
                _setSvcChip('svc-shrimp', null);
                _setSvcChip('svc-minimax', null);
                _setSvcChip('svc-vlm', null);
            }
        } else {
            _setSvcChip('svc-swarm', false, 'Swarm API unreachable');
        }

        // Quota meter (use "general" model entry, fall back to first)
        const model = (quotaData.model_remains || []).find(m => m.model_name === 'general')
                   || (quotaData.model_remains || [])[0];
        if (model) {
            // New API format: use remaining_percent directly when total_count is 0
            const remainingPct = model.current_interval_remaining_percent;
            const total = model.current_interval_total_count;
            const usageCount = model.current_interval_usage_count;
            let pct;
            if (remainingPct != null) {
                pct = 100 - remainingPct;
            } else {
                const used = total - usageCount;
                pct = total > 0 ? (used / total * 100) : 0;
            }
            const fill = document.getElementById('quotaFill');
            fill.style.width = pct + '%';
            fill.style.background = pct > 90 ? '#da3633' : pct > 70 ? '#f0883e' : '#238636';
            const quotaLabel = total > 0
                ? `${(total - usageCount).toLocaleString()} / ${total.toLocaleString()} (${pct.toFixed(1)}%)`
                : `${pct.toFixed(1)}% used`;
            document.getElementById('quotaText').textContent = quotaLabel;
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

        const allTasks = tasks.tasks || [];
        const pending = allTasks.filter(t => t.status === 'pending').length;
        const recoveringCount = allTasks.filter(t =>
            (t.status === 'pending' || t.status === 'in_progress') &&
            t.metadata && t.metadata.is_recovery_task
        ).length;

        const allAgents = agentsData.agents || [];
        const activeAgents = allAgents.filter(a => a.status === 'active');
        const recentAgents = allAgents.slice(-12); // show last 12

        // Count completed agents from history in the last 24 hours
        const cutoff = Date.now() - 24 * 60 * 60 * 1000;
        const cutoff1h = Date.now() - 60 * 60 * 1000;
        const historyAgentsList = historyData.agents || [];
        const completedToday = historyAgentsList.filter(a => {
            if (a.status !== 'completed') return false;
            const t = a.completed_at ? new Date(a.completed_at).getTime() : 0;
            return t > cutoff;
        }).length;
        // Agents completed in the last hour (throughput rate)
        const completedLastHour = historyAgentsList.filter(a => {
            if (a.status !== 'completed') return false;
            const t = a.completed_at ? new Date(a.completed_at).getTime() : 0;
            return t > cutoff1h;
        }).length;

        const totalTokenCount = activeAgents.reduce((sum, a) => sum + (a.input_tokens || 0) + (a.output_tokens || 0), 0);
        const totalTokensLabel = totalTokenCount >= 1e9 ? (totalTokenCount/1e9).toFixed(1)+'B'
            : totalTokenCount >= 1e6 ? (totalTokenCount/1e6).toFixed(1)+'M'
            : totalTokenCount >= 1e3 ? (totalTokenCount/1e3).toFixed(1)+'k'
            : totalTokenCount > 0 ? String(totalTokenCount) : '0';

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

        // Recovery tasks — red pill when non-zero
        const recoveringEl = document.getElementById('recoveringCount');
        recoveringEl.textContent = recoveringCount;
        recoveringEl.closest('.stat-pill').classList.toggle('stat-pill-failed-active', recoveringCount > 0);

        // Agents/hr throughput
        document.getElementById('agentsPerHour').textContent = completedLastHour;
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
        
        // Pending tasks - sorted by priority, flat grid
        const pendingGrid = document.getElementById('pendingTasksGrid');
        const pendingTasks = allTasks.filter(t => t.status === 'pending');
        pendingTasks.sort((a, b) => (b.priority || 50) - (a.priority || 50));
        pendingGrid.innerHTML = pendingTasks.length
            ? pendingTasks.map(t => createTaskCard(t, false, false, '', false)).join('')
            : '<div class="card" style="color:var(--text-faint);font-size:13px">No pending tasks</div>';
        
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
            // Use most recent commit timestamp as activity signal (more reliable than last_update)
            const commits = data.recent_commits || [];
            if (commits.length > 0 && commits[0].timestamp) {
                activityMap[name] = Math.max(activityMap[name] || 0, commits[0].timestamp);
            }
        }

        const sortedProjects = sortProjectEntries(Object.entries(projectList), projectTaskCount, activityMap);
        const visibleProjects = getVisibleProjectEntries(sortedProjects, activityMap);
        const sidebarProjectNames = sortedProjects.map(([name]) => name);
        _allProjectNames = sidebarProjectNames;

        // Recently completed: projects with an agent completed in the last 24h,
        // sorted by most recent completion, capped at 8, excluding currently live projects.
        const recentCompletionMap = {};
        for (const a of historyAgentsList) {
            if (a.status !== 'completed' || !a.project || !a.completed_at) continue;
            const ms = new Date(a.completed_at).getTime();
            if (!Number.isFinite(ms) || ms <= 0) continue;
            if (Date.now() - ms > 24 * 60 * 60 * 1000) continue;
            recentCompletionMap[a.project] = Math.max(recentCompletionMap[a.project] || 0, ms);
        }
        const recentlyCompleted = Object.entries(recentCompletionMap)
            .sort((a, b) => b[1] - a[1])
            .slice(0, 8)
            .map(([name, ms]) => ({ name, ms }));
        _recentlyCompleted = recentlyCompleted;
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

        // Fetch health and closure per-project and update each card in-place to avoid full re-render flicker.
        await Promise.all(visibleProjects.map(async ([name, data]) => {
            await Promise.all([fetchProjectHealth(name), fetchProjectClosure(name)]);
            const safeName = name.replace(/[^a-z0-9]/gi, '_');
            const cardEl = document.getElementById(`project-card-${safeName}`);
            if (!cardEl) return;
            const newHtml = createProjectCard(name, data, projectAnyTaskCount[name] || 0, velocityMap[name] || null);
            const tmp = document.createElement('div');
            tmp.innerHTML = newHtml;
            const newCard = tmp.firstElementChild;
            if (newCard) cardEl.replaceWith(newCard);
            loadProjectNotes(name, `notes-${safeName}`);
        }));
        applyProjectFilter();

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

function showPromptWarnings() {
    const warnings = window._promptWarnings || [];
    if (!warnings.length) return;
    const list = warnings.map(w => `<li style="margin:4px 0;color:#f0883e">${w}</li>`).join('');
    const html = `<b style="color:#f0883e">⚠ Prompt template warnings</b><br><small style="color:#8b949e">These variables were missing when a prompt was rendered — the field was left blank. Fix the prompt or the task script to eliminate these.</small><ul style="margin:8px 0 0;padding-left:18px;font-size:12px">${list}</ul>`;
    const el = document.createElement('div');
    el.innerHTML = html;
    el.style.cssText = 'position:fixed;bottom:24px;right:24px;z-index:9999;background:#161b22;border:1px solid rgba(240,136,62,0.5);border-radius:6px;padding:16px 20px;font-size:13px;color:#e6edf3;max-width:480px;line-height:1.6;box-shadow:0 4px 16px rgba(0,0,0,.6);cursor:pointer';
    el.title = 'Click to dismiss';
    el.onclick = () => el.remove();
    document.body.appendChild(el);
}
