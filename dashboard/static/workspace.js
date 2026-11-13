/* Page routing never duplicates form IDs or resets unsent event values. */
(() => {
  const el = id => document.getElementById(id);
  const pages = {
    overview: ['Overview', 'Cloud services and hospital operations at a glance.'],
    events: ['Events & Hospitals', 'Capture events, inspect queues and verify PostgreSQL history.'],
    recovery: ['Recovery & MetaGuard', 'Run the local outage workflow and observe real recovery snapshots.'],
    monitoring: ['Monitoring', 'Live application metrics and database connectivity.'],
    settings: ['Settings & Diagnostics', 'Connection guidance, configuration and response codes.']
  };
  const aliases = {edge: 'events', testing: 'events', history: 'events', scheduler: 'events', telemetry: 'monitoring', guide: 'settings', 'outage-simulator': 'recovery'};
  function route(focus = true) {
    const requested = location.hash.slice(1);
    const page = pages[requested] ? requested : aliases[requested] || 'overview';
    if (requested !== page) history.replaceState(null, '', `#${page}`);
    for (const panel of document.querySelectorAll('[data-page]')) panel.hidden = panel.dataset.page !== page;
    for (const link of document.querySelectorAll('.nav-link')) {
      const active = link.getAttribute('href') === `#${page}`;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    }
    el('page-title').textContent = pages[page][0];
    el('page-description').textContent = pages[page][1];
    document.title = `${pages[page][0]} | CrisisGuard`;
    el('workspace-nav').classList.remove('open');
    el('menu-toggle').setAttribute('aria-expanded', 'false');
    if (focus) { el('page-title').focus(); window.scrollTo(0, 0); }
  }
  el('menu-toggle').addEventListener('click', () => {
    const open = el('workspace-nav').classList.toggle('open');
    el('menu-toggle').setAttribute('aria-expanded', String(open));
  });
  window.addEventListener('hashchange', () => route());
  route(false);

  function card(title, rows) {
    const node = document.createElement('article'); node.className = 'card';
    const heading = document.createElement('h3'); heading.textContent = title; node.appendChild(heading);
    for (const [label, value] of rows) node.appendChild(metricRow(label, value ?? 'Unknown'));
    return node;
  }
  window.workspaceMetrics = data => {
    const grid = el('overview-metrics'); grid.replaceChildren();
    let available = 0;
    for (const name of ['patient', 'emergency', 'admin']) {
      const entry = data?.[name];
      const m = entry?.http_status === 200 && entry.response && typeof entry.response === 'object' && !Array.isArray(entry.response) ? entry.response : null;
      if (m) available++;
      grid.appendChild(card(`${name[0].toUpperCase() + name.slice(1)} metrics`, [
        ['PostgreSQL', m?.database_healthy === true ? 'Healthy' : m?.database_healthy === false ? 'Unavailable' : 'Unknown'],
        ['Avg latency', metricValue(m?.average_latency_ms, ' ms')],
        ['Throughput', metricValue(m?.throughput_rps, ' req/s')],
        ['Error rate', typeof m?.error_rate === 'number' && m.error_rate <= 1 ? metricValue(m.error_rate * 100, '%') : 'Unavailable']
      ]));
    }
    el('connection-status').textContent = `${available}/3 Cloud metrics endpoints available. Last checked ${new Date().toLocaleTimeString()}. See Overview for API health and hospital status.`;
  };
  window.workspaceHospitals = data => {
    const grid = el('overview-summary'); grid.replaceChildren();
    let pending = 0, known = true;
    for (const [key, label] of [['hospital-a', 'Hospital A'], ['hospital-b', 'Hospital B']]) {
      const h = data?.[key], ok = h?.status === 'healthy';
      known = known && ok; if (ok) pending += h.pending;
      grid.appendChild(card(label, [['Connection', ok ? 'Healthy' : 'Unavailable'], ['Pending', ok ? h.pending : 'Unknown'], ['Applied', ok ? h.applied : 'Unknown']]));
    }
    grid.appendChild(card('Queue summary', [['Pending', known ? pending : 'Unknown'], ['Recovery', known ? (pending ? 'Pending events; see Recovery' : 'No pending events') : 'Unknown'], ['Failed-event total', 'Not exposed by queue API']]));
  };
  let busy = false;
  async function refreshRecovery() {
    if (busy) return;
    busy = true;
    const grid = el('recovery-grid');
    try {
      const response = await fetch('/api/recovery', {signal: AbortSignal.timeout(20000)});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json(); grid.replaceChildren();
      const c = data.controller;
      grid.appendChild(card('MetaGuard admission', controllerMetricRows(c)));
      for (const [key, label] of [['hospital-a', 'Hospital A'], ['hospital-b', 'Hospital B']]) {
        const h = data.hospitals?.[key];
        grid.appendChild(card(`${label} recovery`, h?.status === 'available' ? [
          ['Automatic replay', h.enabled ? 'Enabled' : 'Disabled'], ['Cloud reachable', h.cloud_reachable == null ? 'Unknown' : h.cloud_reachable ? 'Yes' : 'No'],
          ['Replayable backlog', h.replayable_backlog], ['Last replay applied / attempted', `${h.last_replay_applied ?? 'Unknown'} / ${h.last_replay_attempted ?? 'Unknown'}`],
          ['Last check', h.last_check_at], ['Drain completed', h.drain_completed_at]
        ] : [['Connection', 'Unavailable']]));
      }
    } catch (error) { grid.textContent = `Recovery monitoring unavailable: ${error.message}`; }
    finally { busy = false; }
  }
  el('refresh').addEventListener('click', () => { refreshRecovery(); });
  refreshRecovery(); setInterval(refreshRecovery, 5000);
})();
