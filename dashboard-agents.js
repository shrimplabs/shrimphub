

let _activeStream = null;

function closeModal() {
    document.getElementById('outputModal').classList.remove('active');
    if (_activeStream) { _activeStream.close(); _activeStream = null; }
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

        const currentHour = new Date().getHours();

        counts.forEach((count, h) => {
            const barH = count > 0 ? Math.max(2, (count / maxCount) * (H - 8)) : 0;
            const x = h * barW;
            const y = H - barH;
            const intensity = count > 0 ? Math.max(0.3, count / maxCount) : 0.08;
            ctx.fillStyle = count > 0
                ? `rgba(248, 81, 73, ${intensity})`
                : `rgba(33, 38, 45, 0.6)`;
            ctx.fillRect(x + 1, y, barW - 2, barH);
        });

        // Playhead — thin green line at the left edge of the current hour
        const playheadX = currentHour * barW;
        ctx.strokeStyle = '#3fb950';
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(playheadX, 0);
        ctx.lineTo(playheadX, H);
        ctx.stroke();
        // Small triangle at top
        ctx.fillStyle = '#3fb950';
        ctx.beginPath();
        ctx.moveTo(playheadX - 4, 0);
        ctx.lineTo(playheadX + 4, 0);
        ctx.lineTo(playheadX, 6);
        ctx.closePath();
        ctx.fill();

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

