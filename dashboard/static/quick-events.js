/* Bounded, single-attempt hospital capture for local outage demonstrations. */
(() => {
  const el = id => document.getElementById(id);
  let busy = false;
  async function generate() {
    if (busy) return;
    const hospital = el('quick-hospital').value;
    const rawCount = el('quick-count').value.trim();
    const count = Number(rawCount);
    if (!['hospital-a', 'hospital-b'].includes(hospital) || !rawCount ||
        !Number.isInteger(count) || count < 1 || count > 10) {
      el('quick-status').textContent = 'Select Hospital A or B and a whole number of events from 1 to 10.';
      return;
    }
    busy = true;
    const controls = ['quick-generate', 'quick-hospital', 'quick-count'];
    controls.forEach(id => { el(id).disabled = true; });
    el('quick-results').replaceChildren();
    let successful = 0, failed = 0;
    const summary = () => {
      el('quick-status').textContent = `${successful} successful, ${failed} failed or unconfirmed captures; ${successful + failed}/${count} attempted.`;
    };
    summary();
    try {
      for (let i = 0; i < count; i++) {
        const event = {
          event_id: newId('QUICK'),
          site_id: hospital === 'hospital-a' ? 'Hospital_A' : 'Hospital_B',
          event_type: 'patient_registration', base_priority: 2, prerequisite_ids: [],
          payload: {patient_id: newId('QUICK_PATIENT'), test_source: 'crisisguard_edge_dashboard'}
        };
        let message;
        try {
          const response = await fetch(`/api/hospitals/${hospital}/capture`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(event), signal: AbortSignal.timeout(15000)
          });
          const data = await response.json();
          if (response.ok && data.http_status === 201 && data.response?.status === 'pending' &&
              data.response?.event_id === event.event_id) {
            successful++;
            message = 'Stored Locally (Pending Synchronization).';
          } else {
            failed++;
            message = `Failed or unconfirmed (HTTP ${response.ok ? data.http_status ?? response.status : response.status}). Check the queue; no retry was sent.`;
          }
        } catch (error) {
          failed++;
          message = 'Capture unconfirmed: response unavailable. Check the queue; no retry was sent.';
        }
        const row = document.createElement('li');
        row.textContent = `${event.event_id}: ${message}`;
        el('quick-results').appendChild(row);
        if (message.startsWith('Stored Locally') && typeof window !== 'undefined') window.eventSync?.track(event, hospital, row);
        summary();
      }
    } finally {
      busy = false;
      controls.forEach(id => { el(id).disabled = false; });
    }
    // Observe queues only; this generator never triggers replay or Cloud writes.
    try { await refreshEdge(); } catch (_) { /* Capture results remain available. */ }
  }
  el('quick-generate').addEventListener('click', generate);
})();
