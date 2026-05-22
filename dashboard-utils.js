/**
 * dashboard-utils.js — pure utility helpers for the Swarm dashboard.
 *
 * No DOM access, no global state. Load before dashboard.js and
 * dashboard_closure.js so all modules can use these helpers.
 */

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
