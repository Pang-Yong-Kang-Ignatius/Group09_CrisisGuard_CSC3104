const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync('dashboard/static/app.js', 'utf8');
const markup = fs.readFileSync('dashboard/static/index.html', 'utf8');
const begin = source.indexOf('async function previewHospitalScheduler()');
const end = source.indexOf('\nfunction openSchedulerInspector()', begin);
const revealBegin = source.indexOf('function openSchedulerInspector()');

async function renderInspector(events, options = {}) {
  const elements = new Map();
  const element = () => ({value: '', textContent: '', children: [], style: {}, listeners: {},
    addEventListener(name, handler) { this.listeners[name] = handler; },
    replaceChildren() { this.children = []; }, appendChild(child) { this.children.push(child); }});
  const $ = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  $('scheduler-hospital').value = 'hospital-a';
  $('scheduler-inheritance').value = options.inheritance ?? 'false';
  const context = vm.createContext({$, document: {createElement: element}, fetch: async url => {
    if (options.requests) options.requests.push(url);
    if (options.beforeResponse) await options.beforeResponse($);
    return {
    ok: true, json: async () => {
      if (options.beforeJson) await options.beforeJson($);
      return {events, next_event_id: events.find(event => event.selected)?.event_id ?? null, pending_count: 2,
      inheritance_enabled: false, evaluated_at: 'now', outstanding_event_id: 'A',
      replay_policy: {inheritance_enabled: true, ageing_interval_seconds: 600}, comparison_only: true,
      replay_wait_reason: 'Waiting for outstanding MetaGuard event A', warning: 'Read-only snapshot.',
      ...options.preview};
    },
    };
  }});
  vm.runInContext(source.slice(begin, end), context);
  await context.previewHospitalScheduler();
  return elements;
}

test('Inspector identifies read-only preview and outstanding-event wait', async () => {
  const elements = await renderInspector([]);
  const $ = id => elements.get(id);
  assert.match($('scheduler-summary').textContent, /not an executed grant or replay/);
  assert.match($('scheduler-summary').textContent, /Waiting for outstanding MetaGuard event A/);
  assert.match($('scheduler-summary').textContent, /Comparison only/);
  assert.match($('scheduler-summary').textContent, /Read-only snapshot/);
  assert.equal($('preview-scheduler').disabled, false);
});

for (const selector of ['scheduler-hospital', 'scheduler-inheritance']) {
  test(`Inspector discards a response after ${selector} changes while JSON is pending`, async () => {
    const elements = await renderInspector([], {beforeJson: async $ => {
      $(selector).value = selector === 'scheduler-hospital' ? 'hospital-b' : 'true';
      $(selector).listeners.change();
    }});
    assert.equal(elements.get('scheduler-summary').textContent, 'Selection changed. Load a new scheduler preview.');
    assert.equal(elements.get('scheduler-events-body').children.length, 0);
    assert.equal(elements.get('preview-scheduler').disabled, false);
  });

  test(`Inspector clears a rendered snapshot when ${selector} changes`, async () => {
    const elements = await renderInspector([{
      event_id: 'A_ONLY', base_priority: 2, aged_priority: 2, effective_priority: 2,
      prerequisite_ids: [], waiting_on: [], eligible: true, selected: true,
    }]);
    assert.equal(elements.get('scheduler-events-body').children[0].children[0].textContent, 'A_ONLY');
    elements.get(selector).value = selector === 'scheduler-hospital' ? 'hospital-b' : 'true';
    elements.get(selector).listeners.change();
    assert.equal(elements.get('scheduler-events-body').children.length, 0);
    assert.equal(elements.get('scheduler-summary').textContent, 'Selection changed. Load a new scheduler preview.');
  });
}

test('Inspector does not display an old hospital failure after selection changes', async () => {
  const elements = await renderInspector([], {beforeResponse: async $ => {
    $('scheduler-hospital').value = 'hospital-b';
    $('scheduler-hospital').listeners.change();
    throw new Error('Old Hospital A request failed');
  }});
  assert.equal(elements.get('scheduler-summary').textContent, 'Selection changed. Load a new scheduler preview.');
  assert.equal(elements.get('preview-scheduler').disabled, false);
});

test('Inspector still displays failures for the current selection and permits retry', async () => {
  const elements = await renderInspector([], {beforeResponse: async () => {
    throw new Error('Current hospital unavailable');
  }});
  assert.equal(elements.get('scheduler-summary').textContent, 'Preview failed: Current hospital unavailable');
  assert.equal(elements.get('preview-scheduler').disabled, false);
});

