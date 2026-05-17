(function () {
    let _contextProvider = null;
    let _viz = null;
    let _depHistoryLimit = 2;
    let _depGraphState = null;
    let _depGraphViewByProject = {};
    let _integrityData = null;

    function _ctx() {
        return _contextProvider ? _contextProvider() : {};
    }

    function _depRenderHistoryCandidates() {
        const seed = [Number(_depHistoryLimit) || 2, 2, 1, 0];
        return [...new Set(seed.filter(n => Number.isFinite(n) && n >= 0))];
    }

    function _isVizMemoryError(error) {
        const message = (error && (error.message || error.toString()) || '').toLowerCase();
        return message.includes('cannot enlarge memory arrays') || message.includes('out of memory') || message.includes('memory');
    }

    function _depGraphViewKey() {
        return _ctx().selectedProject || '__all__';
    }

    function _depGraphShortcutEnabled() {
        if (!(_ctx().isDepsVisible && _ctx().isDepsVisible()) || !_depGraphState) return false;
        const el = document.activeElement;
        if (!el) return true;
        const tag = (el.tagName || '').toLowerCase();
        return tag !== 'input' && tag !== 'textarea' && !el.isContentEditable;
    }

    function _getDepGraphNodeBoundsByTaskIds(svg, taskIds) {
        if (!svg || !taskIds || !taskIds.size) return null;
        let minX = Infinity;
        let minY = Infinity;
        let maxX = -Infinity;
        let maxY = -Infinity;
        let found = 0;
        svg.querySelectorAll('g.node').forEach(node => {
            const titleEl = node.querySelector('title');
            if (!titleEl) return;
            const taskId = titleEl.textContent.trim();
            if (!taskIds.has(taskId)) return;
            const box = node.getBBox();
            minX = Math.min(minX, box.x);
            minY = Math.min(minY, box.y);
            maxX = Math.max(maxX, box.x + box.width);
            maxY = Math.max(maxY, box.y + box.height);
            found += 1;
        });
        if (!found) return null;
        return {x: minX, y: minY, width: Math.max(1, maxX - minX), height: Math.max(1, maxY - minY)};
    }

    function _getFocusedDepTaskIds(tasks, selectedProject = null) {
        const relevantTasks = (tasks || []).filter(task => !selectedProject || task.project === selectedProject);
        const taskById = new Map(relevantTasks.map(task => [task.id, task]));
        const focused = new Set();
        const stack = [];
        relevantTasks.forEach(task => {
            if (task.status === 'pending' || task.status === 'in_progress') {
                focused.add(task.id);
                stack.push(task.id);
            }
        });
        while (stack.length) {
            const taskId = stack.pop();
            const task = taskById.get(taskId);
            if (!task) continue;
            const deps = Array.isArray(task.dependencies) ? task.dependencies : [];
            deps.forEach(depId => {
                if (!depId || focused.has(depId) || !taskById.has(depId)) return;
                focused.add(depId);
                stack.push(depId);
            });
        }
        return focused;
    }

    function saveDepGraphView() {
        if (!_depGraphState) return;
        _depGraphViewByProject[_depGraphViewKey()] = {
            scale: _depGraphState.scale,
            translateX: _depGraphState.translateX,
            translateY: _depGraphState.translateY,
        };
    }

    function applyDepGraphTransform() {
        if (!_depGraphState || !_depGraphState.graphRoot) return;
        _depGraphState.graphRoot.setAttribute(
            'transform',
            `translate(${_depGraphState.translateX}, ${_depGraphState.translateY}) scale(${_depGraphState.scale})`
        );
    }

    function fitDepGraphView(boundsOverride = null) {
        if (!_depGraphState || !_depGraphState.graphRoot || !_depGraphState.container) return;
        const bounds = boundsOverride || _depGraphState.graphRoot.getBBox();
        const containerRect = _depGraphState.container.getBoundingClientRect();
        const padding = 48;
        const availableW = Math.max(1, containerRect.width - padding * 2);
        const availableH = Math.max(1, containerRect.height - padding * 2);
        const scaleX = availableW / Math.max(1, bounds.width);
        const scaleY = availableH / Math.max(1, bounds.height);
        const scale = Math.max(_depGraphState.minScale, Math.min(_depGraphState.maxScale, Math.min(scaleX, scaleY)));
        _depGraphState.scale = scale;
        _depGraphState.translateX = padding - bounds.x * scale + Math.max(0, (availableW - bounds.width * scale) / 2);
        _depGraphState.translateY = padding - bounds.y * scale + Math.max(0, (availableH - bounds.height * scale) / 2);
        applyDepGraphTransform();
        saveDepGraphView();
    }

    function initDepGraphInteraction(container, svg, options = {}) {
        const graphRoot = svg.querySelector('g');
        if (!graphRoot) {
            _depGraphState = null;
            return;
        }
        _depGraphState = {
            container,
            svg,
            graphRoot,
            scale: 1,
            minScale: 0.35,
            maxScale: 4,
            translateX: 0,
            translateY: 0,
            isDragging: false,
            dragMoved: false,
            startX: 0,
            startY: 0,
            originX: 0,
            originY: 0,
            focusBounds: options.focusBounds || null,
        };
        const savedView = _depGraphViewByProject[_depGraphViewKey()];
        if (savedView) {
            _depGraphState.scale = savedView.scale;
            _depGraphState.translateX = savedView.translateX;
            _depGraphState.translateY = savedView.translateY;
            applyDepGraphTransform();
        } else {
            window.requestAnimationFrame(() => {
                window.requestAnimationFrame(() => {
                    if (_depGraphState && _depGraphState.svg === svg) fitDepGraphView(_depGraphState.focusBounds);
                });
            });
        }

        container.onwheel = (event) => {
            if (!_depGraphState) return;
            event.preventDefault();
            const factor = event.deltaY < 0 ? 1.1 : 1 / 1.1;
            api.zoomDepGraph(factor, event.clientX, event.clientY);
        };

        container.onmousedown = (event) => {
            if (!_depGraphState || event.button !== 0) return;
            _depGraphState.isDragging = true;
            _depGraphState.dragMoved = false;
            _depGraphState.startX = event.clientX;
            _depGraphState.startY = event.clientY;
            _depGraphState.originX = _depGraphState.translateX;
            _depGraphState.originY = _depGraphState.translateY;
            container.classList.add('is-dragging');
        };

        window.onmousemove = (event) => {
            if (!_depGraphState || !_depGraphState.isDragging) return;
            const dx = event.clientX - _depGraphState.startX;
            const dy = event.clientY - _depGraphState.startY;
            if (Math.abs(dx) > 3 || Math.abs(dy) > 3) _depGraphState.dragMoved = true;
            _depGraphState.translateX = _depGraphState.originX + dx;
            _depGraphState.translateY = _depGraphState.originY + dy;
            applyDepGraphTransform();
            saveDepGraphView();
        };

        window.onmouseup = () => {
            if (!_depGraphState) return;
            _depGraphState.isDragging = false;
            container.classList.remove('is-dragging');
            window.setTimeout(() => {
                if (_depGraphState) _depGraphState.dragMoved = false;
            }, 0);
        };

        container.onclick = (event) => {
            if (_depGraphState && _depGraphState.dragMoved) {
                event.preventDefault();
                event.stopPropagation();
            }
        };
    }

    async function openNodeDetail(taskId) {
        const ctx = _ctx();
        const taskDataMap = ctx.taskDataMap ? ctx.taskDataMap() : {};
        const agentByTaskId = ctx.agentByTaskId ? ctx.agentByTaskId() : {};
        const task = taskDataMap[taskId];
        if (!task) return;
        if (task.status === 'in_progress') {
            const agent = agentByTaskId[taskId];
            if (agent && ctx.showAgentOutput) {
                ctx.showAgentOutput(agent.id, task.project, true, taskId);
                return;
            }
        }
        if (ctx.openEditTaskModal) ctx.openEditTaskModal(task);
    }

    const api = {
        setContextProvider(fn) {
            _contextProvider = fn;
        },
        getDepHistoryLimit() {
            return _depHistoryLimit;
        },
        setDepHistoryLimit(value) {
            const parsed = parseInt(value, 10);
            _depHistoryLimit = Number.isFinite(parsed) ? Math.max(0, Math.min(parsed, 25)) : 2;
            const select = document.getElementById('depsHistoryLimit');
            if (select && select.value !== String(_depHistoryLimit)) select.value = String(_depHistoryLimit);
        },
        resetDepGraphView() {
            if (!_depGraphState) return;
            _depGraphState.scale = 1;
            _depGraphState.translateX = 0;
            _depGraphState.translateY = 0;
            applyDepGraphTransform();
            saveDepGraphView();
        },
        zoomDepGraph(factor, clientX = null, clientY = null) {
            if (!_depGraphState) return;
            const containerRect = _depGraphState.container.getBoundingClientRect();
            const anchorX = clientX === null ? containerRect.left + containerRect.width / 2 : clientX;
            const anchorY = clientY === null ? containerRect.top + containerRect.height / 2 : clientY;
            const localX = anchorX - containerRect.left;
            const localY = anchorY - containerRect.top;
            const prevScale = _depGraphState.scale;
            const nextScale = Math.max(_depGraphState.minScale, Math.min(_depGraphState.maxScale, _depGraphState.scale * factor));
            if (nextScale === prevScale) return;
            const graphX = (localX - _depGraphState.translateX) / prevScale;
            const graphY = (localY - _depGraphState.translateY) / prevScale;
            _depGraphState.scale = nextScale;
            _depGraphState.translateX = localX - graphX * nextScale;
            _depGraphState.translateY = localY - graphY * nextScale;
            applyDepGraphTransform();
            saveDepGraphView();
        },
        focusActiveDepGraphView() {
            if (!_depGraphState) return;
            fitDepGraphView(_depGraphState.focusBounds || null);
        },
        fitDepGraphView() {
            fitDepGraphView();
        },
        renderIntegrityPanel(data) {
            _integrityData = data || null;
            const ctx = _ctx();
            const escapeHtml = ctx.escapeHtml || (s => s);
            const subtitle = document.getElementById('integritySubtitle');
            const summaryEl = document.getElementById('integritySummary');
            const findingsEl = document.getElementById('integrityFindings');
            const actionsEl = document.getElementById('integrityActions');
            if (!subtitle || !summaryEl || !findingsEl || !actionsEl) return;
            const scopeLabel = ctx.selectedProject ? `Project: ${ctx.selectedProject}` : 'All projects';
            if (!data || data.error) {
                subtitle.textContent = data?.error ? `Diagnostics unavailable: ${data.error}` : 'Diagnostics unavailable';
                summaryEl.innerHTML = '';
                findingsEl.innerHTML = '';
                actionsEl.innerHTML = '';
                return;
            }
            const summary = data.summary || {};
            const live = summary.live || {};
            const archival = summary.archival || {};
            const active = summary.active || {};
            const findings = data.findings || {};
            subtitle.textContent = `${scopeLabel} · ${live.problem_count || 0} live issues · ${archival.problem_count || 0} archival issues`;
            const chips = [
                ['Live issues', live.problem_count || 0, `${live.stale_heads || 0} heads · ${live.orphaned_agents || 0} agents`],
                ['Archival issues', archival.problem_count || 0, `${archival.stale_plans || 0} stale plans`],
                ['Blocked tasks', active.blocked_tasks || 0, `${active.missing_dependencies || 0} missing deps`],
                ['Invalid blockers', live.dead_blockers || 0, `${live.continuity_gaps || 0} continuity gaps`],
                ['Ready tasks', active.ready_tasks || 0, `${active.task_count || 0} active tasks scanned`],
            ];
            summaryEl.innerHTML = chips.map(([label, value, note]) => `
                <div class="integrity-chip">
                    <div class="integrity-chip-label">${escapeHtml(label)}</div>
                    <div class="integrity-chip-value">${escapeHtml(String(value))}</div>
                    <div class="integrity-chip-note">${escapeHtml(note)}</div>
                </div>
            `).join('');
            const findingPills = [];
            if ((live.stale_heads || 0) > 0) findingPills.push(['warn', `${live.stale_heads} stale head${live.stale_heads === 1 ? '' : 's'}`]);
            if ((live.orphaned_agents || 0) > 0) findingPills.push(['warn', `${live.orphaned_agents} orphaned agent${live.orphaned_agents === 1 ? '' : 's'}`]);
            if ((live.dead_blockers || 0) > 0) findingPills.push(['danger', `${live.dead_blockers} task${live.dead_blockers === 1 ? '' : 's'} blocked by invalid state`]);
            if ((live.recursive_recovery || 0) > 0) findingPills.push(['danger', `${live.recursive_recovery} recursive recovery chain${live.recursive_recovery === 1 ? '' : 's'}`]);
            if ((archival.stale_plans || 0) > 0) findingPills.push(['warn', `${archival.stale_plans} stale plan snapshot${archival.stale_plans === 1 ? '' : 's'}`]);
            const staleHeadRows = (findings.stale_heads || []).map(item => `
                <div class="integrity-detail-row">
                    <div class="integrity-detail-main">
                        <span class="integrity-detail-kind warn">Stale head</span>
                        <span class="integrity-detail-project">${escapeHtml(item.project || 'unknown project')}</span>
                        <span class="integrity-detail-meta">${escapeHtml(item.head_task_id || 'missing')} · ${escapeHtml(item.reason || 'invalid')}</span>
                    </div>
                    <div class="integrity-detail-actions">
                        <button type="button" class="integrity-action-btn warn" onclick="runIntegrityRepair('head', '${escapeHtml(item.project || '')}')">Repair head</button>
                    </div>
                </div>
            `);
            const stalePlanRows = (findings.stale_plans || []).map(item => `
                <div class="integrity-detail-row">
                    <div class="integrity-detail-main">
                        <span class="integrity-detail-kind warn">Stale plan</span>
                        <span class="integrity-detail-project">${escapeHtml(item.project || 'unknown project')}</span>
                        <span class="integrity-detail-meta">${escapeHtml(item.plan_id || 'unknown plan')} · ${escapeHtml((item.reasons || []).join(', ') || 'stale')}</span>
                    </div>
                    <div class="integrity-detail-actions">
                        <button type="button" class="integrity-action-btn warn" onclick="runIntegrityRepair('plans', '${escapeHtml(item.project || '')}')">Clean stale plans</button>
                    </div>
                </div>
            `);
            const detailRows = [...staleHeadRows, ...stalePlanRows];
            findingsEl.innerHTML = (
                findingPills.length
                    ? `<div class="integrity-finding-pills">${findingPills.map(([kind, text]) => `<span class="integrity-finding-pill ${kind}">${escapeHtml(text)}</span>`).join('')}</div>`
                    : ''
            ) + (
                detailRows.length
                    ? `<div class="integrity-detail-list">${detailRows.join('')}</div>`
                    : '<div class="integrity-chip-note">No integrity findings detected for the current scope.</div>'
            );
            const button = (label, kind, cls='') => `<button type="button" class="integrity-action-btn ${cls}" onclick="runIntegrityRepair('${kind}')">${label}</button>`;
            const buttons = [];
            if (ctx.selectedProject && (live.stale_heads || 0) > 0) buttons.push(button('Repair head', 'head', 'warn'));
            if (ctx.selectedProject && (archival.stale_plans || 0) > 0) buttons.push(button('Clean stale plans', 'plans', 'warn'));
            if (ctx.selectedProject && ((live.recursive_recovery || 0) > 0 || (live.continuity_gaps || 0) > 0)) buttons.push(button('Clean recovery chain', 'recovery', 'danger'));
            if ((live.orphaned_agents || 0) > 0) buttons.push(button('Reconcile agents', 'agents', 'warn'));
            actionsEl.innerHTML = buttons.length ? buttons.join('') : '<div class="integrity-chip-note">No direct repair actions suggested for the current integrity state.</div>';
        },
        async loadIntegrityPanel() {
            const ctx = _ctx();
            const subtitle = document.getElementById('integritySubtitle');
            if (subtitle) subtitle.textContent = 'Loading diagnostics…';
            try {
                const projectQuery = ctx.selectedProject ? `project=${encodeURIComponent(ctx.selectedProject)}&` : '';
                const res = await fetch(`${ctx.API}/api/dependencies/integrity?${projectQuery}include_history=true`);
                const data = await res.json();
                api.renderIntegrityPanel(data);
            } catch (e) {
                api.renderIntegrityPanel({error: e.message});
            }
        },
        async runIntegrityRepair(kind, projectOverride = null) {
            const ctx = _ctx();
            const before = _integrityData;
            const projectName = projectOverride || ctx.selectedProject;
            let url = '';
            if (kind === 'head' && projectName) url = `${ctx.API}/api/projects/${encodeURIComponent(projectName)}/reconcile-head`;
            else if (kind === 'plans' && projectName) url = `${ctx.API}/api/plans/${encodeURIComponent(projectName)}/cleanup`;
            else if (kind === 'recovery' && projectName) url = `${ctx.API}/api/projects/${encodeURIComponent(projectName)}/cleanup-recovery`;
            else if (kind === 'agents') url = `${ctx.API}/api/agents/reconcile`;
            else {
                ctx.showToast && ctx.showToast('Repair action requires a selected project.', '#f85149');
                return;
            }
            try {
                const res = await fetch(url, {method: 'POST'});
                const result = await res.json();
                if (!res.ok || result.error) {
                    ctx.showToast && ctx.showToast(`Repair failed: ${ctx.escapeHtml(result.error || 'unknown error')}`, '#f85149');
                    return;
                }
                await api.loadIntegrityPanel();
                const after = _integrityData;
                const beforeSummary = before?.summary || {};
                const afterSummary = after?.summary || {};
                const beforeLive = beforeSummary.live || {};
                const afterLive = afterSummary.live || {};
                const beforeArch = beforeSummary.archival || {};
                const afterArch = afterSummary.archival || {};
                const detail = [];
                if (kind === 'head') detail.push(`stale heads ${beforeLive.stale_heads || 0}→${afterLive.stale_heads || 0}`);
                if (kind === 'plans') detail.push(`stale plans ${beforeArch.stale_plans || 0}→${afterArch.stale_plans || 0}`);
                if (kind === 'recovery') detail.push(`continuity gaps ${beforeLive.continuity_gaps || 0}→${afterLive.continuity_gaps || 0}`);
                if (kind === 'recovery') detail.push(`recursive recovery ${beforeLive.recursive_recovery || 0}→${afterLive.recursive_recovery || 0}`);
                if (kind === 'agents') detail.push(`orphaned agents ${beforeLive.orphaned_agents || 0}→${afterLive.orphaned_agents || 0}`);
                const label = projectName ? `Project: ${projectName}` : (ctx.selectedProject ? `Project: ${ctx.selectedProject}` : 'All projects');
                ctx.showToast && ctx.showToast(`<strong>${ctx.escapeHtml(label)}</strong>: ${detail.join(' · ') || 'repair complete'}`, '#e3b341');
                ctx.loadData && ctx.loadData();
            } catch (e) {
                ctx.showToast && ctx.showToast(`Repair failed: ${ctx.escapeHtml(e.message)}`, '#f85149');
            }
        },
        async renderDepsGraph() {
            const ctx = _ctx();
            const container = document.getElementById('depsSvg');
            const badge = document.getElementById('planBadge');
            let dot = '';
            try {
                if (!ctx.selectedProject) {
                    container.textContent = 'Select a project to render its dependency graph. Global graph rendering is disabled for performance.';
                    if (badge) {
                        badge.textContent = '';
                        badge.style.display = 'none';
                    }
                    return;
                }
                const taskData = await fetch(`${ctx.API}/api/tasks`).then(r => r.json());
                const tasks = Array.isArray(taskData.tasks) ? taskData.tasks : [];
                const liveProjectTasks = ctx.selectedProject
                    ? tasks.filter(task => task.project === ctx.selectedProject)
                    : tasks;
                if (ctx.selectedProject && !liveProjectTasks.length) {
                    container.textContent = 'No live tasks found for this project.';
                    if (badge) {
                        badge.textContent = '';
                        badge.style.display = 'none';
                    }
                    return;
                }
                let svg = null;
                let renderedWithHistory = ctx.selectedProject ? _depHistoryLimit : 0;
                let lastRenderError = null;
                const historyCandidates = ctx.selectedProject ? _depRenderHistoryCandidates() : [0];
                for (const historyLimit of historyCandidates) {
                    const historyQuery = `history_depth=${encodeURIComponent(historyLimit)}`;
                    const dotUrl = ctx.selectedProject
                        ? `${ctx.API}/api/dependencies/dot?project=${encodeURIComponent(ctx.selectedProject)}&${historyQuery}`
                        : `${ctx.API}/api/dependencies/dot?${historyQuery}`;
                    const depsRes = await fetch(dotUrl);
                    if (!depsRes.ok) { container.textContent = 'No dependency data.'; return; }
                    const data = await depsRes.json();
                    dot = data.dot || '';
                    if (!dot.trim() || dot.trim() === 'digraph {}') {
                        container.textContent = ctx.selectedProject ? 'No tasks found for this project.' : 'No task dependencies defined.';
                        return;
                    }
                    try {
                        if (!_viz) _viz = new Viz();
                        svg = await _viz.renderSVGElement(dot);
                        renderedWithHistory = historyLimit;
                        lastRenderError = null;
                        break;
                    } catch (renderError) {
                        lastRenderError = renderError;
                        console.error('Dependency graph render attempt failed', {error: renderError, historyLimit, dot});
                        _viz = null;
                        if (!_isVizMemoryError(renderError) || historyLimit === historyCandidates[historyCandidates.length - 1]) {
                            throw renderError;
                        }
                    }
                }
                if (!svg) {
                    throw lastRenderError || new Error('graph renderer returned no SVG');
                }
                svg.style.maxWidth = '100%';
                svg.style.fontFamily = '-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';
                svg.style.overflow = 'visible';
                svg.classList.add('deps-graph-svg');
                svg.setAttribute('overflow', 'visible');
                svg.querySelectorAll('polygon[fill="white"], polygon[fill="#ffffff"]').forEach(el => el.remove());
                svg.querySelectorAll('g.node polygon').forEach(poly => {
                    const pts = (poly.getAttribute('points') || '').trim().split(/\s+/).map(p => p.split(',').map(Number));
                    if (!pts.length) return;
                    const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
                    const x = Math.min(...xs), y = Math.min(...ys);
                    const w = Math.max(...xs) - x, h = Math.max(...ys) - y;
                    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                    rect.setAttribute('x', x);
                    rect.setAttribute('y', y);
                    rect.setAttribute('width', w);
                    rect.setAttribute('height', h);
                    rect.setAttribute('rx', '6');
                    rect.setAttribute('ry', '6');
                    rect.setAttribute('fill', poly.getAttribute('fill'));
                    rect.setAttribute('stroke', poly.getAttribute('stroke'));
                    rect.setAttribute('stroke-width', poly.getAttribute('stroke-width') || '1.5');
                    const sda = poly.getAttribute('stroke-dasharray');
                    if (sda) rect.setAttribute('stroke-dasharray', sda);
                    poly.parentNode.replaceChild(rect, poly);
                });
                svg.setAttribute('style', 'max-width:100%;background:transparent;overflow:visible');
                container.innerHTML = '';
                container.appendChild(svg);
                const historySelect = document.getElementById('depsHistoryLimit');
                if (historySelect && String(renderedWithHistory) !== historySelect.value) {
                    historySelect.value = String(renderedWithHistory);
                }
                const focusTaskIds = _getFocusedDepTaskIds(taskData.tasks || [], ctx.selectedProject);
                const focusBounds = _getDepGraphNodeBoundsByTaskIds(svg, focusTaskIds);
                initDepGraphInteraction(container, svg, {focusBounds});
                if (ctx.selectedProject && badge) {
                    try {
                        const planRes = await fetch(`${ctx.API}/api/plans/${encodeURIComponent(ctx.selectedProject)}`);
                        if (planRes.ok) {
                            const payload = await planRes.json();
                            const latestPlan = Array.isArray(payload.plans) ? payload.plans[0] : null;
                            if (latestPlan && latestPlan.created_at) {
                                const createdAt = new Date(latestPlan.created_at).toLocaleDateString('en-US', {month: 'short', day: 'numeric'});
                                badge.textContent = `Plan: ${createdAt}`;
                                badge.style.display = '';
                            } else {
                                badge.textContent = '';
                                badge.style.display = 'none';
                            }
                        }
                    } catch (e) {
                        badge.textContent = '';
                        badge.style.display = 'none';
                    }
                } else if (badge) {
                    badge.textContent = '';
                    badge.style.display = 'none';
                }
                svg.querySelectorAll('g.node').forEach(nodeEl => {
                    const titleEl = nodeEl.querySelector('title');
                    if (!titleEl) return;
                    const taskId = titleEl.textContent.trim();
                    nodeEl.style.cursor = 'pointer';
                    nodeEl.addEventListener('click', (e) => {
                        if (_depGraphState && _depGraphState.dragMoved) return;
                        e.stopPropagation();
                        openNodeDetail(taskId);
                    });
                });
            } catch (e) {
                const message = (e && (e.message || e.toString())) || 'unknown render error';
                console.error('Dependency graph render failed', {error: e, dot});
                container.textContent = 'Could not render graph: ' + message;
            }
        }
    };

    window.SwarmDepsIntegrityUI = api;
})();
