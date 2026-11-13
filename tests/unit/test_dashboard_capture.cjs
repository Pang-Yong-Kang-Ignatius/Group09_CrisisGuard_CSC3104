// Run from the repository root: node --test tests/unit/test_dashboard_capture.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync('dashboard/static/app.js', 'utf8');
const captureSource = source.slice(
  source.indexOf('async function captureEdgeEvent()'),
  source.indexOf('\n$("edge-event-id").value = newId("EDGE");')
);

function setup(prerequisites, hospital = 'hospital-a', result = {http_status: 201}) {
  const elements = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {value: '', disabled: false});
    return elements.get(id);
  };
  $('edge-hospital').value = hospital;
  $('edge-event-id').value = 'CHILD';
  $('edge-event-type').value = 'encounter_creation';
  $('edge-priority').value = '5';
  $('edge-prerequisites').value = prerequisites;
  const requests = [], tracked = [];
  let refreshes = 0;
  const context = vm.createContext({
    $,
    window: {eventSync: {track: (...args) => tracked.push(args)}},
    newId: prefix => `${prefix}-NEW`,
    refreshEdge: async () => { refreshes += 1; },
    fetch: async (url, options) => {
      requests.push({url, body: JSON.parse(options.body)});
      if (result instanceof Error) throw result;
      return {status: result.http_status, json: async () => result};
    },
  });
  vm.runInContext(captureSource, context);
  return {$, requests, tracked, capture: () => context.captureEdgeEvent(), refreshes: () => refreshes};
}

test('capture sends multiple prerequisite IDs and selected hospital identity', async () => {
  const ui = setup(' ROOT-1, ROOT-2 ', 'hospital-b');
  await ui.capture();
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].url, '/api/hospitals/hospital-b/capture');
  assert.deepEqual(ui.requests[0].body.prerequisite_ids, ['ROOT-1', 'ROOT-2']);
  assert.equal(ui.requests[0].body.site_id, 'Hospital_B');
  assert.equal(ui.requests[0].body.event_id, 'CHILD');
  assert.equal(ui.requests[0].body.base_priority, 5);
  assert.equal(ui.$('edge-prerequisites').value, '');
  assert.equal(ui.$('edge-event-id').value, 'EDGE-NEW');
  assert.equal(ui.$('capture-edge').disabled, false);
  assert.equal(ui.refreshes(), 1);
  assert.match(ui.$('edge-result-status').textContent, /Stored Locally \(Pending Synchronization\)/);
  assert.equal(ui.tracked[0][0].event_id, 'CHILD');
  assert.equal(ui.tracked[0][1], 'hospital-b');
});

test('an empty prerequisite field preserves independent capture', async () => {
  const ui = setup('');
  await ui.capture();
  assert.deepEqual(ui.requests[0].body.prerequisite_ids, []);
  assert.equal(ui.requests[0].body.site_id, 'Hospital_A');
});

test('blank comma-separated entries are ignored', async () => {
  const ui = setup(' , ROOT-1, , ROOT-2, ');
  await ui.capture();
  assert.deepEqual(ui.requests[0].body.prerequisite_ids, ['ROOT-1', 'ROOT-2']);
});

test('server rejection preserves entered IDs and displays the validation error', async () => {
  const ui = setup('ROOT, ROOT', 'hospital-a', {
    http_status: 422,
    response: {detail: [{msg: 'Duplicate prerequisite ID'}]},
  });
  await ui.capture();
  // Keep duplicates visible to the API validator rather than silently changing them.
  assert.deepEqual(ui.requests[0].body.prerequisite_ids, ['ROOT', 'ROOT']);
  assert.equal(ui.$('edge-prerequisites').value, 'ROOT, ROOT');
  assert.equal(ui.$('edge-event-id').value, 'CHILD');
  assert.match(ui.$('edge-result-output').textContent, /Duplicate prerequisite ID/);
  assert.match(ui.$('edge-result-status').textContent, /HTTP 422.*Capture rejected/);
  assert.equal(ui.tracked.length,0);
  assert.equal(ui.$('capture-edge').disabled, false);
});

test('a connection failure preserves input for retry and re-enables capture', async () => {
  const ui = setup('ROOT', 'hospital-a', new Error('Hospital unavailable'));
  await ui.capture();
  assert.equal(ui.$('edge-prerequisites').value, 'ROOT');
  assert.equal(ui.$('edge-event-id').value, 'CHILD');
  assert.equal(ui.$('edge-result-output').textContent, 'Hospital unavailable');
  assert.equal(ui.$('capture-edge').disabled, false);
});
