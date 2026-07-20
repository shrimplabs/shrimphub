// Bootstrap: wire up context provider and start polling.
// This runs after all module scripts are loaded.

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

function refreshAll() { loadData().then(() => { if (_depsVisible) renderDepsGraph(); }); }
loadData();
_probeHeadroom();
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
loadHumanReviewFlag();
loadAutoReplan();
loadMetrics();
loadWebhook();
syncVisionProviders();
loadPausedProjects();
loadGardenerState();
loadMetaModeState();
// Graph is NOT in the auto-refresh loop — re-renders only on tab open, project switch,
// or manual refresh. DOT debounce handles the case where nothing changed.
setInterval(() => { loadData(); }, 5000);
setInterval(syncAutoMode, 5000);
setInterval(syncProviders, 30000);
setInterval(loadMetrics, 30000);
setInterval(() => { if (typeof loadAnalytics === 'function') loadAnalytics(); }, 30000);
setInterval(_probeHeadroom, 30000);
