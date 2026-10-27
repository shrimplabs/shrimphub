/**
 * dashboard-utils.js — pure utility helpers for the Swarm dashboard.
 *
 * No DOM access, no global state. Load before dashboard.js and
 * dashboard_closure.js so all modules can use these helpers.
 */

/**
 * Operator token for controllers running with agent_api_enforcement="strict".
 * Set once from the browser console:
 *     localStorage.setItem('swarm_operator_token', '<config.json operator_token>')
 * Every same-origin /api/ fetch then carries X-Swarm-Operator-Token. No-op when
 * unset (default "header" mode needs nothing).
 */
(function installOperatorTokenFetch() {
    if (typeof window === 'undefined' || !window.fetch || window.__swarmOperatorFetchInstalled) return;
    window.__swarmOperatorFetchInstalled = true;
    const _fetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
        let token = '';
        try { token = localStorage.getItem('swarm_operator_token') || ''; } catch (e) { token = ''; }
        if (!token) return _fetch(input, init);
        const url = (typeof input === 'string') ? input : (input && input.url) || '';
        if (url.indexOf('/api/') === -1) return _fetch(input, init);
        const opts = Object.assign({}, init || {});
        const headers = new Headers(opts.headers || (input && input.headers) || {});
        if (!headers.has('X-Swarm-Operator-Token')) headers.set('X-Swarm-Operator-Token', token);
        opts.headers = headers;
        return _fetch(input, opts);
    };
})();

/**
 * Escape HTML special characters for safe insertion into innerHTML.
 * @param {string} str
 * @returns {string}
 */
function escapeHtml(str) {
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/**
 * Return the largest file size (in bytes) from a project's files map.
 * Returns 0 if the project has no file data.
 * @param {object} data — project data object (has a `files` property)
 * @returns {number}
 */
function projectLargestFileSize(data) {
    const sizes = Object.values((data && data.files) || {});
    return sizes.length ? Math.max(...sizes) : 0;
}
