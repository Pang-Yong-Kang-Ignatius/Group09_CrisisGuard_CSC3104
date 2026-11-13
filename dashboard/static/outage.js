/* Fixed loopback agent. Authorization token stays only in this closure. */
(() => {
  const agent = 'http://127.0.0.1:8765';
  const el = id => document.getElementById(id);
  let token = '', busy = false, polling = false;
  const message = text => { el('outage-message').textContent = text; };
  function controls(disabled) {
    el('outage-stop').disabled = disabled || busy || !token;
    el('outage-restore').disabled = disabled || busy || !token;
    el('outage-connect').disabled = busy;
    el('outage-token').disabled = busy;
  }
  function health(services) {
    el('outage-health').textContent = ['patient', 'emergency', 'admin'].map(name =>
      `${name[0].toUpperCase()+name.slice(1)} API: ${services[name]?.healthy ? 'Healthy' : 'Unavailable'}`
    ).join(' | ');
  }
  async function request(path, action) {
    const response = await fetch(agent+path, {
      method: action ? 'POST' : 'GET', cache: 'no-store',
      headers: {Authorization: `Bearer ${token}`, ...(action ? {'Content-Type':'application/json'} : {})},
      ...(action ? {body: JSON.stringify({action})} : {}),
      signal: AbortSignal.timeout(action ? 240000 : 10000)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Local control request rejected');
    return result;
  }
  async function poll() {
    if (busy || polling) return;
    polling = true;
    try {
      if (token) {
        const status = await request('/status');
        health(status.services); controls(status.busy);
      } else {
        const response = await fetch('/api/services', {signal: AbortSignal.timeout(30000)});
        if (!response.ok) throw new Error('Cloud status is unavailable');
        const status = await response.json();
        health(Object.fromEntries(Object.entries(status).map(([name, value]) => [name, {healthy: value.http_status === 200}])));
      }
    } catch (error) {
      controls(true);
      if (token) message(`Controls unavailable: ${error.message}. Check the agent terminal, then authorize again.`);
    } finally { polling = false; }
  }
  el('outage-connect').addEventListener('click', async () => {
    if (busy) return;
    token = el('outage-token').value.trim(); el('outage-token').value = '';
    if (!token) { message('Enter the temporary token from the local agent terminal.'); return; }
    busy = true; controls(true);
    try {
      const status = await request('/status'); health(status.services);
      message(`Authorized original project: ${status.project}. API health verified.`);
      busy = false; controls(status.busy);
    } catch (error) {
      token = ''; busy = false; controls(true); message(`Authorization failed: ${error.message}`);
    }
  });
  async function operate(action) {
    if (busy || !token) return;
    busy = true; controls(true);
    message(action === 'outage' ? 'Stopping the three Cloud APIs and verifying they are unavailable...' : 'Starting Cloud APIs and waiting for verified health...');
    try {
      const status = await request('/operation', action); health(status.services);
      message(action === 'outage' ? 'Cloud outage confirmed. Hospitals remain available for offline capture.' : 'Cloud health confirmed. Hospital queues replay automatically; watch pending events become applied.');
    } catch (error) {
      message(`Operation not confirmed: ${error.message}. Refresh status before retrying.`);
    } finally { busy = false; await poll(); }
  }
  el('outage-stop').addEventListener('click', () => operate('outage'));
  el('outage-restore').addEventListener('click', () => operate('restore'));
  poll(); setInterval(poll, 10000);
})();
