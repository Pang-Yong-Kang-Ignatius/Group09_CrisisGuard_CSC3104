/* Server owns credentials, targets and rate limits. No browser telemetry token. */
let demoRecordsReady = false;
let refreshTrafficControls = () => {};
(() => {
  const button = document.getElementById('test-traffic-generate');
  const count = document.getElementById('test-traffic-count');
  const status = document.getElementById('test-traffic-status');
  const results = document.getElementById('test-traffic-results');
  if (!button || !count || !status || !results) return;
  let pending = false;
  let enabled = false;
  let timer = null;
  let wasActive = false;
  let startError = '';

  function controls(active) {
    button.disabled = count.disabled = !enabled || !demoRecordsReady || active;
  }
  refreshTrafficControls = () => controls(pending || wasActive);

  function render(data) {
    enabled = data?.enabled === true;
    const active = data?.active === true;
    controls(active || pending);
    results.replaceChildren();
    const run = data?.run;
    status.textContent = !enabled ? 'Not enabled in this deployment.' : startError && !active ? startError : !run
      ? 'Ready: authenticated local reads only.'
      : `Test traffic: ${run.status}. ${run.error || ''}`;
    for (const service of ['patient', 'emergency', 'admin']) {
      const item = run?.services?.[service];
      if (!item) continue;
      const row = document.createElement('li');
      const errors = Object.entries(item.errors || {}).map(([label, n]) => `${label}: ${n}`).join('; ');
      row.textContent = `${service}: ${item.status}; ${item.attempted}/${run.count} attempted, ` +
        `${item.succeeded} successful, ${item.failed} failed, ${item.not_attempted} not attempted` +
        (item.in_flight ? `, ${item.in_flight} in flight` : '') + (errors ? `. ${errors}` : '');
      results.appendChild(row);
    }
    if (run && !active && (wasActive || pending)) {
      // Refresh real measurements, never inject derived latency/SLO numbers.
      if (typeof loadDashboard === 'function') void loadDashboard();
    }
    wasActive = active;
    return active;
  }

  async function poll() {
    clearTimeout(timer);
    try {
      const response = await fetch('/api/test-traffic', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
      if (response.status === 404) { render({enabled: false}); return; }
      if (!response.ok) throw new Error('status unavailable');
      if (render(await response.json())) timer = setTimeout(poll, 500);
    } catch (_) {
      status.textContent = 'Traffic status unavailable; an accepted server job may still be running. Rechecking.';
      controls(true);
      timer = setTimeout(poll, 2000);
    }
  }

  async function generate() {
    if (!enabled || !demoRecordsReady || pending || wasActive) return;
    if (!['10', '50', '100'].includes(count.value)) {
      status.textContent = 'Choose 10, 50 or 100 requests per API.';
      return;
    }
    pending = true;
    startError = '';
    controls(true);
    status.textContent = 'Starting test traffic...';
    try {
      const response = await fetch('/api/test-traffic', {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CrisisGuard-Local-Action': 'test-traffic'},
        body: JSON.stringify({count: Number(count.value)}), signal: AbortSignal.timeout(5000)});
      if (response.status === 409) {
        status.textContent = 'Test traffic is already running; checking progress.';
      } else if (!response.ok) {
        // Never echo arbitrary backend/proxy error bodies or credentials.
        throw new Error(`Start rejected (HTTP ${response.status}); check local enablement and server credentials.`);
      } else {
        render(await response.json());
      }
    } catch (error) {
      startError = error.name === 'Error' ? error.message : 'Request unconfirmed; checking server status. No automatic retry.';
      status.textContent = startError;
    } finally {
      pending = false;
      // GET-only polling resolves ambiguous POST failures without resubmission.
      await poll();
    }
  }

  button.addEventListener('click', generate);
  void poll();
})();

/* Explicit local capture through Hospital A; automatic replay remains grant-controlled. */
(() => {
  const button = document.getElementById('demo-prepare');
  const status = document.getElementById('demo-status');
  const results = document.getElementById('demo-results');
  if (!button || !status || !results) return;
  let pending = false;
  let timer = null;
  let startError = '';

  async function observe() {
    clearTimeout(timer);
    try {
      const response = await fetch('/api/demo-records', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
      if (response.status === 404) {
        demoRecordsReady = false;
        refreshTrafficControls();
        button.disabled = true;
        status.textContent = 'Demo record preparation is available only on the protected local stack.';
        return;
      }
      if (!response.ok) throw new Error('status unavailable');
      const data = await response.json();
      demoRecordsReady = data.status === 'ready';
      refreshTrafficControls();
      button.disabled = pending || data.status === 'conflict';
      if (data.status === 'ready') startError = '';
      const observed = data.status === 'ready' ? 'Ready: all three synthetic records applied through MetaGuard.'
        : data.status === 'pending' ? 'Pending synchronization: waiting for Hospital A automatic replay. If this persists, inspect its Scheduler Inspector.'
        : data.status === 'conflict' ? 'Conflicting demo event ID. Inspect Hospital A; no record was overwritten.'
        : 'Prepare synthetic records for Patient, Emergency and Admin.';
      status.textContent = startError ? `${startError} ${observed}` : observed;
      results.replaceChildren();
      for (const service of ['patient', 'emergency', 'admin']) {
        const row = document.createElement('li');
        row.textContent = `${service}: ${data.services?.[service] || 'unavailable'}`;
        results.appendChild(row);
      }
      if (data.status === 'pending') timer = setTimeout(observe, 2000);
    } catch (_) {
      demoRecordsReady = false;
      refreshTrafficControls();
      button.disabled = true;
      status.textContent = 'Preparation status unavailable. Check local services and retry the status page.';
      timer = setTimeout(observe, 5000);
    }
  }

  async function prepare() {
    if (pending || button.disabled) return;
    pending = true;
    demoRecordsReady = false;
    refreshTrafficControls();
    startError = '';
    button.disabled = true;
    status.textContent = 'Capturing synthetic records through Hospital A...';
    try {
      const response = await fetch('/api/demo-records', {
        method: 'POST', headers: {'X-CrisisGuard-Local-Action': 'prepare-demo-records'},
        signal: AbortSignal.timeout(30000)
      });
      if (!response.ok) throw new Error(`Preparation failed or unconfirmed (HTTP ${response.status}). Inspect Hospital A before retrying.`);
    } catch (error) {
      startError = error.name === 'Error' ? error.message
        : 'Capture response unconfirmed. Checking durable status; no automatic retry.';
      status.textContent = startError;
    } finally {
      pending = false;
      await observe();
    }
  }

  button.addEventListener('click', prepare);
  void observe();
})();
