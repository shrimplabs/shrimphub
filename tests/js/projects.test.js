/**
 * Tests for project sorting and visibility logic in dashboard-projects.js.
 */
import { readFileSync } from 'fs';
import { resolve } from 'path';
import vm from 'vm';
import { describe, it, expect, beforeAll, beforeEach } from 'vitest';

const ROOT = resolve(import.meta.dirname, '../..');

function loadScript(filename) {
    const code = readFileSync(resolve(ROOT, filename), 'utf8');
    vm.runInThisContext(code, { filename });
}

beforeAll(() => {
    // Globals that dashboard-projects.js reads at load time
    globalThis.localStorage = { getItem: () => null, setItem: () => {} };
    globalThis.maxLines = 5000;
    globalThis._sidebarTaskCounts = {};
    globalThis._pausedProjects = new Set();
    globalThis._activeProjectSet = new Set();
    globalThis._projectTokenMap = {};
    globalThis._autoReplanProjects = new Set();
    globalThis._projectHealthCache = {};
    globalThis._projectClosureCache = {};
    globalThis.document = {
        getElementById: () => null,
        addEventListener: () => {},
        body: { setAttribute: () => {}, getAttribute: () => null },
    };

    loadScript('dashboard-utils.js');
    loadScript('dashboard-projects.js');
});

beforeEach(() => {
    // Assign directly into the vm context (not globalThis) so the module's
    // let-bindings are updated. vm.runInThisContext shares scope with the test file.
    vm.runInThisContext(`
        _projectSortMode = 'default';
        _selectedProject = null;
        _recentProjectsOnly = false;
        _projectRecentWindowDays = 7;
        _sidebarTaskCounts = {};
    `);
});

// ── sortProjectEntries ───────────────────────────────────────────────────────

const makeEntry = (name, largestFile = 0) =>
    [name, { files: largestFile ? { 'main.gd': largestFile } : {} }];

describe('sortProjectEntries', () => {
    it('sorts alphabetically in name mode', () => {
        vm.runInThisContext(`_projectSortMode = 'name'`);
        const entries = [makeEntry('zebra'), makeEntry('alpha'), makeEntry('mango')];
        const result = sortProjectEntries(entries, {}, {});
        expect(result.map(([n]) => n)).toEqual(['alpha', 'mango', 'zebra']);
    });

    it('sorts by largest file descending in largest mode', () => {
        vm.runInThisContext(`_projectSortMode = 'largest'`);
        const entries = [makeEntry('small', 100), makeEntry('huge', 9000), makeEntry('medium', 500)];
        const result = sortProjectEntries(entries, {}, {});
        expect(result.map(([n]) => n)).toEqual(['huge', 'medium', 'small']);
    });

    it('default: projects with active tasks float to top', () => {
        vm.runInThisContext(`_projectSortMode = 'default'`);
        const entries = [makeEntry('idle'), makeEntry('busy'), makeEntry('also-idle')];
        const result = sortProjectEntries(entries, { busy: 3 }, {});
        expect(result[0][0]).toBe('busy');
    });

    it('default: breaks ties by recent activity', () => {
        vm.runInThisContext(`_projectSortMode = 'default'`);
        const now = Date.now();
        const entries = [makeEntry('old'), makeEntry('new')];
        const activityMap = { new: now, old: now - 1000 };
        const result = sortProjectEntries(entries, {}, activityMap);
        expect(result[0][0]).toBe('new');
    });

    it('does not mutate the original array', () => {
        vm.runInThisContext(`_projectSortMode = 'name'`);
        const entries = [makeEntry('b'), makeEntry('a')];
        const firstBefore = entries[0][0];
        sortProjectEntries(entries, {}, {});
        expect(entries[0][0]).toBe(firstBefore);
    });
});

// ── getVisibleProjectEntries ─────────────────────────────────────────────────

describe('getVisibleProjectEntries', () => {
    it('returns all when no project selected and recentOnly=false', () => {
        vm.runInThisContext(`_selectedProject = null; _recentProjectsOnly = false`);
        const entries = [makeEntry('a'), makeEntry('b'), makeEntry('c')];
        expect(getVisibleProjectEntries(entries, {}).length).toBe(3);
    });

    it('filters to only the selected project', () => {
        vm.runInThisContext(`_selectedProject = 'b'`);
        const entries = [makeEntry('a'), makeEntry('b'), makeEntry('c')];
        const result = getVisibleProjectEntries(entries, {});
        expect(result.length).toBe(1);
        expect(result[0][0]).toBe('b');
    });

    it('selected project overrides recentOnly filter', () => {
        vm.runInThisContext(`_selectedProject = 'stale'; _recentProjectsOnly = true`);
        const entries = [makeEntry('stale')];
        expect(getVisibleProjectEntries(entries, {}).length).toBe(1);
    });

    it('recentOnly: hides projects with no recent activity and no tasks', () => {
        vm.runInThisContext(`
            _selectedProject = null;
            _recentProjectsOnly = true;
            _projectRecentWindowDays = 7;
            _sidebarTaskCounts = {};
        `);
        const now = Date.now();
        const activityMap = {
            recent: now - 1000,
            stale: now - 30 * 86400000,
        };
        const entries = [makeEntry('recent'), makeEntry('stale')];
        const result = getVisibleProjectEntries(entries, activityMap);
        expect(result.map(([n]) => n)).toEqual(['recent']);
    });

    it('recentOnly: keeps stale project if it has pending/in-progress tasks', () => {
        vm.runInThisContext(`
            _selectedProject = null;
            _recentProjectsOnly = true;
            _projectRecentWindowDays = 7;
            _sidebarTaskCounts = { stale: 2 };
        `);
        const activityMap = { stale: Date.now() - 30 * 86400000 };
        const entries = [makeEntry('stale')];
        expect(getVisibleProjectEntries(entries, activityMap).length).toBe(1);
    });
});
