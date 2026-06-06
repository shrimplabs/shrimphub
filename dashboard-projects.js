
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

async function spawnArtSprint(event, name) {
    const btn = event.currentTarget;
    btn.disabled = true;
    btn.textContent = '⏳ Queuing…';
    try {
        const res = await fetch(`${API}/api/tasks/batch`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                project: name,
                tasks: [
                    {
                        type: 'art_pass',
                        description: `Art pass for ${name}. Assess current visual state with a screenshot, find and integrate relevant assets to improve appearance. Focus on placeholder sprites, missing icons, UI elements that look unfinished.`,
                        priority: 70
                    },
                    {
                        type: 'polish',
                        description: `Polish pass for ${name}. After art pass: check screen transitions, button feedback, menu flow, HUD clarity, audio cues, game feel. Take before/after screenshots for any visual change.`,
                        priority: 70,
                        depends_on: [0]
                    },
                    {
                        type: 'qa',
                        description: `QA pass for ${name}. After art and polish: launch the game, read GAME_DESIGN.md, test all critical flows, verify the game is playable end-to-end. File bug tasks for any deviations.`,
                        priority: 75,
                        depends_on: [1]
                    }
                ]
            })
        });
        const data = await res.json();
        if (data.created === 3) {
            showToast(`🎨 Art sprint queued for <strong>${escapeHtml(name)}</strong> — art → polish → QA`, '#3fb950');
        } else {
            showToast(`Art sprint: ${escapeHtml(data.error || 'unexpected response')}`, '#f85149');
        }
    } catch(e) {
        showToast('Network error queuing art sprint', '#f85149');
    } finally {
        btn.disabled = false;
        btn.textContent = '🎨 Art Sprint';
    }
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
    if (!confirm('Kill all running agents and disable auto mode?')) return;
    const btn = document.getElementById('killAllBtn');
    btn.disabled = true;
    btn.textContent = '⏹ Stopping…';
    try {
        // Turn off auto mode first so nothing respawns
        await fetch(API + '/api/auto-mode', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: false})
        });
        autoEnabled = false;
        autoSuspended = false;
        updateAutoBtn();

        // Kill all active agents
        const res = await fetch(API + '/api/agents');
        const data = await res.json();
        const active = (data.agents || []).filter(a => a.status === 'active');
        await Promise.all(active.map(a =>
            fetch(`${API}/api/agents/${a.id}/kill`, {method: 'POST'})
        ));
        showToast(`⏹ Emergency stop — auto mode off, ${active.length} agent${active.length === 1 ? '' : 's'} killed`, '#f0883e');
        setTimeout(loadData, 1000);
    } catch(e) {
        showToast('Kill All failed: ' + e.message, '#f85149');
    }
    btn.textContent = '⏹ Kill All';
    btn.disabled = false;
}

