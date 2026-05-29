(function () {
    let _contextProvider = null;
    let _viz = null;
    let _depHistoryLimit = 2;
    let _depGraphState = null;
    let _depGraphViewByProject = {};
    let _integrityData = null;

    // Graph render state for loading-indicator retry UX
    let _graphRenderState = null; // { retries: 0, abortCtrl: null }

    // Playback state
    let _pbEvents = null;       // array of event objects from /api/dependencies/playback-events
    let _pbIndex = 0;           // current event index
    let _pbPlaying = false;
    let _pbTimer = null;
    let _pbProject = null;      // project for which events were loaded

    function _ctx() {
        return _contextProvider ? _contextProvider() : {};
    }

    // Color maps: DOT-generated hardcoded hex → theme-aware replacements.
    // Keys are the exact fillcolor/fontcolor values from dependencies.py + api_deps.py.
    const _DOT_COLOR_MAP = {
        dark: {
            // These are already the dark-mode values -- keep as-is
        },
        light: {
            // node fill remaps
            '#30363d': '#e8edf2',   // pending fill: dark grey → light grey
            '#0d1117': '#f0f4f8',   // ghost / in_progress bg fill → near-white
            '#238636': '#2da44e',   // completed fill (ok as-is, slightly lighter)
            '#da3633': '#d1242f',   // failed fill (ok)
            // font remaps (what was readable on dark is invisible on light)
            '#c9d1d9': '#24292f',   // pending text: light grey → near-black
            '#ffffff': '#24292f',   // completed/failed white text → near-black
            '#f0e68c': '#7a6500',   // ghost HEAD khaki text → dark gold
            // edge/stroke colors
            '#8b949e': '#57606a',   // default edge: medium grey → darker
            '#3fb950': '#1a7f37',   // ghost ok text/border → darker green
            '#d4af37': '#9a6700',   // ghost HEAD border gold → darker
            '#f0883e': '#bc4c00',   // in_progress orange → darker
            '#58a6ff': '#0969da',   // history edge blue → darker blue
        }
    };

    function _remapDepGraphColors(svg) {
        const theme = document.body.getAttribute('data-theme') || 'cyberpunk';
        if (theme !== 'light') return; // dark/cyberpunk already look correct

        const map = _DOT_COLOR_MAP.light;

        function remapAttr(el, attr) {
            const val = (el.getAttribute(attr) || '').toLowerCase().trim();
            if (map[val]) el.setAttribute(attr, map[val]);
        }

        // Remap fill/stroke/fontcolor on all SVG elements
        svg.querySelectorAll('[fill], [stroke], [color]').forEach(el => {
            remapAttr(el, 'fill');
            remapAttr(el, 'stroke');
            remapAttr(el, 'color');
        });

        // Also remap text fill (SVG text uses fill for color)
        svg.querySelectorAll('text').forEach(el => {
            remapAttr(el, 'fill');
            // Inline style fill
            const style = el.getAttribute('style') || '';
            if (style.includes('fill:')) {
                const newStyle = style.replace(/fill:\s*(#[0-9a-fA-F]{6})/gi, (_, hex) => {
                    return 'fill:' + (map[hex.toLowerCase()] || hex);
                });
                el.setAttribute('style', newStyle);
            }
        });

        // Edge paths
        svg.querySelectorAll('g.edge path, g.edge polygon').forEach(el => {
            remapAttr(el, 'stroke');
            remapAttr(el, 'fill');
        });

        // Cluster/subgraph labels
        svg.querySelectorAll('g.cluster text').forEach(el => {
            remapAttr(el, 'fill');
        });
        svg.querySelectorAll('g.cluster polygon, g.cluster rect').forEach(el => {
            remapAttr(el, 'stroke');
        });
    }

    function _depRenderHistoryCandidates() {
        const seed = [Number(_depHistoryLimit) || 2, 2, 1, 0];
        return [...new Set(seed.filter(n => Number.isFinite(n) && n >= 0))];
    }

    function _postProcessDepSvg(svg) {
        svg.removeAttribute('width');
        svg.removeAttribute('height');
        svg.style.width = '100%';
        svg.style.height = '100%';
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
            rect.setAttribute('x', x); rect.setAttribute('y', y);
            rect.setAttribute('width', w); rect.setAttribute('height', h);
            rect.setAttribute('rx', '6'); rect.setAttribute('ry', '6');
            rect.setAttribute('fill', poly.getAttribute('fill'));
            rect.setAttribute('stroke', poly.getAttribute('stroke'));
            rect.setAttribute('stroke-width', poly.getAttribute('stroke-width') || '1.5');
            const sda = poly.getAttribute('stroke-dasharray');
            if (sda) rect.setAttribute('stroke-dasharray', sda);
            poly.parentNode.replaceChild(rect, poly);
        });
        svg.setAttribute('style', 'width:100%;height:100%;background:transparent;overflow:visible');
        _remapDepGraphColors(svg);
    }

    function _isVizMemoryError(error) {
        const message = (error && (error.message || error.toString()) || '').toLowerCase();
        return message.includes('cannot enlarge memory arrays') || message.includes('out of memory') || message.includes('memory');
    }

    const _RENDER_MAX_RETRIES = 3;
    const _RENDER_TIMEOUT_MS  = 30000;

    function _showGraphLoading(container) {
        _hideGraphLoading(container);
        const el = document.createElement('div');
        el.id = 'graphLoadingOverlay';
        el.textContent = 'Rendering graph...';
        el.style.cssText = [
            'position:absolute','inset:0','display:flex','align-items:center',
            'justify-content:center','font-size:13px','color:#8b949e',
            'background:rgba(13,17,23,0.55)','border-radius:6px','z-index:10',
            'pointer-events:none',
        ].join(';');
        container.style.position = 'relative';
        container.appendChild(el);
    }

    function _hideGraphLoading(container) {
        const el = container && document.getElementById('graphLoadingOverlay');
        if (el) el.remove();
    }

    function _isRenderAborted() {
        return _graphRenderState && _graphRenderState._abortCtrl &&
            _graphRenderState._abortCtrl.signal.aborted;
    }

    async function _renderWithRetry(dot) {
        _graphRenderState = { _abortCtrl: new AbortController() };
        const ac = _graphRenderState._abortCtrl;
        const startedAt = Date.now();

        for (let attempt = 0; attempt < _RENDER_MAX_RETRIES; attempt++) {
            if (ac.signal.aborted) return null;
            if (Date.now() - startedAt > _RENDER_TIMEOUT_MS) {
                if (!ac.signal.aborted) ac.abort();
                throw new Error('graph render timed out after 30 s');
            }
            if (_viz) { try { _viz.destroy(); } catch (_) {} }
            _viz = new Viz();
            try {
                return await _viz.renderSVGElement(dot);
            } catch (err) {
                if (ac.signal.aborted) return null;
                const memory = _isVizMemoryError(err);
                if (memory && attempt < _RENDER_MAX_RETRIES - 1) {
                    console.warn('Graph render WASM memory error, attempt ' + (attempt + 1) + ', retrying');
                    _viz = null;
                    await new Promise(function(resolve) { setTimeout(resolve, 200); });
                    continue;
                }
                throw err;
            }
        }
        return null;
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
        _updateMinimap();
    }

    // ── Minimap ──────────────────────────────────────────────────────────────

    const MINIMAP_W = 160;
    const MINIMAP_H = 110;

    function _ensureMinimap(container) {
        let mm = container.querySelector('.deps-minimap');
        if (mm) return mm;

        mm = document.createElement('div');
        mm.className = 'deps-minimap';
        mm.title = 'Minimap -- click or drag to pan';
        mm.innerHTML = `
            <svg class="deps-minimap-svg" xmlns="http://www.w3.org/2000/svg"
                 width="${MINIMAP_W}" height="${MINIMAP_H}">
                <g class="deps-minimap-graph"></g>
                <rect class="deps-minimap-viewport" rx="2"/>
            </svg>`;

        // Drag-to-pan on minimap
        let mmDragging = false;
        function mmPanTo(e) {
            if (!_depGraphState) return;
            const rect = mm.getBoundingClientRect();
            const mx = (e.clientX - rect.left) / MINIMAP_W;
            const my = (e.clientY - rect.top) / MINIMAP_H;
            const graphBounds = _depGraphState.graphRoot.getBBox();
            const containerRect = _depGraphState.container.getBoundingClientRect();
            // Centre the view on the clicked graph point
            const gx = graphBounds.x + mx * graphBounds.width;
            const gy = graphBounds.y + my * graphBounds.height;
            _depGraphState.translateX = containerRect.width / 2 - gx * _depGraphState.scale;
            _depGraphState.translateY = containerRect.height / 2 - gy * _depGraphState.scale;
            applyDepGraphTransform();
            saveDepGraphView();
        }
        mm.addEventListener('mousedown', e => { mmDragging = true; mmPanTo(e); e.preventDefault(); });
        window.addEventListener('mousemove', e => { if (mmDragging) mmPanTo(e); });
        window.addEventListener('mouseup', () => { mmDragging = false; });

        container.appendChild(mm);
        return mm;
    }

    function _updateMinimap() {
        if (!_depGraphState) return;
        const container = _depGraphState.container;
        const mm = _ensureMinimap(container);
        const mmSvg = mm.querySelector('.deps-minimap-svg');
        const mmGroup = mm.querySelector('.deps-minimap-graph');
        const mmViewport = mm.querySelector('.deps-minimap-viewport');

        // Get full graph bounds in graph-space
        let graphBounds;
        try { graphBounds = _depGraphState.graphRoot.getBBox(); } catch(e) { return; }
        if (!graphBounds.width || !graphBounds.height) return;

        // Scale to fit the minimap
        const mmScale = Math.min(MINIMAP_W / graphBounds.width, MINIMAP_H / graphBounds.height) * 0.92;
        const mmOffX = (MINIMAP_W - graphBounds.width * mmScale) / 2 - graphBounds.x * mmScale;
        const mmOffY = (MINIMAP_H - graphBounds.height * mmScale) / 2 - graphBounds.y * mmScale;

        // Sync the minimap graph content from the main graph (clone nodes only, no labels for perf)
        const mainGroup = _depGraphState.graphRoot;
        if (mmGroup._lastSource !== mainGroup || mmGroup._lastChildCount !== mainGroup.children.length) {
            mmGroup._lastSource = mainGroup;
            mmGroup._lastChildCount = mainGroup.children.length;
            mmGroup.innerHTML = '';
            // Shallow-clone each node's shape (polygon/ellipse) -- skip text for perf
            mainGroup.querySelectorAll('g.node').forEach(node => {
                const shape = node.querySelector('polygon,ellipse,rect,circle');
                if (!shape) return;
                const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
                g.setAttribute('transform', node.getAttribute('transform') || '');
                const s = shape.cloneNode(false);
                // Preserve fill colour (status colour) but drop stroke detail
                s.setAttribute('stroke-width', '0.5');
                g.appendChild(s);
                mmGroup.appendChild(g);
            });
            // Clone edges (paths) at low detail
            mainGroup.querySelectorAll('g.edge path').forEach(path => {
                const p = path.cloneNode(false);
                p.setAttribute('stroke-width', '0.5');
                p.setAttribute('fill', 'none');
                mmGroup.insertBefore(p, mmGroup.firstChild);
            });
        }

        mmGroup.setAttribute('transform', `translate(${mmOffX},${mmOffY}) scale(${mmScale})`);

        // Draw viewport rectangle: what's visible in the main container
        const containerRect = container.getBoundingClientRect();
        // Top-left of container in graph-space
        const visLeft   = (-_depGraphState.translateX) / _depGraphState.scale;
        const visTop    = (-_depGraphState.translateY) / _depGraphState.scale;
        const visRight  = visLeft + containerRect.width / _depGraphState.scale;
        const visBottom = visTop  + containerRect.height / _depGraphState.scale;

        // Map to minimap pixels
        const rx = visLeft   * mmScale + mmOffX;
        const ry = visTop    * mmScale + mmOffY;
        const rw = (visRight  - visLeft) * mmScale;
        const rh = (visBottom - visTop)  * mmScale;

        mmViewport.setAttribute('x', rx);
        mmViewport.setAttribute('y', ry);
        mmViewport.setAttribute('width',  Math.max(4, rw));
        mmViewport.setAttribute('height', Math.max(4, rh));
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
        let task = taskDataMap[taskId];
        if (!task) {
            // Not in the local map (e.g. completed/history node) — fetch directly
            try {
                const res = await fetch(`${ctx.API}/api/tasks/${encodeURIComponent(taskId)}`);
                if (res.ok) task = await res.json();
            } catch (_) {}
        }
        if (!task) return;
        // Active agents: open live log
        if (task.status === 'in_progress') {
            const agent = agentByTaskId[taskId];
            if (agent && ctx.showAgentOutput) {
                ctx.showAgentOutput(agent.id, task.project, true, taskId);
                return;
            }
        }
        // Completed/failed/cancelled: read-only info panel
        if (task.status === 'completed' || task.status === 'failed' || task.status === 'cancelled') {
            if (typeof openTaskInfoPanel === 'function') {
                openTaskInfoPanel(task);
                return;
            }
        }
        // Pending: editable
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
            subtitle.textContent = `${scopeLabel} * ${live.problem_count || 0} live issues * ${archival.problem_count || 0} archival issues`;
            const chips = [
                ['Live issues', live.problem_count || 0, `${live.stale_heads || 0} heads * ${live.orphaned_agents || 0} agents`],
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
                        <span class="integrity-detail-meta">${escapeHtml(item.head_task_id || 'missing')} * ${escapeHtml(item.reason || 'invalid')}</span>
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
                        <span class="integrity-detail-meta">${escapeHtml(item.plan_id || 'unknown plan')} * ${escapeHtml((item.reasons || []).join(', ') || 'stale')}</span>
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
            if (subtitle) subtitle.textContent = 'Loading diagnostics...';
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
                ctx.showToast && ctx.showToast(`<strong>${ctx.escapeHtml(label)}</strong>: ${detail.join(' * ') || 'repair complete'}`, '#e3b341');
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
                const taskData = await fetch(`${ctx.API}/api/tasks`).then(r => r.json());
                const tasks = Array.isArray(taskData.tasks) ? taskData.tasks : [];
                const liveProjectTasks = ctx.selectedProject
                    ? tasks.filter(task => task.project === ctx.selectedProject)
                    : tasks;
                // Global graph: warn if very large but still render
                if (!ctx.selectedProject && liveProjectTasks.length > 500) {
                    container.textContent = `Too many tasks to render globally (${liveProjectTasks.length}). Select a project.`;
                    return;
                }
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
                        container.textContent = ctx.selectedProject ? 'No tasks found for this project.' : 'No pending tasks with dependencies.';
                        return;
                    }
                    try {
                        _showGraphLoading(container);
                        svg = await _renderWithRetry(dot);
                        if (svg === null && _isRenderAborted()) {
                            // Render was aborted (e.g. user switched tabs); bail out silently
                            _hideGraphLoading(container);
                            return;
                        }
                        renderedWithHistory = historyLimit;
                        lastRenderError = null;
                        break;
                    } catch (renderError) {
                        _hideGraphLoading(container);
                        lastRenderError = renderError;
                        console.error('Dependency graph render attempt failed', {error: renderError, historyLimit, dot});
                        _viz = null;
                        if (!_isVizMemoryError(renderError) || historyLimit === historyCandidates[historyCandidates.length - 1]) {
                            throw renderError;
                        }
                    }
                }
                if (!svg) {
                    _hideGraphLoading(container);
                    throw lastRenderError || new Error('graph renderer returned no SVG');
                }
                _postProcessDepSvg(svg);
                _hideGraphLoading(container);
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
                _hideGraphLoading(container);
                container.textContent = 'Could not render graph: ' + message;
            }
        }
    };

    // ---------- Dep Graph Playback ----------

    function _fmtPlaybackTs(iso) {
        if (!iso) return '--';
        try {
            const d = new Date(iso);
            return d.toLocaleDateString('en-US', {month: 'short', day: 'numeric', year: 'numeric'})
                + ' ' + d.toLocaleTimeString('en-US', {hour: '2-digit', minute: '2-digit'});
        } catch (e) { return iso; }
    }

    function _pbStop() {
        if (_pbTimer) { clearTimeout(_pbTimer); _pbTimer = null; }
        _pbPlaying = false;
        const btn = document.getElementById('playbackPlayBtn');
        if (btn) btn.textContent = '▶';
    }

    async function _pbRenderAt(idx) {
        if (!_pbEvents || !_pbEvents.length) return;
        idx = Math.max(0, Math.min(idx, _pbEvents.length - 1));
        _pbIndex = idx;
        const ev = _pbEvents[idx];
        const ctx = _ctx();

        // Update scrubber
        const scrubber = document.getElementById('playbackScrubber');
        if (scrubber) scrubber.value = idx;

        // Update timestamp label
        const tsLabel = document.getElementById('playbackTimestamp');
        if (tsLabel) tsLabel.textContent = _fmtPlaybackTs(ev.iso);

        // Update callout
        const calloutEl = document.getElementById('playbackCallout');
        if (calloutEl) {
            const calloutLabels = {
                first_task: '🚀 First task created',
                first_completion: '✅ First completion',
                sprint_burst: '⚡ Sprint burst -- 3+ completions in 60s',
                recovery_spawn: '🔄 Recovery task spawned',
            };
            if (ev.callout && calloutLabels[ev.callout]) {
                calloutEl.textContent = calloutLabels[ev.callout] + ' -- ' + ev.label;
                calloutEl.style.display = '';
            } else {
                calloutEl.style.display = 'none';
            }
        }

        // Fetch and render dot-at
        const container = document.getElementById('depsSvg');
        try {
            const res = await fetch(`${ctx.API}/api/dependencies/dot-at?project=${encodeURIComponent(_pbProject)}&at=${encodeURIComponent(ev.iso)}`);
            if (!res.ok) return;
            const data = await res.json();
            const dot = data.dot || '';
            if (!dot.trim() || dot.trim() === 'digraph {}') {
                container.textContent = 'No tasks at this point in time.';
                return;
            }
            const svg = await _renderWithRetry(dot);
            if (svg === null) return;
            _postProcessDepSvg(svg);
            container.innerHTML = '';
            container.appendChild(svg);
            initDepGraphInteraction(container, svg, {});
        } catch (e) {
            console.error('Playback render error', e);
        }
    }

    function _pbStep() {
        if (!_pbPlaying) return;
        if (_pbIndex >= _pbEvents.length - 1) {
            _pbStop();
            return;
        }
        _pbRenderAt(_pbIndex + 1);

        // Pause at callouts for 2s
        const ev = _pbEvents[_pbIndex];
        const speed = parseFloat(document.getElementById('playbackSpeed')?.value || '2');
        const delay = ev.callout ? 2000 : Math.max(200, 800 / speed);

        _pbTimer = setTimeout(_pbStep, delay);
    }

    window.toggleDepPlayback = async function () {
        const ctx = _ctx();
        if (!ctx.selectedProject) return;

        const bar = document.getElementById('depsPlaybackBar');
        if (!bar) return;

        // If already in playback mode for this project, just toggle play/pause
        if (_pbEvents && _pbProject === ctx.selectedProject && bar.style.display !== 'none') {
            window.depPlaybackPlayPause();
            return;
        }

        // Load events
        _pbStop();
        _pbProject = ctx.selectedProject;
        bar.style.display = '';
        const scrubber = document.getElementById('playbackScrubber');
        const tsLabel = document.getElementById('playbackTimestamp');
        if (tsLabel) tsLabel.textContent = 'Loading...';

        try {
            const res = await fetch(`${ctx.API}/api/dependencies/playback-events?project=${encodeURIComponent(ctx.selectedProject)}`);
            if (!res.ok) { bar.style.display = 'none'; return; }
            const data = await res.json();
            _pbEvents = data.events || [];
            if (!_pbEvents.length) {
                bar.style.display = 'none';
                return;
            }

            // Configure scrubber range
            if (scrubber) {
                scrubber.min = 0;
                scrubber.max = _pbEvents.length - 1;
                scrubber.value = 0;
            }

            // Render markers for callout events
            const markersEl = document.getElementById('playbackMarkers');
            if (markersEl) {
                markersEl.innerHTML = '';
                _pbEvents.forEach((ev, i) => {
                    if (!ev.callout) return;
                    const pct = (i / (_pbEvents.length - 1)) * 100;
                    const m = document.createElement('div');
                    m.className = `playback-marker ${ev.callout}`;
                    m.style.left = pct + '%';
                    m.title = ev.label;
                    m.onclick = () => { _pbStop(); _pbRenderAt(i); };
                    markersEl.appendChild(m);
                });
            }

            _pbIndex = 0;
            await _pbRenderAt(0);
        } catch (e) {
            console.error('Failed to load playback events', e);
            bar.style.display = 'none';
        }
    };

    window.depPlaybackPlayPause = function () {
        if (_pbPlaying) {
            _pbStop();
        } else {
            _pbPlaying = true;
            const btn = document.getElementById('playbackPlayBtn');
            if (btn) btn.textContent = '⏸';
            _pbStep();
        }
    };

    window.depPlaybackScrub = function (val) {
        _pbStop();
        _pbRenderAt(parseInt(val, 10));
    };

    window.depPlaybackExit = function () {
        _pbStop();
        _pbEvents = null;
        _pbProject = null;
        const bar = document.getElementById('depsPlaybackBar');
        if (bar) bar.style.display = 'none';
        const calloutEl = document.getElementById('playbackCallout');
        if (calloutEl) calloutEl.style.display = 'none';
        // Re-render live graph
        api.renderDepsGraph();
    };

    // Show/hide the Playback button based on whether a project is selected
    const _origRender = api.renderDepsGraph.bind(api);
    api.renderDepsGraph = async function () {
        const ctx = _ctx();
        const btn = document.getElementById('playbackToggleBtn');
        if (btn) btn.style.display = ctx.selectedProject ? '' : 'none';
        // If playback is active for a different project, exit it
        if (_pbEvents && _pbProject && ctx.selectedProject !== _pbProject) {
            window.depPlaybackExit();
        }
        return _origRender();
    };

    window.SwarmDepsIntegrityUI = api;
})();
