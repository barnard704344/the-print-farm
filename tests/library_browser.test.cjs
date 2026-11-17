const { test } = require('node:test');
const assert = require('node:assert/strict');
global.window = {};
require('../static/library.js');
const browser = window.LibraryBrowser;

test('recognises projects including gcode.3mf separately from gcode', () => {
    const files = ['part.GCODE.3MF', 'part.gcode', 'part.bgcode', 'part.txt']
        .map((original_name, id) => ({ original_name, id }));
    assert.deepEqual(browser.filter(files, '3mf').map(f => f.id), [0]);
    assert.deepEqual(browser.filter(files, 'gcode').map(f => f.id), [1, 2]);
    assert.equal(browser.filter(files, 'all').length, 4);
});

test('pagination has no missing or repeated files and clamps after filtering', () => {
    const files = Array.from({ length: 101 }, (_, id) => ({ id }));
    const pages = [1, 2, 3].flatMap(page => browser.page(files, page).files);
    assert.deepEqual(pages, files);
    assert.equal(browser.page(files.slice(0, 2), 3).current, 1);
    assert.equal(browser.page([], 99).pages, 1);
    assert.deepEqual(browser.page([], 1).files, []);
});
