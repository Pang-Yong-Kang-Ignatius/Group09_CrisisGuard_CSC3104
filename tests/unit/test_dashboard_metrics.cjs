const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync('dashboard/static/app.js', 'utf8');

class Element {
  constructor() {
    this.children = [];
    this.textContent = '';
    this.disabled = false;
    this.attributes = {};
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  appendChild(node) {
    this.children.push(node);
  }

  replaceChildren() {
    this.children = [];
    this.textContent = '';
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }
}

function setup(fetch = async () => {
  throw new Error('temporary outage');
}) {
  const elements = new Map();

  const $ = id => {
    if (!elements.has(id)) {
      elements.set(id, new Element());
    }
    return elements.get(id);
  };

  const context = vm.createContext({
    $,
    document: {
      createElement: () => new Element()
    },
    fetch,
    Date,
    AbortSignal: {
      timeout: ms => ({ timeoutMs: ms })
    }
  });

  vm.runInContext(
    source.slice(
      source.indexOf('function metricRow('),
      source.indexOf('$("service").addEventListener')
    ),
    context
  );

  return { $, context };
}

const metrics = {
  average_latency_ms: 10,
  p95_latency_ms: 25,
  throughput_rps: 2,
  error_rate: 0.025,
  in_flight_requests: 3,
  database_healthy: true,
  window_seconds: 60,
  observation_seconds: 30,
  latency_sample_count: 60,
  window_requests: 60,
  total_requests: 100
};

const entry = m => ({
  http_status: 200,
  response: m
});

const all = m =>
  Object.fromEntries(
    ['patient', 'emergency', 'admin'].map(k => [k, entry(m)])
  );

const rows = card =>
  Object.fromEntries(
    card.children.slice(1).map(r =>
      r.children.map(c => c.textContent)
    )
  );

// --------------------------------------------------
// Existing Cloud metrics
// --------------------------------------------------

test('all three services render real fields and units', () => {
  const s = setup();

  s.context.showMetrics(all(metrics));

  assert.equal(s.$('metrics-grid').children.length, 3);

  for (const card of s.$('metrics-grid').children) {
    const displayed = rows(card);

    for (const [label, expected] of Object.entries({
      'Mean latency': '10 ms',
      'p95 latency': '25 ms',
      'Throughput': '2 req/s',
      'Error rate': '2.5%',
      'In-flight requests': '3',
      'PostgreSQL': 'Healthy',
      'Rolling window': '60 s',
      'Observed duration': '30 s',
      'Latency samples': '60',
      'Window requests': '60',
      'Total requests': '100'
    })) {
      assert.equal(displayed[label], expected);
    }
  }
});

test('idle p95 is undefined rather than a fabricated zero', () => {
  const s = setup();

  s.context.showMetrics(
    all({
      ...metrics,
      p95_latency_ms: null,
      latency_sample_count: 0
    })
  );

  assert.equal(
    rows(s.$('metrics-grid').children[0])['p95 latency'],
    'No samples'
  );
});

test('legacy services keep new metrics unavailable and use existing window sample count', () => {
  const s = setup();

  s.context.showMetrics(
    all({
      average_latency_ms: 0,
      window_requests: 0,
      database_healthy: false
    })
  );

  const r = rows(s.$('metrics-grid').children[0]);

  assert.equal(r['p95 latency'], 'Unavailable');
  assert.equal(r['In-flight requests'], 'Unavailable');
  assert.equal(r['Observed duration'], 'Unavailable');
  assert.equal(r['Latency samples'], '0');
  assert.equal(r.PostgreSQL, 'Unavailable');
});

test('unavailable/malformed service does not hide healthy services', () => {
  const s = setup();

  s.context.showMetrics({
    patient: { http_status: 503 },
    emergency: entry(null),
    admin: entry(metrics)
  });

  assert.equal(
    rows(s.$('metrics-grid').children[0]).Status,
    'Unavailable'
  );

  assert.equal(
    rows(s.$('metrics-grid').children[1]).Status,
    'Unavailable'
  );

  assert.equal(
    rows(s.$('metrics-grid').children[2])['p95 latency'],
    '25 ms'
  );

  s.context.showMetrics(null);

  assert.equal(s.$('metrics-grid').children.length, 3);
});

test('missing or invalid observations never render NaN, null ms, or false health', () => {
  const s = setup();

  s.context.showMetrics(
    all({
      p95_latency_ms: null,
      latency_sample_count: 5,
      error_rate: NaN,
      average_latency_ms: -1
    })
  );

  const r = rows(s.$('metrics-grid').children[0]);

  assert.equal(r['p95 latency'], 'Unavailable');
  assert.equal(r['Error rate'], 'Unavailable');
  assert.equal(r['Mean latency'], 'Unavailable');
  assert.equal(r.PostgreSQL, 'Unknown');
});

test('zero is a valid observation and percentiles are not inferred from sample counts', () => {
  const s = setup();

  s.context.showMetrics(
    all({
      ...metrics,
      p95_latency_ms: 0,
      error_rate: 0,
      in_flight_requests: 0
    })
  );

  const r = rows(s.$('metrics-grid').children[0]);

  assert.equal(r['p95 latency'], '0 ms');
  assert.equal(r['In-flight requests'], '0');
  assert.equal(r['Error rate'], '0%');
});

// --------------------------------------------------
// New live SLO and executor monitoring
// --------------------------------------------------

test('live SLO and executor metrics render correctly', () => {
  const s = setup();

  s.context.showMetrics(
    all({
      ...metrics,
      live_p95_latency_ms: 180,
      live_latency_sample_count: 40,
      replay_latency_sample_count: 20,
      live_slo_threshold_ms: 250,
      live_slo_violation_count: 2,
      live_slo_violation_rate: 0.05,
      executor_queue_depth: 3,
      executor_queue_pressure: 0.15
    })
  );

  const cards = s.$('metrics-grid').children;

  assert.equal(cards.length, 3);

  for (const card of cards) {
    const r = rows(card);

    assert.equal(r['Live p95 latency'], '180 ms');
    assert.equal(r['Live SLO threshold'], '250 ms');
    assert.equal(r['Live SLO violations'], '5.00%');
    assert.equal(r['Live SLO violation count'], '2');
    assert.equal(r['Live request samples'], '40');
    assert.equal(r['Replay request samples'], '20');
    assert.equal(r['Executor queue depth'], '3');
    assert.equal(r['Executor queue pressure'], '15.00%');
  }
});

test('missing live SLO configuration hides SLO rows', () => {
  const s = setup();

  s.context.showMetrics(
    all({
      ...metrics,
      live_p95_latency_ms: null,
      live_latency_sample_count: 0,
      live_slo_threshold_ms: null,
      live_slo_violation_count: null,
      live_slo_violation_rate: null
    })
  );

  const r = rows(s.$('metrics-grid').children[0]);

  assert.equal(r['Live p95 latency'], 'No samples');
  assert.equal(r['Live SLO threshold'], undefined);
  assert.equal(r['Live SLO violations'], undefined);
  assert.equal(r['Live SLO violation count'], undefined);
});

test('configured SLO without live samples does not report zero violations', () => {
  const s = setup();

  s.context.showMetrics(
    all({
      ...metrics,
      live_p95_latency_ms: null,
      live_latency_sample_count: 0,
      live_slo_threshold_ms: 250,
      live_slo_violation_count: 0,
      live_slo_violation_rate: null,
      executor_queue_depth: 0,
      executor_queue_pressure: 0
    })
  );

  const r = rows(s.$('metrics-grid').children[0]);

  assert.equal(r['Live p95 latency'], 'No samples');
  assert.equal(r['Live SLO threshold'], '250 ms');
  assert.equal(r['Live SLO violations'], 'No live samples');
  assert.equal(r['Executor queue depth'], '0');
  assert.equal(r['Executor queue pressure'], '0.00%');
});

// --------------------------------------------------
// Controller status
// --------------------------------------------------

for (const [freshness, label] of [
  [true, 'Fresh'],
  [false, 'Stale or unavailable'],
  [undefined, 'Unknown (not exposed)']
]) {
  test(`controller freshness ${freshness} uses API information only`, () => {
    const s = setup();

    const r = Object.fromEntries(
      s.context.controllerMetricRows({
        status: 'available',
        state: 'Recovery',
        rate_rps: 0.2,
        telemetry_fresh: freshness
      })
    );

    assert.equal(r.State, 'Recovery');
    assert.equal(r['Admission rate'], '0.2 req/s');
    assert.equal(r['Telemetry freshness'], label);
  });
}

test('controller configuration and connection failures do not invent state', () => {
  const s = setup();

  assert.equal(
    Object.fromEntries(
      s.context.controllerMetricRows({
        status: 'not_configured'
      })
    ).Connection,
    'Not configured'
  );

  assert.equal(
    Object.fromEntries(
      s.context.controllerMetricRows(null)
    )['Controller state'],
    'Unknown'
  );
});

// --------------------------------------------------
// Dashboard refresh and error handling
// --------------------------------------------------

test('temporary metrics request failure shows connection error without stale healthy claims', async () => {
  const s = setup();

  await s.context.refreshDashboard();

  assert.match(
    s.$('metrics-grid').textContent,
    /Unable to retrieve telemetry/
  );

  assert.equal(s.$('updated').textContent, 'Connection error');
  assert.equal(s.$('patient-health').textContent, 'Unknown');
});

test('manual refresh shows progress while an existing poll finishes', async () => {
  const releases = [];
  const options = [];

  const s = setup((_url, requestOptions) => {
    options.push(requestOptions);

    return new Promise(resolve => releases.push(resolve));
  });

  const background = s.context.refreshDashboard();
  const pending = s.context.refreshNow();

  const button = s.$('refresh');

  assert.equal(button.textContent, 'Refreshing…');
  assert.equal(button.disabled, true);
  assert.equal(button.attributes['aria-busy'], 'true');

  assert.equal(options.length, 2);
  assert.equal(options[0].signal.timeoutMs, 15000);
  assert.equal(options[1].signal.timeoutMs, 15000);

  releases[0]({
    ok: true,
    json: async () => ({})
  });

  releases[1]({
    ok: true,
    json: async () => ({})
  });

  await Promise.all([background, pending]);

  assert.equal(button.textContent, 'Refresh now');
  assert.equal(button.disabled, false);
  assert.equal(button.attributes['aria-busy'], undefined);
});

test('timed-out refresh reports the timeout and clears progress', async () => {
  const timeout = new Error('The operation was aborted');
  timeout.name = 'TimeoutError';

  const s = setup(async () => {
    throw timeout;
  });

  await s.context.refreshNow();

  assert.match(
    s.$('metrics-grid').textContent,
    /Request timed out after 15 seconds/
  );

  assert.equal(s.$('updated').textContent, 'Connection error');
  assert.equal(s.$('patient-health').textContent, 'Unknown');
  assert.equal(s.$('refresh').textContent, 'Refresh now');
  assert.equal(s.$('refresh').disabled, false);
});

// --------------------------------------------------
// Overview
// --------------------------------------------------

test('Overview does not render undefined/NaN or invent database health for partial metrics', () => {
  const s = setup();

  s.context.window = {};
  s.context.el = s.$;

  const workspace = fs.readFileSync(
    'dashboard/static/workspace.js',
    'utf8'
  );

  vm.runInContext(
    workspace.slice(
      workspace.indexOf('  function card('),
      workspace.indexOf('  let busy = false;')
    ),
    s.context
  );

  s.context.window.workspaceMetrics(all({}));

  const r = rows(s.$('overview-metrics').children[0]);

  assert.equal(r['Avg latency'], 'Unavailable');
  assert.equal(r['Error rate'], 'Unavailable');
  assert.equal(r.PostgreSQL, 'Unknown');
});

// --------------------------------------------------
// Event history
// --------------------------------------------------

test('history filtering after a failed refresh cannot resurrect stale applied rows', async () => {
  let available = true;

  const old = {
    event_id: 'OLD-APPLIED',
    site_id: 'Hospital_A',
    event_type: 'patient_registration',
    status: 'applied'
  };

  const s = setup(async () =>
    available
      ? {
          ok: true,
          json: async () => ({
            events: [old]
          })
        }
      : {
          ok: false,
          status: 503
        }
  );

  s.$('history-limit').value = '50';
  s.$('history-search').value = '';

  s.context.document.createElement = () => {
    const e = new Element();
    e.addEventListener = () => {};
    return e;
  };

  vm.runInContext(
    'let cloudEvents=[];' +
      source.slice(
        source.indexOf('async function loadEventHistory()'),
        source.indexOf('function showEventDetails(')
      ),
    s.context
  );

  await s.context.loadEventHistory();

  assert.match(
    s.$('history-count').textContent,
    /Showing 1/
  );

  available = false;

  await s.context.loadEventHistory();

  assert.match(
    s.$('history-count').textContent,
    /Unable/
  );

  s.context.renderEventHistory();

  assert.match(
    s.$('history-body').children[0].children[0].textContent,
    /History unavailable/
  );

  assert.match(
    s.$('history-count').textContent,
    /Unable/
  );

  available = true;

  await s.context.loadEventHistory();

  assert.match(
    s.$('history-count').textContent,
    /Showing 1/
  );
});

test('unmarked successful traffic is visible without becoming live', () => {
  const s = setup();
  s.context.showMetrics(all({...metrics, live_latency_sample_count:0,
    live_p95_latency_ms:null, live_slo_threshold_ms:250,
    live_slo_violation_rate:null, live_slo_violation_count:0,
    replay_latency_sample_count:0, unclassified_latency_sample_count:100}));
  const r=rows(s.$('metrics-grid').children[0]);
  assert.equal(r['Unclassified request samples'],'100');
  assert.equal(r['Live p95 latency'],'No samples');
  assert.equal(r['Live SLO violations'],'No live samples');
  assert.equal(r['Live SLO violation count'],'No live samples');
});

test('replay samples do not imply live latency samples', () => {
  const s=setup();
  s.context.showMetrics(all({...metrics, live_latency_sample_count:0,
    live_p95_latency_ms:null, live_slo_threshold_ms:250,
    replay_latency_sample_count:100,unclassified_latency_sample_count:0}));
  const r=rows(s.$('metrics-grid').children[0]);
  assert.equal(r['Replay request samples'],'100');
  assert.equal(r['Live p95 latency'],'No samples');
  assert.equal(r['Live SLO violations'],'No live samples');
});

test('missing telemetry hides SLO rows while retaining latency observations', () => {
  const s=setup();
  s.context.showMetrics(all({...metrics}));
  let r=rows(s.$('metrics-grid').children[0]);
  assert.equal(r['Live SLO threshold'],undefined);
  assert.equal(r['Live SLO violations'],undefined);
  assert.equal(r['Live p95 latency'],'Unavailable');
  s.context.showMetrics(all({...metrics,live_latency_sample_count:0,live_slo_threshold_ms:250}));
  r=rows(s.$('metrics-grid').children[0]);
  assert.equal(r['Live p95 latency'],'Unavailable');
});

for(const rate of [NaN,Infinity,-.01,1.01,'0']) {
  test(`invalid live SLO rate ${String(rate)} remains unavailable`, () => {
    const s=setup();
    s.context.showMetrics(all({...metrics,live_latency_sample_count:1,
      live_p95_latency_ms:5,live_slo_threshold_ms:250,live_slo_violation_rate:rate}));
    assert.equal(rows(s.$('metrics-grid').children[0])['Live SLO violations'],'Unavailable');
  });
}

test('observed zero live violations remains a valid measurement', () => {
  const s=setup();
  s.context.showMetrics(all({...metrics,live_latency_sample_count:1,
    live_p95_latency_ms:0,live_slo_threshold_ms:250,
    live_slo_violation_rate:0,live_slo_violation_count:0}));
  const r=rows(s.$('metrics-grid').children[0]);
  assert.equal(r['Live p95 latency'],'0 ms');
  assert.equal(r['Live SLO violations'],'0.00%');
  assert.equal(r['Live SLO violation count'],'0');
});

test('connection failure is distinct from missing telemetry and healthy peers', () => {
  const s=setup();
  s.context.showMetrics({patient:{http_status:null,response:{code:'SERVICE_UNREACHABLE'}},
    emergency:{http_status:200,response:null},admin:entry(metrics)});
  const cards=s.$('metrics-grid').children;
  assert.equal(rows(cards[0]).Status,'Connection failed');
  assert.equal(rows(cards[1]).Status,'Unavailable');
  assert.equal(rows(cards[2])['p95 latency'],'25 ms');
});
