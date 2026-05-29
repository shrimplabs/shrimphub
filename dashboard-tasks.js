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

// ---- Task Info Panel (read-only, for completed/failed/cancelled nodes) ----

function closeTaskInfoModal() {
    document.getElementById('taskInfoModal').classList.remove('active');
}

function openTaskInfoPanel(task) {
    const statusColors = {
        completed: '#3fb950', failed: '#f85149', cancelled: '#8b949e',
        in_progress: '#58a6ff', pending: '#e6edf3',
    };
    const color = statusColors[task.status] || '#e6edf3';
    const meta = task.metadata || {};

    function row(label, value, mono) {
        if (!value && value !== 0) return '';
        const style = mono ? 'font-family:monospace;font-size:12px;white-space:pre-wrap;word-break:break-all' : 'font-size:13px';
        return `<div>
            <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.08em;margin-bottom:3px">${label}</div>
            <div style="${style};color:#e6edf3">${escapeHtml(String(value))}</div>
        </div>`;
    }

    function pill(label, value, c) {
        return `<span style="font-size:11px;border:1px solid ${c||'#30363d'};color:${c||'#8b949e'};border-radius:999px;padding:2px 8px">${escapeHtml(label)}: ${escapeHtml(String(value))}</span>`;
    }

    const pills = [
        pill('type', task.type, '#58a6ff'),
        pill('project', task.project, '#8b949e'),
        pill('priority', task.priority, '#8b949e'),
        pill('attempts', `${task.attempts}/${task.max_attempts}`, task.attempts >= task.max_attempts ? '#f85149' : '#8b949e'),
    ].join(' ');

    const timestamps = [
        task.created ? `Created ${new Date(task.created).toLocaleString()}` : '',
        task.started ? `Started ${new Date(task.started).toLocaleString()}` : '',
        task.completed ? `Completed ${new Date(task.completed).toLocaleString()}` : '',
    ].filter(Boolean).join(' · ');

    const deps = (task.dependencies || []).length
        ? task.dependencies.join('\n')
        : null;

    const diffStat = meta.diff_stat || null;
    const lastFailure = meta.last_failure || null;
    const researchCtx = meta.research_context || null;

    document.getElementById('taskInfoTitle').innerHTML =
        `<span style="color:${color}">${escapeHtml(task.status.replace('_',' '))}</span> · ${escapeHtml(task.id)}`;

    document.getElementById('taskInfoBody').innerHTML = `
        <div style="display:flex;flex-wrap:wrap;gap:6px">${pills}</div>
        ${timestamps ? `<div style="font-size:12px;color:#6e7681">${escapeHtml(timestamps)}</div>` : ''}
        ${row('Description', task.description)}
        ${deps ? row('Dependencies', deps, true) : ''}
        ${diffStat ? row('Diff stat', diffStat, true) : ''}
        ${lastFailure ? `<div>
            <div style="font-size:11px;color:#f85149;text-transform:uppercase;letter-spacing:.08em;margin-bottom:3px">Last failure</div>
            <pre style="font-size:11px;color:#e6edf3;background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:8px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;max-height:200px;overflow-y:auto;margin:0">${escapeHtml(lastFailure)}</pre>
        </div>` : ''}
        ${researchCtx ? `<div>
            <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.08em;margin-bottom:3px">Research context</div>
            <pre style="font-size:11px;color:#e6edf3;background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:8px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;max-height:150px;overflow-y:auto;margin:0">${escapeHtml(researchCtx)}</pre>
        </div>` : ''}
    `;

    // Footer actions
    const footer = document.getElementById('taskInfoFooter');
    const btns = [];
    if (task.agent_id) {
        btns.push(`<button onclick="closeTaskInfoModal();showAgentOutput('${escapeHtml(task.agent_id)}','${escapeHtml(task.project)}',true,'${escapeHtml(task.id)}')" style="background:#161b22;color:#58a6ff;border:1px solid #1f6feb;border-radius:4px;padding:5px 12px;font-size:12px;cursor:pointer">📋 View log</button>`);
    }
    if (task.status === 'failed') {
        btns.push(`<button onclick="closeTaskInfoModal();openResetModal('${escapeHtml(task.id)}')" style="background:#161b22;color:#f0883e;border:1px solid rgba(240,136,62,.4);border-radius:4px;padding:5px 12px;font-size:12px;cursor:pointer">↺ Reset</button>`);
    }
    btns.push(`<button onclick="closeTaskInfoModal()" style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:5px 12px;font-size:12px;cursor:pointer">Close</button>`);
    footer.innerHTML = btns.join('');

    document.getElementById('taskInfoModal').classList.add('active');
}

