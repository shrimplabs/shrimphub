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
loadGardenerState();
loadMetaModeState();
setInterval(() => { loadData().then(() => { if (_depsVisible) renderDepsGraph(); }); }, 5000);
setInterval(syncAutoMode, 5000);
setInterval(syncProviders, 30000);
setInterval(loadMetrics, 30000);
