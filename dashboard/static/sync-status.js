/* One visible-workspace queue/history loop. A queue ACK alone cannot prove Cloud application. */
(() => {
  const el = id => document.getElementById(id);
  const tracked = new Map();
  let timer = null, running = null, epoch = 0, disposed = false;
  const visible = () => !disposed && !document.hidden && ['#events', '#recovery'].includes(location.hash);
  const pending = () => [...tracked.values()].some(item => !item.applied);
  function render() {
    el('sync-events').replaceChildren();
    for (const item of tracked.values()) {
      const text = `${item.event.event_id}: ${item.applied ? 'Applied to Cloud' : 'Stored Locally (Pending Synchronization)'}`;
      const row = document.createElement('li'); row.textContent = text;
      el('sync-events').appendChild(row);
      // A new capture owns its result element; older events cannot overwrite it.
      if (item.target && item.target.syncOwner === item.key) item.target.textContent = text;
    }
  }
  function clear() { if (timer !== null) clearTimeout(timer); timer = null; }
  function schedule() {
    clear();
    if (visible()) timer = setTimeout(() => { timer = null; refresh(); }, pending() ? 2000 : 5000);
  }
  async function refresh() {
    clear();
    if (!visible()) return;
    if (running) return running;
    const generation = epoch;
    running = (async () => {
      const [hospitalResult, historyResult] = await Promise.allSettled([refreshEdge(), loadEventHistory()]);
      if (generation !== epoch || !visible()) return;
      const hospitals = hospitalResult.status === 'fulfilled' ? hospitalResult.value : null;
      const history = historyResult.status === 'fulfilled' ? historyResult.value : null;
      for (const item of tracked.values()) {
        if (Array.isArray(history) && history.some(row => row.event_id === item.event.event_id &&
            row.site_id === item.event.site_id && row.event_type === item.event.event_type &&
            row.status === 'applied' && row.applied_at)) item.applied = true;
      }
      render();
      const healthy = ['hospital-a', 'hospital-b'].every(key => hospitals?.[key]?.status === 'healthy');
      el('sync-updated').textContent = `Last updated: ${new Date().toLocaleTimeString()}. ` +
        (healthy && Array.isArray(history) ? 'Queues and Cloud history refreshed.' :
          'Some API observations unavailable; pending events remain unconfirmed.');
    })();
    try { await running; }
    finally { running = null; schedule(); }
  }
  function lifecycle() {
    epoch++; clear();
    el('sync-panel').hidden = !['#events', '#recovery'].includes(location.hash);
    if (visible()) {
      // Discard any old workspace observation before issuing a new one.
      if (running) running.finally(schedule); else refresh();
    }
  }
  window.eventSync = {
    track(event, hospital, target) {
      const key = `${hospital}:${event.event_id}`;
      if (!tracked.has(key)) tracked.set(key, {key, event: {...event}, hospital, applied: false, target});
      const item = tracked.get(key); item.target = target;
      if (target) target.syncOwner = key;
      render();
      if (visible()) refresh();
    },
    refresh
  };
  el('sync-refresh').addEventListener('click', refresh);
  el('refresh').addEventListener('click', refresh);
  window.addEventListener('hashchange', lifecycle);
  document.addEventListener('visibilitychange', lifecycle);
  window.addEventListener('pagehide', () => { disposed = true; epoch++; clear(); });
  window.addEventListener('pageshow', () => { disposed = false; lifecycle(); });
  lifecycle();
})();
