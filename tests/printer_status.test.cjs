const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const template = fs.readFileSync(path.join(__dirname, '../templates/dashboard.html'), 'utf8');
const code = template.slice(template.indexOf('        function printerCardStatus('), template.indexOf('        function renderPrinterStatus('));
const context = vm.createContext({ hmsCodeLabel: entry => 'HMS_' + entry.code });
vm.runInContext(code, context);
const state = p => context.printerCardStatus({ connected: true, type: 'bambulab', ...p });

test('finished prints tell the operator to clear the plate; cleared printers are ready', () => {
    assert.equal(state({status:'FINISH',plate_needs_clear:true}).label, 'Finished · clear plate');
    assert.equal(state({status:'FINISH',plate_needs_clear:true}).tone, 'paused');
    assert.equal(state({status:'FINISH',display_status:'READY'}).label, 'Ready to print');
    assert.equal(state({status:'IDLE'}).label, 'Ready to print');
});
test('print progress and paused filament states remain clear', () => {
    assert.equal(state({status:'RUNNING'}).label, 'Printing');
    assert.equal(state({status:'PAUSED'}).label, 'Paused');
    assert.equal(state({status:'PAUSE_FILAMENT'}).label, 'Paused · check filament');
    assert.equal(state({status:'FAILED',plate_needs_clear:true}).label, 'Print failed');
});
test('health alerts combine with the print state and override the ready colour', () => {
    const result = state({status:'IDLE',hms:[{code:123}],print_error:42});
    assert.equal(result.label, 'Needs attention · 2 alerts');
    assert.equal(result.tone, 'failed');
    assert.match(result.title, /HMS_123/);
    assert.match(result.title, /0000002A/);
});
test('Klipper errors are preserved in the combined status', () => {
    const result = state({status:'FAILED',type:'klipper',error_message:'Heater not heating'});
    assert.equal(result.label, 'Print failed · 1 alert');
    assert.match(result.title, /Heater not heating/);
});
test('offline health is unknown even with stale errors; unrecognised states are not OK', () => {
    const result = state({connected:false,status:'RUNNING',hms:[{code:123}]});
    assert.equal(result.label, 'Offline');
    assert.equal(result.tone, 'unknown');
    assert.doesNotMatch(result.title, /No reported/);
    assert.equal(state({status:'UNKNOWN'}).label, 'Status unknown');
});
