const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync('dashboard/static/app.js', 'utf8');
const replaySource = source.slice(
  source.indexOf('async function replayHospital('),
  source.indexOf('\n$("replay-a").addEventListener', source.indexOf('async function replayHospital('))
);

function setup(token, response = {ok: true, http_status: 200}) {
  const elements = new Map(), requests = [];
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {value: '', disabled: false, textContent: '', className: ''});
    return elements.get(id);
  };
  $('replay-token').value = token;
  const context = vm.createContext({$, fetch: async (url, options) => {
    requests.push({url, options});
    return {ok: response.ok, json: async () => response.ok
      ? {http_status: response.http_status, response: {applied: 1, total_attempted: 1}}
      : {detail: 'Manual replay authorization required'}};
  }, refreshEdge: async () => {}, loadEventHistory: async () => {}, refreshDashboard: async () => {}});
  vm.runInContext(replaySource, context);
  return {$, requests, replay: hospital => context.replayHospital(hospital)};
}

test('blank operator token does not call the replay route', async () => {
  const ui = setup('');
  await ui.replay('hospital-a');
  assert.equal(ui.requests.length, 0);
  assert.match(ui.$('replay-status').textContent, /authorization required/);
});

test('operator token is sent once and cleared from the input', async () => {
  const ui = setup('operator-private-token');
  await ui.replay('hospital-b');
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].url, '/api/hospitals/hospital-b/replay');
  assert.equal(ui.requests[0].options.headers.Authorization, 'Bearer operator-private-token');
  assert.equal(ui.$('replay-token').value, '');
  assert.match(ui.$('replay-status').textContent, /1 applied out of 1 attempted/);
});

test('rejection does not display the supplied token', async () => {
  const ui = setup('operator-private-token', {ok: false});
  await ui.replay('hospital-a');
  assert.equal(ui.$('replay-token').value, '');
  assert.equal(ui.$('replay-output').textContent.includes('operator-private-token'), false);
  assert.equal(ui.$('replay-a').disabled, false);
});
