const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../templates/dashboard.html'), 'utf8');
const code = template.slice(template.indexOf('        // ── Obico Configuration'), template.indexOf('        // ── Spoolman Settings'));

function harness(fetch) {
    const elements = {};
    const element = id => elements[id] ??= {value:'', checked:false, style:{}, innerHTML:'', textContent:'', children:[], appendChild(x) {this.children.push(x);}};
    const document = {getElementById:element, createElement:() => ({})};
    const context = vm.createContext({document, fetch, BASE:'', escapeHtml:s => String(s)});
    vm.runInContext(code, context);
    return {element, context};
}
const response = data => ({ok:true, json:async () => data});

test('all four work Bambu printers and the Klipper printer are selectable', async () => {
    const printers = Object.fromEntries([1,2,3,4].map(n => [`P1S-${n}`, {type:'bambulab'}]));
    printers.Voron = {type:'klipper'};
    const {element, context} = harness(async () => response({printers}));
    await context.populateObicoPrinterDropdown();
    assert.deepEqual(element('obicoTargetPrinter').children.map(x => x.value), ['P1S-1','P1S-2','P1S-3','P1S-4','Voron']);
});

test('Bambu restores its masked token and control setting; Klipper hides those fields', async () => {
    const {element, context} = harness(async url => response(url.endsWith('/Bambu')
        ? {printer_type:'bambulab', enabled:true, auth_token:'********', password:'********', allow_remote_control:false}
        : {printer_type:'klipper', enabled:true}));
    element('obicoTargetPrinter').value = 'Bambu';
    await context.loadObicoForPrinter();
    assert.equal(element('obicoBambuFields').style.display, '');
    assert.equal(element('obicoAuthToken').value, '********');
    assert.equal(element('obicoRemoteControl').checked, false);
    element('obicoTargetPrinter').value = 'Voron';
    await context.loadObicoForPrinter();
    assert.equal(element('obicoBambuFields').style.display, 'none');
    assert.equal(element('obicoAuthToken').value, '');
});

test('late responses cannot overwrite the newly selected printer or its credentials', async () => {
    const pending = {};
    const {element, context} = harness(url => new Promise(resolve => pending[url.split('/').pop()] = resolve));
    element('obicoTargetPrinter').value = 'P1S-1';
    const first = context.loadObicoForPrinter();
    element('obicoTargetPrinter').value = 'P1S-2';
    const second = context.loadObicoForPrinter();
    pending['P1S-2'](response({printer_type:'bambulab', server:'https://second.test', printer_id:2, auth_token:'second-token'}));
    await second;
    pending['P1S-1'](response({printer_type:'bambulab', server:'https://first.test', printer_id:1, auth_token:'first-token'}));
    await first;
    assert.equal(element('obicoServer').value, 'https://second.test');
    assert.equal(element('obicoAuthToken').value, 'second-token');
});

test('save sends the selected printer settings and reloads masked credentials', async () => {
    let posted;
    const {element, context} = harness(async (url, options) => {
        if (options) {posted = {url, data:JSON.parse(options.body)}; return response({ok:true, enabled:true});}
        return response({printer_type:'bambulab', enabled:true, auth_token:'********', password:'********'});
    });
    element('obicoTargetPrinter').value = 'P1S-1';
    await context.loadObicoForPrinter();
    element('obicoServer').value = 'https://obico.test';
    element('obicoPrinterId').value = '4';
    element('obicoAuthToken').value = 'new-token';
    element('obicoRemoteControl').checked = true;
    await context.saveObicoConfig();
    assert.equal(posted.url, '/api/obico/config/P1S-1');
    assert.equal(posted.data.printer_id, 4);
    assert.equal(posted.data.auth_token, 'new-token');
    assert.equal(posted.data.allow_remote_control, true);
    assert.equal(element('obicoAuthToken').value, '********');
});