function createAgentCard(agent, taskDesc) {
    const isActive = agent.status === 'active';
    const statusLabel = agent.status || "unknown";
    const meta = agent.metadata || {};
    const diffStat = meta.diff_stat ? meta.diff_stat.split('\n').pop() : '';
    const descFull = taskDesc ? taskDesc.trim() : '';
    const descFirstLine = descFull.split('\n')[0].slice(0, 120);
    const descTruncated = descFull.length > 120 || descFull.includes('\n');
    const descId = `agent-desc-${agent.id ? agent.id.slice(0, 8) : Math.random().toString(36).slice(2)}`;
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
        <div class="card agent-card status-${badgeClass}" data-project="${escapeHtml(agent.project)}" onclick="showAgentOutput('${agent.id}', '${agent.project}', ${isActive}, '${agent.task_id || ''}')">
            <div class="card-header">
                <span class="project-name">${escapeHtml(agent.project)}</span>
                <span class="agent-short-id">${shortId}</span>
                <span class="status ${badgeClass}">${badgeLabel}</span>
            </div>
            ${descFull ? `
            <div style="font-size:11px;color:#8b949e;margin:4px 0 2px;line-height:1.4">
                <span id="${descId}-short">${escapeHtml(descFirstLine)}${descTruncated ? '<span style="color:#6e7681">…</span>' : ''}</span>
                ${descTruncated ? `<span id="${descId}-full" style="display:none;white-space:pre-wrap">${escapeHtml(descFull)}</span>` : ''}
                ${descTruncated ? `<button onclick="event.stopPropagation();(function(s,f,b){var show=f.style.display==='none';f.style.display=show?'':'none';s.style.display=show?'none':'';b.textContent=show?'less':'more';})(document.getElementById('${descId}-short'),document.getElementById('${descId}-full'),this)" style="background:none;border:none;color:#58a6ff;font-size:10px;cursor:pointer;padding:0 0 0 4px;vertical-align:baseline">more</button>` : ''}
            </div>` : ''}
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
    const humanReviewBadge = needsHumanReview ? `<span title="${escapeHtml(meta.human_review_reason || 'Agent and research both failed — needs human diagnosis')}" style="background:#da3633;color:#fff;font-size:10px;padding:1px 6px;border-radius:3px;margin-left:6px;font-weight:600">🛑 needs review</span>` : '';
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
        <div class="card ${deepChain ? 'deep-chain-warning' : ''} ${needsHumanReview ? 'needs-human-review' : ''}" data-project="${escapeHtml(task.project)}" ${cardAttr} style="${isActive ? '' : 'cursor:pointer'}">
            <div class="card-header"${isParent ? ` onclick="toggleChildren('${task.id}')"` : ''}>
                <span class="project-name">${isChild ? '<span class="task-id-prefix">&#8618;</span>' : ''}${escapeHtml(task.project)}</span>
                ${isParent ? '<span class="collapse-toggle" id="toggle-' + task.id + '">[-] hide</span>' : ''}
                ${activeBadge}
                <span class="status ${task.status}">${task.status.replace('_', ' ')}</span>${deepChainBadge}${humanReviewBadge}${delegationBadge}${helperBadge}
            </div>
            <div class="task-desc" title="${escapeHtml(task.description)}">${escapeHtml(task.description.split('\n')[0].slice(0, 120))}${task.description.length > 120 || task.description.includes('\n') ? '…' : ''}</div>
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
let _projectNotesCache = {};  // { projectName: { notes, blurb } }
let _selectedProject = null;  // null = all projects
let _allProjectNames = [];
let _recentlyCompleted = [];  // [{ name, ms }] sorted by most recent, last 24h
let _integrityData = null;
const PROJECT_SORT_MODES = new Set(['active', 'recent', 'name', 'largest']);
let _projectSortMode = PROJECT_SORT_MODES.has(localStorage.getItem('swarm.projectSortMode'))
    ? localStorage.getItem('swarm.projectSortMode')
    : 'active';
let _recentProjectsOnly = localStorage.getItem('swarm.recentProjectsOnly') === 'true';
let _projectRecentWindowDays = parseInt(localStorage.getItem('swarm.projectRecentWindowDays') || '7', 10);
if (![1, 7, 30].includes(_projectRecentWindowDays)) _projectRecentWindowDays = 7;

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

    const artSprintBtn = `<button onclick="spawnArtSprint(event,'${escapeHtml(name)}')"
        style="background:transparent;color:#f0883e;border:1px solid #f0883e;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer"
        title="Queue an art pass → polish → QA chain for this project">🎨 Art Sprint</button>`;

    const isAutoReplan = _autoReplanProjects.has(name);
    const autoReplanBtn = `<button onclick="toggleAutoReplan(event,'${escapeHtml(name)}')"
        style="background:transparent;color:${isAutoReplan ? '#a371f7' : '#8b949e'};border:1px solid ${isAutoReplan ? '#a371f7' : '#30363d'};border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer"
        title="${isAutoReplan ? 'Auto re-plan ON — will spawn new plan when tasks run out' : 'Auto re-plan OFF'}">♻ Auto</button>`;

    const snapshotBtn = `<button onclick="openSnapshotModal(event,'${escapeHtml(name)}')"
        style="background:transparent;color:#79c0ff;border:1px solid #79c0ff;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer"
        title="Save, restore, or clone a snapshot of this project">📸 Snapshots</button>`;

    const removeProjectBtn = `<button onclick="removeProject(event,'${escapeHtml(name)}')"
        style="background:transparent;color:#f85149;border:1px solid #f85149;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer"
        title="Remove project from swarm (does not delete the git repo)">✕ Remove</button>`;

    const projectTokens = _projectTokenMap[name] || 0;
    const projectTokensHtml = projectTokens > 0
        ? `<span style="font-size:11px;color:#8b949e;margin-left:8px">${(projectTokens/1000).toFixed(1)}k tok</span>`
        : '';

    const isActive = _activeProjectSet.has(name);
    const activeLed = isActive ? '<span class="active-led" title="Agent running"></span>' : '';

    const cachedBlurb = (_projectNotesCache[name] && _projectNotesCache[name].blurb) || '';
    return `
        <div class="card" id="project-card-${String(name).replace(/[^a-z0-9]/gi, '_')}" data-has-project="${escapeHtml(name)}" style="${isPaused ? 'opacity:0.6' : ''}">
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
                    ${artSprintBtn}
                    ${autoReplanBtn}
                    ${snapshotBtn}
                    ${removeProjectBtn}
                    <span class="status ${data.status}" style="margin-left:4px">${data.status}</span>
                </div>
            </div>
            <div id="${blurbId}" style="font-size:11px;color:#8b949e;margin:2px 0 6px 0;font-style:italic;min-height:14px">${escapeHtml(cachedBlurb)}</div>
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
            // Compute blurb: prefer first line of notes, fall back to GAME_DESIGN.md concept
            let blurb = '';
            if (data.notes) {
                const lines = data.notes.split('\n').map(l => l.trim()).filter(l => l && !l.startsWith('#') && !l.startsWith('-'));
                blurb = lines[0] || '';
            }
            if (!blurb && data.concept) {
                blurb = data.concept;
            }
            const blurbText = blurb.length > 150 ? blurb.slice(0, 147) + '…' : blurb;
            // Cache so card re-renders pre-fill the blurb without a fetch
            _projectNotesCache[name] = { notes: data.notes || '', blurb: blurbText };
            // Update in-place — avoids flash on card replacement
            const blurbEl = document.getElementById(`blurb-${name.replace(/[^a-z0-9]/gi, '_')}`);
            if (blurbEl) blurbEl.textContent = blurbText;
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
