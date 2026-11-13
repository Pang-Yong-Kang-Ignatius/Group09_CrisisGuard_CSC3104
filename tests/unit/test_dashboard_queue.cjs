// Run with: node tests/unit/test_dashboard_queue.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.children = []; this.textContent = ''; this.checked = false; }
  replaceChildren() { this.children = []; }
  appendChild(child) { this.children.push(child); }
}
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
let hospitals;
const context = vm.createContext({$, document: {createElement: () => new Element()},
  fetch: async () => ({ok: true, json: async () => hospitals})});
const source = fs.readFileSync('dashboard/static/app.js', 'utf8');
vm.runInContext(source.slice(source.indexOf('async function refreshEdge()'),
  source.indexOf('async function captureEdgeEvent()')), context);
const queueText = () => $('edge-events-body').children
  .flatMap(row => row.children.map(cell => cell.textContent)).join(' ');
(async () => {
  const event = {event_id: 'offline-A', event_type: 'patient_registration', base_priority: 2, status: 'pending'};
  hospitals = Object.fromEntries(['a', 'b'].map(site => [`hospital-${site}`, {
    status: 'healthy', total: 1, pending: 1, applied: 0,
    events: [{...event, event_id: `offline-${site}`}]
  }]));
  await context.refreshEdge();
  assert.match(queueText(), /offline-a/);
  assert.match(queueText(), /offline-b/);
  assert.match(queueText(), /pending/);
  for (const hospital of Object.values(hospitals)) {
    hospital.events[0].status = 'applied'; hospital.pending = 0; hospital.applied = 1;
  }
  await context.refreshEdge();
  assert.equal(queueText(), 'No pending events.');
  assert.equal($('edge-a-applied').textContent, 1);
  $('show-applied-edge').checked = true;
  await context.refreshEdge();
  assert.match(queueText(), /offline-a/);
  assert.match(queueText(), /applied/);
  hospitals['hospital-a'] = {status: 'unavailable', error: 'ConnectTimeout: hospital offline'};
  await context.refreshEdge();
  assert.match(queueText(), /Hospital A: unable to retrieve queue.*ConnectTimeout/);
  assert.equal($('edge-a-total').textContent, 'Unknown');
  assert.match(queueText(), /offline-b/);
  console.log('PASS queue pending, recovery, counts, applied toggle, and hospital error rendering');
})().catch(error => { console.error(error); process.exitCode = 1; });