test('Active Inspector option omits comparison query and displays actual replay OFF', async () => {
  const requests = [];
  const elements = await renderInspector([], {inheritance: 'active', requests,
    preview: {replay_policy: {inheritance_enabled: false, ageing_interval_seconds: 30}, comparison_only: false}});
  assert.deepEqual(requests, ['/api/hospitals/hospital-a/scheduler/inspect']);
  const summary = elements.get('scheduler-summary').textContent;
  assert.match(summary, /Active replay: inheritance OFF, ageing 30 s/);
  assert.match(summary, /Preview uses active replay policy/);
  assert.doesNotMatch(summary, /Comparison only|live replay uses inheritance ON/);
});

test('ON comparison preserves the displayed active OFF policy', async () => {
  const requests = [];
  const elements = await renderInspector([], {inheritance: 'true', requests,
    preview: {inheritance_enabled: true, replay_policy: {inheritance_enabled: false, ageing_interval_seconds: 600}}});
  assert.deepEqual(requests, ['/api/hospitals/hospital-a/scheduler/inspect?inheritance=true']);
  const summary = elements.get('scheduler-summary').textContent;
  assert.match(summary, /Active replay: inheritance OFF/);
  assert.match(summary, /Comparison only; replay policy unchanged/);
});

test('Older hospital response does not invent an active inheritance policy', async () => {
  const elements = await renderInspector([], {preview: {replay_policy: undefined, comparison_only: undefined}});
  assert.match(elements.get('scheduler-summary').textContent, /Active replay policy unavailable/);
  assert.doesNotMatch(elements.get('scheduler-summary').textContent, /live replay uses inheritance ON/);
});

test('Inspector renders prerequisite and waiting-on IDs with existing decision fields', async () => {
  const elements = await renderInspector([
    {event_id: 'root', base_priority: 2, aged_priority: 2, effective_priority: 2,
      inherited_from: null, prerequisite_ids: [], waiting_on: [],
      eligible: true, reason: 'Eligible', selected: true},
    {event_id: 'child', base_priority: 1, aged_priority: 2, effective_priority: 5,
      inherited_from: 'child', prerequisite_ids: ['root'], waiting_on: ['root'],
      eligible: false, reason: 'Waiting for prerequisite acknowledgement', selected: false},
  ]);
  const $ = id => elements.get(id);
  const [selectedRow, blockedRow] = $('scheduler-events-body').children;
  assert.equal(selectedRow.children[0].textContent, 'root');
  assert.equal(selectedRow.children[8].textContent, 'Preview selects next');
  const row = blockedRow;
  assert.deepEqual(row.children.map(cell => cell.textContent), [
    'child', '1', '2', '5', 'child', 'root', 'root', 'No',
    'Waiting for prerequisite acknowledgement', '',
  ]);
  assert.match($('scheduler-summary').textContent, /not an executed grant or replay/);
});

test('Inspector clearly represents empty dependency lists', async () => {
  const elements = await renderInspector([{
    event_id: 'root', base_priority: 3, aged_priority: 3, effective_priority: 3,
    inherited_from: null, prerequisite_ids: [], waiting_on: [], eligible: true,
    reason: 'Eligible', selected: true,
  }]);
  const $ = id => elements.get(id);
  const row = $('scheduler-events-body').children[0];
  assert.equal(row.children[5].textContent, 'None');
  assert.equal(row.children[6].textContent, 'None');
  assert.equal(row.children[9].textContent, 'PREVIEW NEXT');
});

test('Events workspace shortcut opens and scrolls to the Inspector disclosure', () => {
  const eventsStart = markup.indexOf('data-page="events"');
  const eventsEnd = markup.indexOf('data-page="recovery"', eventsStart);
  const eventsMarkup = markup.slice(eventsStart, eventsEnd);
  const shortcutIndex = eventsMarkup.indexOf('id="open-scheduler-inspector"');
  const inspectorIndex = eventsMarkup.indexOf('id="scheduler-inspector"');
  assert.notEqual(shortcutIndex, -1);
  assert.notEqual(inspectorIndex, -1);
  assert.ok(shortcutIndex < inspectorIndex);

  const inspector = {open: false, scrollIntoView(options) { this.scrollOptions = options; }};
  const $ = id => id === 'scheduler-inspector' ? inspector : null;
  const context = vm.createContext({$, document: {}});
  const handlerEnd = source.indexOf("$('open-scheduler-inspector').addEventListener", revealBegin);
  vm.runInContext(source.slice(revealBegin, handlerEnd), context);
  context.openSchedulerInspector();
  assert.equal(inspector.open, true);
  assert.equal(inspector.scrollOptions.behavior, 'smooth');
  assert.equal(inspector.scrollOptions.block, 'start');
});
