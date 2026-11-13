const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');
const source=fs.readFileSync('dashboard/static/test-traffic.js','utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const ready={enabled:true,active:false,run:null};
function job(active=false,errors=false){return {enabled:true,active,run:{id:'run',count:10,status:active?'running':'completed',services:Object.fromEntries(['patient','emergency','admin'].map(s=>[s,{status:errors?'completed_with_errors':active?'running':'completed',attempted:10,succeeded:errors?8:10,failed:errors?2:0,not_attempted:0,in_flight:0,errors:errors?{'HTTP 503':2}:{}}]))}};}
function response(data,status=200){return {ok:status>=200&&status<300,status,json:async()=>data};}
function setup(handler,demoHandler=null){
 const elements=new Map();
 function el(id){if(!elements.has(id))elements.set(id,{value:'100',disabled:true,children:[],textContent:'',addEventListener(_,f){this.click=f;},replaceChildren(){this.children=[];},appendChild(n){this.children.push(n);}});return elements.get(id);}
 const calls=[],timers=[];let refreshes=0;
 const context=vm.createContext({document:{getElementById:el,createElement:()=>({textContent:''})},
  fetch:async(url,options={})=>{
   if(url==='/api/demo-records'&&!demoHandler)return response({status:'ready',services:{patient:'applied',emergency:'applied',admin:'applied'}});
   calls.push({url,options});
   return (url==='/api/demo-records'?demoHandler:handler)(url,options,calls);
  },
  AbortSignal:{timeout:()=>({})},setTimeout:(f,ms)=>{timers.push({f,ms});return timers.length;},clearTimeout:()=>{},
  loadDashboard:async()=>{refreshes++;}});
 vm.runInContext(source,context);
 return {el,calls,timers,refreshes:()=>refreshes,click:()=>el('test-traffic-generate').click()};
}

test('disabled deployment never posts traffic',async()=>{
 const s=setup(()=>response({},404));await flush();
 assert.equal(s.el('test-traffic-generate').disabled,true);
 await s.click();assert.equal(s.calls.length,1);
 assert.match(s.el('test-traffic-status').textContent,/Not enabled/);
});

test('successful batch sends only bounded count and local-action header, then refreshes metrics',async()=>{
 let gets=0;
 const s=setup((url,o)=>response(o.method==='POST'?job(true):++gets===1?ready:job()));
 await flush();assert.equal(s.el('test-traffic-generate').disabled,false);
 await s.click();
 const post=s.calls.find(c=>c.options.method==='POST');
 assert.deepEqual(JSON.parse(post.options.body),{count:100});
 assert.equal(post.options.headers['X-CrisisGuard-Local-Action'],'test-traffic');
 assert.equal(Object.keys(post.options.headers).length,2);
 assert.equal(s.el('test-traffic-results').children.length,3);
 assert.match(s.el('test-traffic-results').children[0].textContent,/10 successful, 0 failed/);
 assert.equal(s.refreshes(),1);
 assert.equal(s.el('test-traffic-generate').disabled,false);
});

test('double-click and active jobs freeze controls and show progress',async()=>{
 let release,finished=false,gets=0;
 const s=setup((url,o)=>o.method==='POST'?new Promise(r=>{release=()=>r(response(job(true)));}):response(++gets===1?ready:job(!finished)));
 await flush();const pending=s.click();
 assert.equal(s.el('test-traffic-count').disabled,true);
 await s.click();assert.equal(s.calls.filter(c=>c.options.method==='POST').length,1);
 release();await pending;assert.equal(s.el('test-traffic-generate').disabled,true);
 assert.match(s.el('test-traffic-status').textContent,/running/);
 finished=true;await s.timers.pop().f();
 assert.equal(s.el('test-traffic-generate').disabled,false);assert.equal(s.refreshes(),1);
});

for(const value of ['0','11','101','1.5','NaN'])test(`rejects selector value ${value}`,async()=>{
 const s=setup(()=>response(ready));await flush();s.el('test-traffic-count').value=value;
 await s.click();assert.equal(s.calls.length,1);
 assert.match(s.el('test-traffic-status').textContent,/Choose 10, 50 or 100/);
});

test('partial failures and missing records are displayed rather than counted as success',async()=>{
 let gets=0;
 const result=job(false,true);
 result.run.services.admin={status:'no_applied_record',attempted:0,succeeded:0,failed:0,not_attempted:10,errors:{}};
 const s=setup((u,o)=>response(o.method==='POST'?job(true):++gets===1?ready:result));
 await flush();await s.click();
 const lines=s.el('test-traffic-results').children.map(c=>c.textContent).join('\n');
 assert.match(lines,/8 successful, 2 failed/);assert.match(lines,/HTTP 503: 2/);
 assert.match(lines,/no_applied_record.*0 successful.*10 not attempted/);
 assert.equal(s.refreshes(),1);
});

test('uncertain POST is never automatically resubmitted and status is checked',async()=>{
 let gets=0;
 const s=setup((u,o)=>{if(o.method==='POST')throw new TypeError('network error');return response(++gets===1?ready:job(true));});
 await flush();await s.click();
 assert.equal(s.calls.filter(c=>c.options.method==='POST').length,1);
 assert.equal(s.el('test-traffic-generate').disabled,true);
 assert.ok(s.timers.length);
});

test('start rejection does not echo response bodies or lose its error',async()=>{
 let gets=0;
 const s=setup((u,o)=>o.method==='POST'?response({detail:'PRIVATE_TOKEN'},503):response(++gets===1?ready:ready));
 await flush();await s.click();
 assert.match(s.el('test-traffic-status').textContent,/HTTP 503/);
 assert.ok(!s.el('test-traffic-status').textContent.includes('PRIVATE_TOKEN'));
});

test('status failure leaves controls disabled and schedules GET-only recovery',async()=>{
 const s=setup(()=>response({},503));await flush();
 assert.equal(s.el('test-traffic-generate').disabled,true);
 assert.match(s.el('test-traffic-status').textContent,/status unavailable/);
 assert.ok(s.timers.length);
});

test('demo preparation is explicit and waits for applied records',async()=>{
 let state='not_prepared';
 const snapshot=()=>({status:state,services:{patient:state,emergency:state,admin:state}});
 const s=setup(()=>response(ready),(url,o)=>{
   if(o.method==='POST'){state='pending';return response(snapshot());}
   return response(snapshot());
 });
 await flush();
 assert.equal(s.el('demo-prepare').disabled,false);
 assert.equal(s.el('test-traffic-generate').disabled,true);
 assert.match(s.el('demo-status').textContent,/Prepare synthetic records/);
 await s.el('demo-prepare').click();
 const posts=s.calls.filter(c=>c.url==='/api/demo-records'&&c.options.method==='POST');
 assert.equal(posts.length,1);
 assert.equal(posts[0].options.headers['X-CrisisGuard-Local-Action'],'prepare-demo-records');
 assert.match(s.el('demo-status').textContent,/Pending synchronization/);
 state='ready';
 await s.timers.pop().f();
 assert.match(s.el('demo-status').textContent,/all three synthetic records applied/);
 assert.equal(s.el('test-traffic-generate').disabled,false);
 assert.equal(s.el('demo-results').children.length,3);
});

test('demo preparation is unavailable when local server gate rejects it',async()=>{
 const s=setup(()=>response(ready),()=>response({},404));
 await flush();
 assert.equal(s.el('demo-prepare').disabled,true);
 await s.el('demo-prepare').click();
 assert.equal(s.calls.filter(c=>c.options.method==='POST').length,0);
});
