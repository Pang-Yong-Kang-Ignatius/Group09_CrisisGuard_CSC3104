const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync('dashboard/static/quick-events.js','utf8');
const app = fs.readFileSync('dashboard/static/app.js','utf8');
const ids = app.slice(app.indexOf('function newId('),app.indexOf('function populateEventTypes('));
function setup({hospital='hospital-a',count='3',respond}={}) {
  const elements = new Map(), requests=[], tracked=[];
  let serial=0, refreshes=0;
  const el=id=>{
    if (!elements.has(id)) elements.set(id,{value:'',disabled:false,textContent:'',children:[],
      addEventListener(_,fn){this.click=fn;}, replaceChildren(){this.children=[];},appendChild(c){this.children.push(c);}});
    return elements.get(id);
  };
  el('quick-hospital').value=hospital; el('quick-count').value=count;
  const context=vm.createContext({document:{getElementById:el,createElement:()=>({})},
    window:{eventSync:{track:(...args)=>tracked.push(args)}},
    crypto:{randomUUID:()=>`unique-${++serial}`},AbortSignal:{timeout:()=>({})},
    refreshEdge:async()=>{refreshes++;},
    fetch:async(url,options)=>{
      const event=JSON.parse(options.body); requests.push({url,event});
      if(respond) return respond(event,requests.length);
      return {ok:true,status:200,json:async()=>({http_status:201,response:{status:'pending',event_id:event.event_id}})};
    }});
  vm.runInContext(ids+source,context);
  return {el,requests,tracked,run:()=>el('quick-generate').click(),refreshes:()=>refreshes};
}
for(const hospital of ['hospital-a','hospital-b']) test(`captures bounded registrations for ${hospital}`,async()=>{
  const s=setup({hospital,count:'10'}); await s.run();
  assert.equal(s.requests.length,10);
  assert.equal(s.tracked.length,10);
  assert.equal(new Set(s.requests.flatMap(r=>[r.event.event_id,r.event.payload.patient_id])).size,20);
  for(const {url,event} of s.requests){
    assert.equal(url,`/api/hospitals/${hospital}/capture`);
    assert.equal(event.site_id,hospital==='hospital-a'?'Hospital_A':'Hospital_B');
    assert.equal(event.event_type,'patient_registration');assert.equal(event.base_priority,2);
    assert.deepEqual(event.prerequisite_ids,[]); assert.ok(event.payload.patient_id);
  }
  assert.match(s.el('quick-status').textContent,/10 successful, 0 failed.*10\/10 attempted/);
  assert.equal(s.refreshes(),1);assert.equal(s.el('quick-generate').disabled,false);
});
for(const count of ['', ' ', '0','-1','11','1.5','NaN','Infinity','1e100']) test(`rejects invalid count ${JSON.stringify(count)}`,async()=>{
  const s=setup({count});await s.run();assert.equal(s.requests.length,0);
  assert.match(s.el('quick-status').textContent,/whole number/);assert.equal(s.refreshes(),0);
});
test('rejects unknown hospital without sending requests',async()=>{
  const s=setup({hospital:'arbitrary'});await s.run();assert.equal(s.requests.length,0);
});
test('counts partial failures once, retains event IDs and does not retry',async()=>{
  const s=setup({count:'5',respond:(event,n)=>{
    if(n===2) return {ok:true,status:200,json:async()=>({http_status:409})};
    if(n===3) throw new Error('lost response');
    if(n===4) return {ok:false,status:503,json:async()=>({detail:'unavailable'})};
    return {ok:true,status:200,json:async()=>({http_status:201,response:{status:'pending',event_id:event.event_id}})};
  }});
  await s.run();assert.equal(s.requests.length,5);assert.equal(s.tracked.length,2);
  assert.match(s.el('quick-status').textContent,/2 successful, 3 failed or unconfirmed.*5\/5 attempted/);
  assert.equal(s.el('quick-results').children.length,5);
  s.requests.forEach((r,i)=>assert.ok(s.el('quick-results').children[i].textContent.includes(r.event.event_id)));
  assert.match(s.el('quick-results').children[2].textContent,/no retry/);
});
for(const failure of ['invalid-json','wrong-id','wrong-state']) test(`does not confirm ${failure}`,async()=>{
  const s=setup({count:'1',respond:event=>({ok:true,status:200,json:async()=>{
    if(failure==='invalid-json')throw new Error('bad json');
    return {http_status:201,response:{status:failure==='wrong-state'?'applied':'pending',event_id:failure==='wrong-id'?'other':event.event_id}};
  }})});
  await s.run();assert.match(s.el('quick-status').textContent,/0 successful, 1 failed/);assert.equal(s.requests.length,1);
});
test('blocks repeated clicks and freezes hospital/count during an active batch',async()=>{
  let release;
  const s=setup({count:'1',respond:event=>new Promise(resolve=>{release=()=>resolve({ok:true,status:200,json:async()=>({http_status:201,response:{status:'pending',event_id:event.event_id}})});} )});
  const pending=s.run();assert.equal(s.el('quick-generate').disabled,true);
  assert.equal(s.el('quick-hospital').disabled,true);assert.equal(s.el('quick-count').disabled,true);
  await s.run();assert.equal(s.requests.length,1);release();await pending;
  assert.equal(s.el('quick-hospital').disabled,false);
  const next=s.run(); release(); await next;
  assert.notEqual(s.requests[0].event.event_id,s.requests[1].event.event_id);
});
