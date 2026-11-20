const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const template = fs.readFileSync(path.join(__dirname, '../templates/dashboard.html'), 'utf8');
function extract(name, next) {
    const start = template.indexOf(`        function ${name}(`);
    assert.ok(start >= 0);
    return template.slice(start, template.indexOf(`        function ${next}(`, start));
}
const ctx = vm.createContext({jsArg: JSON.stringify, iconMarkup: () => '', amsSlotLabel: t => `A${t.slot+1}`});
vm.runInContext(extract('amsSourceLabel', 'renderPrinterSubline'), ctx);
vm.runInContext(extract('renderPrinterControls', 'confirmStopPrint'), ctx);

test('slot-2 recovery does not claim the print uses an external spool', () => {
    const label = ctx.amsSourceLabel({ams_tray_now:254,ams_recovery:{busy:true,source_label:'AMS A slot 2',phase:'Purging old filament'}});
    assert.match(label, /slot 2.*Purging/);
    assert.doesNotMatch(label, /external/);
    assert.equal(ctx.amsSourceLabel({ams_tray_now:254}), 'Using external spool');
    assert.equal(ctx.amsSourceLabel({ams_tray_now:1,ams_trays:[{id:1,slot:1}]}), 'Using A2');
});
test('unresolved AMS recovery replaces normal Resume with the correct guarded recovery controls', () => {
    const html = ctx.renderPrinterControls('P1S', {status:'PAUSE_FILAMENT',ams_recovery:{active:true,can_retry:true,can_confirm_extrusion:false}});
    assert.match(html, /Retry AMS/);
    assert.doesNotMatch(html, /printerCmd\([^)]*'resume'/);
    assert.doesNotMatch(html, /Filament is extruding/);
    assert.match(html, /Stop/);
});
test('physical extrusion confirmation appears only when the printer requests it', () => {
    assert.match(ctx.renderPrinterControls('P1S', {status:'PAUSE_FILAMENT',ams_recovery:{active:true,can_confirm_extrusion:true}}), /Filament is extruding — Continue/);
});

test('control failures produce visible feedback instead of a silent success', async () => {
    const toasts = [];
    const c = vm.createContext({apiPost: async () => ({ok:false,message:'Printer acknowledged, but recovery did not start'}),showToast: (...x) => toasts.push(x),refreshPrinters: () => {}});
    const start = template.indexOf('        async function sendFilamentCommand(');
    vm.runInContext(template.slice(start, template.indexOf('        async function amsRecoveryCmd(',start)), c);
    await c.sendFilamentCommand('P1S','/test',{});
    assert.equal(toasts[0][1], 'error');
    assert.match(toasts[0][0], /did not start/);
});

test('manual slot-4 load after FINISH uses the requested target, not transitional external tray', () => {
    const label = ctx.amsSourceLabel({status:'FINISH',ams_tray_now:254,ams_recovery:{busy:true,active:true,target_tray:3,target_label:'AMS A slot 4',source_label:'AMS A slot 2',phase:'Retracting filament'}});
    assert.equal(label, 'Loading AMS A slot 4 — Retracting filament');
    assert.doesNotMatch(label, /external/);
    assert.equal(ctx.amsSourceLabel({ams_tray_now:3,ams_trays:[{id:3,slot:3}],ams_recovery:{busy:false,active:false}}),'Using A4');
});
