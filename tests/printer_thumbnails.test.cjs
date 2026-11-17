const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../templates/dashboard.html'), 'utf8');
const code = template.slice(template.indexOf('        function printerHasAms('), template.indexOf('        const SPEED_LEVELS'));
const context = vm.createContext({BASE:'/farm',jobThumbKey:p=>p.job||'',htmlEscape:s=>s,iconMarkup:()=>'<svg></svg>'});
vm.runInContext(code,context);
const printer = {type:'bambulab',printer_image:'printers/bambu-p1s.png',printer_model_name:'P1S'};

test('AMS detection uses reported hardware rather than the name',()=>{
    assert.equal(context.printerHasAms({...printer,name:'P1S-AMS',has_ams:false}),false);
    assert.equal(context.printerHasAms({...printer,name:'Workshop',has_ams:true}),true);
    assert.equal(context.printerHasAms({...printer,type:'klipper',has_ams:true,has_mmu:true}),false);
});
test('idle thumbnail refreshes when AMS attaches or disappears',()=>{
    assert.notEqual(context.printerThumbKey({...printer,has_ams:false}),context.printerThumbKey({...printer,has_ams:true}));
    assert.equal(context.printerThumbKey(printer),context.printerThumbKey({...printer,mc_percent:50}));
});
test('AMS picture accompanies the printer only when reported',()=>{
    const withAms=context.renderJobThumb('P1S',{...printer,has_ams:true});
    assert.match(withAms,/bambu-p1s\.png/);
    assert.match(withAms,/bambu-ams\.png/);
    assert.match(withAms,/P1S with AMS/);
    assert.doesNotMatch(context.renderJobThumb('P1S',printer),/bambu-ams\.png/);
});
test('active job preview sits over the printer and AMS fallback',()=>{
    const html=context.renderJobThumb('P1S',{...printer,has_ams:true,job:'model.gcode'});
    assert.ok(html.indexOf('pc-print-preview')>html.indexOf('pc-ams-picture'));
    assert.match(html,/\/api\/printer\/P1S\/thumbnail/);
});
