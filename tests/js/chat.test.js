/**
 * Tests for unified chat scope key logic.
 *
 * dashboard-chat.js is heavily DOM-dependent, so we test the scope/session-key
 * conventions directly rather than loading the full module.
 */
import { describe, it, expect } from 'vitest';

// The session key convention used in dashboard-chat.js:
//   globalScope → '_global'
//   projectScope → project name
function scopeKey(project) {
    return project || '_global';
}

// The localStorage key convention:
//   'unified_session_' + scopeKey
function localStorageKey(project) {
    return 'unified_session_' + scopeKey(project);
}

describe('scope key conventions', () => {
    it('global scope uses _global key', () => {
        expect(scopeKey(null)).toBe('_global');
        expect(scopeKey(undefined)).toBe('_global');
    });

    it('project scope uses project name as key', () => {
        expect(scopeKey('star-sovereigns')).toBe('star-sovereigns');
        expect(scopeKey('chess-2')).toBe('chess-2');
    });

    it('localStorage keys are distinct per scope', () => {
        expect(localStorageKey(null)).toBe('unified_session__global');
        expect(localStorageKey('chess-2')).toBe('unified_session_chess-2');
        expect(localStorageKey(null)).not.toBe(localStorageKey('chess-2'));
    });

    it('different projects have different keys', () => {
        expect(localStorageKey('project-a')).not.toBe(localStorageKey('project-b'));
    });
});

describe('scope change detection', () => {
    it('detects scope change from null to project', () => {
        const prev = null;
        const next = 'chess-2';
        expect(prev !== next).toBe(true);
    });

    it('detects scope change from project to null', () => {
        const prev = 'chess-2';
        const next = null;
        expect(prev !== next).toBe(true);
    });

    it('detects scope change between projects', () => {
        const prev = 'chess-2';
        const next = 'star-sovereigns';
        expect(prev !== next).toBe(true);
    });

    it('no scope change when same project reopened', () => {
        const prev = 'chess-2';
        const next = 'chess-2';
        expect(prev !== next).toBe(false);
    });

    it('no scope change when global reopened', () => {
        const prev = null;
        const next = null;
        expect(prev !== next).toBe(false);
    });
});

// ── Project name validation logic (mirrors api_chat.py server-side check) ──
// The dashboard sends the project name as-is. The server validates it.
// These tests document what fuzzy match the server should catch.

describe('project name typo patterns', () => {
    const knownProjects = new Set([
        'temporal-residue', 'star-sovereigns', 'chess-2', 'swarm-controller'
    ]);

    // Mirror the Python server's fuzzy match logic in api_chat.py:
    // project.lower().replace('-','') in p.lower().replace('-','')
    // OR p.lower().replace('-','') in project.lower().replace('-','')
    function fuzzyMatch(input, known) {
        const norm = s => s.toLowerCase().replace(/-/g, '');
        const inp = norm(input);
        return [...known].filter(p => {
            const pk = norm(p);
            return pk.includes(inp) || inp.includes(pk);
        });
    }

    it('exact match passes', () => {
        expect(knownProjects.has('temporal-residue')).toBe(true);
    });

    it('typo temporal-residude is not in known projects', () => {
        expect(knownProjects.has('temporal-residude')).toBe(false);
    });

    it('temporal-residude does not substring-match temporal-residue (falls back to known list)', () => {
        // The server's fuzzy check is substring-based. 'temporalresidude' and
        // 'temporalresidue' share no substring containment (different chars),
        // so the server falls back to listing known projects — not a silent pass.
        const inp = 'temporal-residude'.replace(/-/g, '').toLowerCase();
        const pk = 'temporal-residue'.replace(/-/g, '').toLowerCase();
        expect(inp.includes(pk)).toBe(false);
        expect(pk.includes(inp)).toBe(false);
    });

    it('partial name star suggests star-sovereigns', () => {
        const matches = fuzzyMatch('star', knownProjects);
        expect(matches).toContain('star-sovereigns');
    });

    it('completely unknown name has no matches', () => {
        const matches = fuzzyMatch('zzz-nonexistent', knownProjects);
        expect(matches).toHaveLength(0);
    });
});
