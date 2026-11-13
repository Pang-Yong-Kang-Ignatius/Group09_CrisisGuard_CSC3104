const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync('dashboard/static/sync-status.js','utf8');
const event = {event_id:'EXACT',site_id:'Hospital_A',event_type:'patient_registration'};
const applied = {...event,status:'applied',applied_at:'2026-10-27T01:00:00Z'};
function setup() {
  const nodes=new Map(), timers=new Map(), handlers={}, docHandlers={};
  let serial=0, queues=0, histories=0;
  const el=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',children:[],hidden:false,
    replaceChildren(){this.children=[];},appendChild(c){this.children.push(c);},addEventListener(k,fn){this[k]=fn;}});return nodes.get(id);};
  const document={hidden:false,getElementById:el,createElement:()=>({}),addEventListener(k,fn){docHandlers[k]=fn;}};
  const location={hash:'#events'};
  const data={hospitals:{'hospital-a':{status:'healthy'},'hospital-b':{status:'healthy'}},history:[]};
  const window={addEventListener(k,fn){handlers[k]=fn;}};
  vm.runInNewContext(source,{document,location,window,Date,Promise,
    setTimeout(fn,ms){const id=++serial;timers.set(id,{fn,ms});return id;},clearTimeout(id){timers.delete(id);},
    refreshEdge:async()=>{queues++; if(data.queueFn)return data.queueFn();return data.hospitals;},
    loadEventHistory:async()=>{histories++;if(data.historyFn)return data.historyFn();return data.history;}});
  const settle=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
  const tick=async()=>{assert.equal(timers.size,1);const [id,t]=[...timers][0];timers.delete(id);t.fn();await settle();};
  return {el,document,location,data,window,handlers,docHandlers,timers,settle,tick,calls:()=>[queues,histories],track:(e=event,t=el('result'))=>window.eventSync.track(e,'hospital-a',t)};
}
test('pending becomes applied only from exact PostgreSQL history; counts/history share one cadence',async()=>{
  const s=setup();await s.settle();s.track();await s.settle();
  assert.match(s.el('result').textContent,/Stored Locally \(Pending Synchronization\)/);
  assert.equal([...s.timers.values()][0].ms,2000);
  s.data.history=[applied];await s.tick();
  assert.match(s.el('result').textContent,/Applied to Cloud/);
  assert.match(s.el('sync-updated').textContent,/Last updated:/);
  assert.equal([...s.timers.values()][0].ms,5000);
  assert.deepEqual(s.calls(),[3,3]);
});
test('outage and API exceptions preserve pending and recover without false failure',async()=>{
  const s=setup();await s.settle();s.track();await s.settle();
  s.data.hospitals={a:{status:'unavailable'}};s.data.history=null;await s.tick();
  assert.match(s.el('result').textContent,/Pending Synchronization/);
  assert.match(s.el('sync-updated').textContent,/unavailable/);
  s.data.historyFn=()=>{throw new Error('503');};await s.tick();
  assert.match(s.el('result').textContent,/Pending Synchronization/);
  s.data.historyFn=null;s.data.history=[applied];await s.tick();
  assert.match(s.el('result').textContent,/Applied to Cloud/);
});
test('wrong ID, hospital, event type, status or missing timestamp never confirms applied',async()=>{
  const s=setup();await s.settle();s.track();await s.settle();
  for(const row of [{...applied,event_id:'OTHER'},{...applied,site_id:'Hospital_B'},
    {...applied,event_type:'encounter_creation'},{...applied,status:'pending'},{...applied,applied_at:null}]){
    s.data.history=[row];await s.tick();assert.match(s.el('result').textContent,/Pending Synchronization/);
  }
});
test('hidden tab and navigation cancel timers; visible return resumes one loop',async()=>{
  const s=setup();await s.settle();s.track();await s.settle();
  s.document.hidden=true;s.docHandlers.visibilitychange();assert.equal(s.timers.size,0);
  const calls=s.calls();await s.window.eventSync.refresh();assert.deepEqual(s.calls(),calls);
  s.document.hidden=false;s.docHandlers.visibilitychange();await s.settle();assert.equal(s.timers.size,1);
  s.location.hash='#settings';s.handlers.hashchange();assert.equal(s.timers.size,0);
  s.location.hash='#recovery';s.handlers.hashchange();await s.settle();assert.equal(s.timers.size,1);
  s.handlers.pagehide();assert.equal(s.timers.size,0);
  s.handlers.pageshow();await s.settle();assert.equal(s.timers.size,1);
});
test('in-flight manual refreshes and duplicate track calls do not overlap; stale responses are ignored',async()=>{
  const s=setup();await s.settle();let release;
  s.data.historyFn=()=>new Promise(resolve=>{release=resolve;});
  s.track();s.track();s.el('sync-refresh').click();s.el('refresh').click();
  assert.deepEqual(s.calls(),[2,2]);
  s.location.hash='#settings';s.handlers.hashchange();release([applied]);await s.settle();
  assert.match(s.el('result').textContent,/Pending Synchronization/);assert.equal(s.timers.size,0);
  s.data.historyFn=null;s.data.history=[];s.location.hash='#events';s.handlers.hashchange();await s.settle();
  assert.equal(s.timers.size,1);assert.match(s.el('result').textContent,/Pending Synchronization/);
});
test('old event cannot overwrite a newer capture result and confirmed events never regress',async()=>{
  const s=setup();await s.settle();s.track();await s.settle();
  s.track({...event,event_id:'NEW'});await s.settle();
  s.data.history=[applied];await s.tick();assert.match(s.el('result').textContent,/NEW: Stored Locally/);
  s.data.history=[{...applied,event_id:'NEW'}];await s.tick();assert.match(s.el('result').textContent,/NEW: Applied to Cloud/);
  s.data.history=null;await s.tick();assert.match(s.el('result').textContent,/NEW: Applied to Cloud/);
});
const appSource=fs.readFileSync('dashboard/static/app.js','utf8');
test('actual queue/history loaders coalesce manual and polling requests',async()=>{
  const elements=new Map();
  const $=id=>{if(!elements.has(id))elements.set(id,{value:'50',children:[],checked:false,
    replaceChildren(){this.children=[];},appendChild(c){this.children.push(c);}});return elements.get(id);};
  const releases=[];let requests=0;
  const context=vm.createContext({$,document:{createElement:()=>({children:[],appendChild(c){this.children.push(c);}})},
    fetch:()=>{requests++;return new Promise(resolve=>releases.push(resolve));},renderEventHistory(){}});
  vm.runInContext('let cloudEvents=[];'+appSource.slice(appSource.indexOf('async function loadEventHistory()'),appSource.indexOf('function renderEventHistory()'))+
    appSource.slice(appSource.indexOf('async function refreshEdge()'),appSource.indexOf('async function captureEdgeEvent()')),context);
  const edge1=context.refreshEdge(),edge2=context.refreshEdge();assert.equal(requests,1);
  releases.shift()({ok:true,json:async()=>({})});await Promise.all([edge1,edge2]);
  const history1=context.loadEventHistory(),history2=context.loadEventHistory();assert.equal(requests,2);
  releases.shift()({ok:true,json:async()=>({events:[applied]})});
  const results=await Promise.all([history1,history2]);assert.equal(results[0][0].event_id,'EXACT');
  assert.equal(results[1][0].event_id,'EXACT');assert.equal(requests,2);
});
test('history limit changes discard obsolete response and serialize the replacement',async()=>{
  const elements={'history-limit':{value:'25'},'history-count':{},'history-body':{replaceChildren(){}}};
  const releases=[];let renders=0;
  const context=vm.createContext({$:id=>elements[id],renderEventHistory(){renders++;},
    fetch:()=>new Promise(resolve=>releases.push(resolve))});
  vm.runInContext('let cloudEvents=[];'+appSource.slice(appSource.indexOf('async function loadEventHistory()'),appSource.indexOf('function renderEventHistory()')),context);
  const old=context.loadEventHistory();elements['history-limit'].value='100';const latest=context.loadEventHistory();
  assert.equal(releases.length,1);releases.shift()({ok:true,json:async()=>({events:[applied]})});await old;
  for(let i=0;i<5;i++)await Promise.resolve();assert.equal(renders,0);assert.equal(releases.length,1);
  releases.shift()({ok:true,json:async()=>({events:[]})});await latest;assert.equal(renders,1);
});

test('empty or partial hospital snapshots cannot be reported as successful queue refresh',async()=>{
 const s=setup();await s.settle();
 for(const snapshot of [{},{'hospital-a':{status:'healthy'}}]){
  s.data.hospitals=snapshot;await s.tick();
  assert.match(s.el('sync-updated').textContent,/unavailable/);
 }
});
