const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const template = fs.readFileSync(path.join(__dirname, '../templates/dashboard.html'), 'utf8');
const start = template.indexOf('        async function checkForUpdates()');
const end = template.indexOf('        checkAuth().then', start);

for (const [label, payload, expected] of [
    ['server message', {ok: false, message: 'Staff login required'}, 'Staff login required'],
    ['server error', {ok: false, error: 'Request origin rejected'}, 'Request origin rejected'],
    ['missing error text', {ok: false}, 'Update request failed (HTTP 403)'],
]) {
    test(`updater displays ${label} for check and apply`, async () => {
        const elements = {};
        const context = vm.createContext({
            BASE: '/the-print-farm',
            document: {getElementById: id => elements[id] ||= {style: {}, innerHTML: ''}},
            fetch: async () => ({status: 403, json: async () => payload}),
            confirm: () => true,
            escapeHtml: text => String(text),
        });
        vm.runInContext(template.slice(start, end), context);
        await context.checkForUpdates();
        assert.ok(elements.updateCheckResult.innerHTML.includes(expected));
        await context.applyUpdate();
        assert.ok(elements.updateCheckResult.innerHTML.includes(expected));
        assert.equal(elements.btnApplyUpdate.disabled, false);
    });
}
