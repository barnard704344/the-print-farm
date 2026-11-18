const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../templates/dashboard.html'), 'utf8');
const api = template.slice(template.indexOf('        async function apiPost('), template.indexOf('        // ── Printer Rendering'));
const send = template.slice(template.indexOf('        async function sendQueuedJob('), template.indexOf('        async function reprintToSelected('));

function harness(assign, confirm=true) {
    const calls=[], toasts=[], refresh=[], removed=[];
    const dropdown={querySelectorAll:()=>[{value:'Voron'},{value:'Lab Bambu'}], classList:{remove:x=>removed.push(x)}};
    const fetch = async (url, options) => {
        calls.push({url, options});
        if (url.endsWith('/check_filament')) return {ok:true,json:async()=>({ok:true,match:true})};
        if (assign instanceof Error) throw assign;
        return {ok:assign.httpOK, status:assign.status, json:async()=>assign.body};
    };
    const context=vm.createContext({BASE:'',fetch,showToast:(...args)=>toasts.push(args),
        refreshJobs:()=>refresh.push('jobs'),refreshPrinters:()=>refresh.push('printers'),
        document:{getElementById:()=>dropdown},confirm:()=>confirm,alert:()=>{}});
    vm.runInContext(api+send, context);
    return {context,calls,toasts,refresh,removed};
}

test('assigned Send shows the server plate-clear refusal instead of silently returning', async()=>{
    const reason="The build plate on 'Voron' has not been confirmed clear. Press 'Plate cleared'.";
    const h=harness({httpOK:false,status:409,body:{error:reason}});
    await h.context.sendToAssigned(340,'Voron');
    assert.equal(h.toasts[0][0],reason);
    assert.equal(h.toasts[0][1],'error');
    assert.equal(h.refresh.length,0);
    assert.deepEqual(JSON.parse(h.calls[1].options.body),{printers:['Voron']});
});

test('multi-printer Send displays missing-file errors and keeps the selector open', async()=>{
    const reason='Job source file is missing; re-upload the file to print again';
    const h=harness({httpOK:false,status:400,body:{error:reason}});
    await h.context.sendToSelected(12);
    assert.equal(h.toasts[0][0],reason);
    assert.equal(h.toasts[0][1],'error');
    assert.equal(h.removed.length,0);
    assert.equal(h.refresh.length,0);
});

test('a failed assignment inside a successful HTTP response is still reported', async()=>{
    const h=harness({httpOK:true,status:200,body:{ok:false,results:[{printer:'Voron',ok:false}]}});
    await h.context.sendToAssigned(12,'Voron');
    assert.equal(h.toasts[0][1],'error');
    assert.equal(h.refresh.length,0);
});

test('network failures produce visible feedback and do not claim the job was sent', async()=>{
    const h=harness(new Error('connection lost'));
    await h.context.sendToAssigned(12,'Voron');
    assert.match(h.toasts[0][0],/network error/);
    assert.equal(h.toasts[0][1],'error');
    assert.equal(h.refresh.length,0);
});

test('successful dispatch shows feedback and refreshes printers and queue', async()=>{
    const h=harness({httpOK:true,status:200,body:{ok:true}});
    await h.context.sendToSelected(12);
    assert.equal(h.toasts[0][0],'Job sent to Voron, Lab Bambu');
    assert.deepEqual(h.refresh,['jobs','printers']);
    assert.deepEqual(h.removed,['open']);
});
