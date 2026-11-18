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

test('every installed Bambu printer and the Klipper printer are selectable', async () => {
    const names = Array.from({length:12}, (_, i) => `School-BBL-${i+1}`);
    const printers = Object.fromEntries(names.map(name => [name, {type:'bambulab'}]));
    printers.Voron = {type:'klipper'};
    const {element, context} = harness(async () => response({printers}));
    await context.populateObicoPrinterDropdown();
    assert.deepEqual(element('obicoTargetPrinter').children.map(x => x.value), [...names, 'Voron']);
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


test('create and connect uses the selected installed name, not a fixed home printer', async () => {
    let posted;
    let configured = false;
    const {element, context} = harness(async (url, options) => {
        if (options) {posted = {url, data:JSON.parse(options.body)}; configured=true; return response({ok:true});}
        return response({printer_type:'bambulab', enabled:configured, server:'https://work.test', username:'owner', password:'********', printer_id:configured?42:0, auth_token:configured?'********':''});
    });
    element('obicoTargetPrinter').value = 'School BBL 12';
    await context.loadObicoForPrinter();
    element('obicoPassword').value = 'login-password';
    await context.createObicoPrinter();
    assert.equal(posted.url, '/api/obico/create/School%20BBL%2012');
    assert.deepEqual(posted.data, {server:'https://work.test', username:'owner', password:'login-password'});
    assert.equal(element('obicoPrinterId').value, 42);
    assert.equal(element('obicoAuthToken').value, '********');
    assert.equal(element('obicoRemoteControl').checked, false);
    assert.equal(element('obicoCreateButton').disabled, true);
});

test('create requires login details and prevents duplicate submissions while waiting', async () => {
    let calls=0, finish;
    const {element, context} = harness(async (url, options) => {
        if (options) {calls++; return new Promise(resolve => finish=resolve);}
        return response({printer_type:'bambulab', enabled:false});
    });
    element('obicoTargetPrinter').value = 'P1';
    await context.loadObicoForPrinter();
    await context.createObicoPrinter();
    assert.equal(calls, 0);
    for (const [id, value] of Object.entries({obicoServer:'https://test', obicoUsername:'user', obicoPassword:'password'})) element(id).value=value;
    const first=context.createObicoPrinter();
    await context.createObicoPrinter();
    assert.equal(calls, 1);
    finish(response({ok:true}));
    await first;
});
