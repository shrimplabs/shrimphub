/**
 * Tests for dashboard-utils.js — pure helpers with no DOM or global state.
 */
import { readFileSync } from 'fs';
import { resolve } from 'path';
import vm from 'vm';
import { describe, it, expect, beforeAll } from 'vitest';

const ROOT = resolve(import.meta.dirname, '../..');

function loadScript(filename) {
    const code = readFileSync(resolve(ROOT, filename), 'utf8');
    vm.runInThisContext(code, { filename });
}

beforeAll(() => {
    loadScript('dashboard-utils.js');
});

describe('escapeHtml', () => {
    it('escapes ampersand', () => {
        expect(escapeHtml('a&b')).toBe('a&amp;b');
    });
    it('escapes less-than and greater-than', () => {
        expect(escapeHtml('<script>')).toBe('&lt;script&gt;');
    });
    it('passes through plain text unchanged', () => {
        expect(escapeHtml('hello world')).toBe('hello world');
    });
    it('handles empty string', () => {
        expect(escapeHtml('')).toBe('');
    });
    it('handles project names with hyphens and numbers', () => {
        expect(escapeHtml('star-sovereigns-2')).toBe('star-sovereigns-2');
    });
    it('escapes all three special chars in one string', () => {
        expect(escapeHtml('<a href="x&y">')).toBe('&lt;a href="x&amp;y"&gt;');
    });
});

describe('projectLargestFileSize', () => {
    it('returns 0 for empty project', () => {
        expect(projectLargestFileSize({})).toBe(0);
    });
    it('returns 0 for null', () => {
        expect(projectLargestFileSize(null)).toBe(0);
    });
    it('returns max file size', () => {
        const data = { files: { 'a.gd': 100, 'b.gd': 500, 'c.gd': 200 } };
        expect(projectLargestFileSize(data)).toBe(500);
    });
    it('returns single file size', () => {
        const data = { files: { 'main.gd': 42 } };
        expect(projectLargestFileSize(data)).toBe(42);
    });
    it('returns 0 for project with no files key', () => {
        expect(projectLargestFileSize({ status: 'ok' })).toBe(0);
    });
});
