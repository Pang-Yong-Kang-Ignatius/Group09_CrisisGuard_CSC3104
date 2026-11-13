
const $ = (id) => document.getElementById(id);

const eventTypes = {
  patient: ["patient_registration", "encounter_creation"],
  emergency: ["emergency_record"],
  admin: ["patient_discharge"]
};

const explanations = {
  200: "Duplicate-safe success: this exact event was already applied.",
  201: "Success: this new event was applied to PostgreSQL.",
  409: "Conflict: check the response for missing prerequisites or an existing event ID with different data.",
  422: "Invalid request: inspect the validation details.",
  503: "PostgreSQL or an upstream dependency is unavailable. Restore service health before retrying the identical event."
};

let lastApplied = null;

function newId(prefix = "DASH") {
  if (crypto.randomUUID) {
    return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
  }
  return `${prefix}_${Date.now()}_${Math.floor(Math.random() * 1000000)}`;
}

function populateEventTypes() {
  const service = $("service").value;
  const select = $("event-type");
  select.replaceChildren();

  for (const type of eventTypes[service]) {
    const option = document.createElement("option");
    option.value = type;
    option.textContent = type.replaceAll("_", " ");
    select.appendChild(option);
  }
}

function defaultPayload() {
  return {
    patient_id: newId("PATIENT"),
    test_source: "crisisguard_dashboard"
  };
}

function setScenario() {
  const scenario = $("scenario").value;
  const service = $("service").value;

  $("priority").value = 2;
  $("prerequisites").value = "";
  $("event-id").value = newId("DASH");
  $("payload").value = JSON.stringify(defaultPayload(), null, 2);

  let instructions = "";
  let expected = "";

  if (scenario === "new") {
    expected = "201 Created";
    instructions =
      "Creates a new synthetic event using a unique ID.\n" +
      "1. Review the values.\n" +
      "2. Click Execute Test.\n" +
      "3. Expect HTTP 201 and result: applied.";
  }

  if (scenario === "duplicate") {
    expected = "200 OK";

    if (lastApplied) {
      $("service").value = lastApplied.service;
      populateEventTypes();
      $("event-type").value = lastApplied.event.event_type;
      $("site").value = lastApplied.event.site_id;
      $("priority").value = lastApplied.event.base_priority;
      $("event-id").value = lastApplied.event.event_id;
      $("prerequisites").value =
        lastApplied.event.prerequisite_ids.join(", ");
      $("payload").value =
        JSON.stringify(lastApplied.event.payload, null, 2);

      instructions =
        "Reusing your last successfully applied event.\n" +
        "Do not modify any values.\n" +
        "Click Execute Test and expect HTTP 200: already_applied.";
    } else {
      instructions =
        "First select New Event and submit it successfully.\n" +
        "Then return to Duplicate Event to reuse exactly the same request.";
    }
  }

  if (scenario === "missing") {
    expected = "409 Conflict";
    $("prerequisites").value = newId("MISSING");
    instructions =
      "This event references a prerequisite that does not exist.\n" +
      "Click Execute Test.\n" +
      "Expect HTTP 409 with MISSING_PREREQUISITES.\n" +
      "The dependent event should not be applied.";
  }

  if (scenario === "invalid") {
    expected = "422 Invalid Input";
    $("priority").value = 9;
    instructions =
      "Priority 9 is outside the allowed range of 1 to 5.\n" +
      "Click Execute Test.\n" +
      "Expect HTTP 422. No event should be applied.";
  }

  if (scenario === "outage") {
    expected = "503 Service Unavailable";
    instructions =
      "Manual database outage test:\n" +
      "1. In PowerShell: docker compose stop postgres\n" +
      "2. Wait for PostgreSQL to stop.\n" +
      "3. Click Execute Test. Expect HTTP 503.\n" +
      "4. In PowerShell: docker compose start postgres\n" +
      "5. Wait until PostgreSQL is healthy.\n" +
      "6. Keep the same event ID and payload, then retry.\n" +
      "7. Expect HTTP 201 after recovery.\n" +
      "Never use docker compose down -v for this test.";
  }

  $("scenario-instructions").textContent =
    `Expected: ${expected}\n\n${instructions}`;

  $("submit-event").disabled =
    scenario === "duplicate" && !lastApplied;
}

function createEvent() {
  const prerequisite_ids = $("prerequisites").value
    .split(",")
    .map(value => value.trim())
    .filter(Boolean);

  const payload = JSON.parse($("payload").value);

  if (
    payload === null ||
    Array.isArray(payload) ||
    typeof payload !== "object"
  ) {
    throw new Error("Payload must be a JSON object.");
  }

  return {
    event_id: $("event-id").value.trim(),
    site_id: $("site").value,
    event_type: $("event-type").value,
    base_priority: Number($("priority").value),
    prerequisite_ids,
    payload
  };
}

async function sendTest() {
  const button = $("submit-event");
  const scenario = $("scenario").value;

  let event;
  try {
    event = createEvent();
  } catch (error) {
    $("result-status").textContent = "Invalid JSON payload";
    $("result-status").className = "result-status error";
    $("result-output").textContent = error.message;
    return;
  }

  const service = $("service").value;
  button.disabled = true;
  button.textContent = "Sending request...";

  try {
    const response = await fetch(`/api/events/${service}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(event)
    });

    const data = await response.json();
    const status = Object.hasOwn(data, "http_status")
      ? data.http_status : response.status;
    const result = data.response ?? data;

    const expected = {
      new: 201,
      duplicate: 200,
      missing: 409,
      invalid: 422,
      outage: 503
    }[scenario];

    const matched = status === expected;

    $("result-status").textContent =
      `HTTP ${status} | ${matched ? "Expected result" : "Unexpected result"}`;
    $("result-status").className =
      `result-status ${matched ? "success" : "error"}`;

    $("result-output").textContent =
      JSON.stringify(result, null, 2);

    $("result-explanation").textContent =
      explanations[status] ??
      (status === null
        ? "The API could not be reached. Check its running status."
        : "Review the returned error details.");

    if (
      status === 201 &&
      result.result === "applied"
    ) {
      lastApplied = {service, event};
    }

  } catch (error) {
    $("result-status").textContent = "Request failed";
    $("result-status").className = "result-status error";
    $("result-output").textContent = error.message;
    $("result-explanation").textContent =
      "Verify the dashboard and Cloud API services are running.";
  } finally {
    button.disabled = false;
    button.textContent = "Execute Test";
  }
}

function metricRow(label, value) {
  const row = document.createElement("div");
  row.className = "metric-row";

  const name = document.createElement("span");
  name.textContent = label;

  const output = document.createElement("strong");
  output.textContent = String(value);

  row.append(name, output);
  return row;
}

function metricValue(value, unit = "") {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? `${value}${unit}` : "Unavailable";
}

function controllerMetricRows(c) {
  if (c?.status !== "available") return [
    ["Connection", c?.status === "not_configured" ? "Not configured" : "Unavailable"],
    ["Controller state", "Unknown"]
  ];
  return [
    ["State", c.state ?? "Unknown"],
    ["Adaptive admission", c.enabled === true ? "Enabled" : c.enabled === false ? "Disabled" : "Unknown"],
    ["Admission rate", metricValue(c.rate_rps, " req/s")],
    ["Telemetry freshness", c.telemetry_fresh === true ? "Fresh" : c.telemetry_fresh === false ? "Stale or unavailable" : "Unknown (not exposed)"],
    ["Policy", c.policy ?? "Not exposed"],
    ["Inflight limit", metricValue(c.inflight_limit)],
    ["Allowed / denied", `${c.allowed ?? "Unknown"} / ${c.denied ?? "Unknown"}`],
    ["Observed at", c.observed_at ?? "Unknown"],
    ["Active grants / fairness", "Not exposed by admission endpoint"]
  ];
}

function showMetrics(data) {
  const grid = $("metrics-grid");
  grid.replaceChildren();
  for (const name of ["patient", "emergency", "admin"]) {
    const entry = data?.[name];
    const card = document.createElement("article");
    card.className = "card";
    const title = document.createElement("div");
    title.className = "metric-header";
    title.textContent = name[0].toUpperCase() + name.slice(1) + " API";
    card.appendChild(title);
    const m = entry?.response;
    if (entry?.http_status !== 200 || !m || typeof m !== "object" || Array.isArray(m)) {
      const connectionFailed = entry?.http_status === null && m?.code === "SERVICE_UNREACHABLE";
      card.appendChild(metricRow("Status", connectionFailed ? "Connection failed" : "Unavailable"));
    } else {
      const samples = m.latency_sample_count ?? m.window_requests;
      const p95 = m.p95_latency_ms === null && samples === 0
        ? "No samples"
        : metricValue(m.p95_latency_ms, " ms");

      const liveSamples = m.live_latency_sample_count;
      const liveCountKnown = Number.isInteger(liveSamples) && liveSamples >= 0;
      const liveP95 = !liveCountKnown ? "Unavailable"
        : liveSamples === 0
          ? (m.live_p95_latency_ms === null ? "No samples" : "Unavailable")
          : metricValue(m.live_p95_latency_ms, " ms");

      const sloConfigured =
        typeof m.live_slo_threshold_ms === "number" &&
        Number.isFinite(m.live_slo_threshold_ms) &&
        m.live_slo_threshold_ms > 0;
      const sloRate = !liveCountKnown ? "Unavailable" : liveSamples === 0
          ? "No live samples"
          : typeof m.live_slo_violation_rate === "number" &&
            Number.isFinite(m.live_slo_violation_rate) &&
            m.live_slo_violation_rate >= 0 && m.live_slo_violation_rate <= 1
            ? `${(m.live_slo_violation_rate * 100).toFixed(2)}%`
            : "Unavailable";

      const executorPressure =
        typeof m.executor_queue_pressure === "number" &&
        Number.isFinite(m.executor_queue_pressure)
          ? `${(m.executor_queue_pressure * 100).toFixed(2)}%`
          : "Unavailable";
      for (const [label, value] of [
        ["Mean latency", metricValue(m.average_latency_ms, " ms")],
        ["p95 latency", p95],
        ["Live p95 latency", liveP95],
        ...(sloConfigured ? [
          ["Live SLO threshold", metricValue(m.live_slo_threshold_ms, " ms")],
          ["Live SLO violations", sloRate],
          ["Live SLO violation count", !liveCountKnown ? "Unavailable" : liveSamples === 0 ? "No live samples" : metricValue(m.live_slo_violation_count)]
        ] : []),
        ["Live request samples", metricValue(liveSamples)],
        ["Replay request samples", metricValue(m.replay_latency_sample_count)],
        ["Unclassified request samples", metricValue(m.unclassified_latency_sample_count)],
        ["Executor queue depth", metricValue(m.executor_queue_depth)],
        ["Executor queue pressure", executorPressure],
        ["Throughput", metricValue(m.throughput_rps, " req/s")],
        ["Error rate", typeof m.error_rate === "number" && m.error_rate <= 1 ? metricValue(m.error_rate * 100, "%") : "Unavailable"],
        ["In-flight requests", metricValue(m.in_flight_requests)],
        ["PostgreSQL", m.database_healthy === true ? "Healthy" : m.database_healthy === false ? "Unavailable" : "Unknown"],
        ["Rolling window", metricValue(m.window_seconds, " s")],
        ["Observed duration", metricValue(m.observation_seconds, " s")],
        ["Latency samples", metricValue(samples)],
        ["Window requests", metricValue(m.window_requests)],
        ["Total requests", metricValue(m.total_requests)]
      ]) card.appendChild(metricRow(label, value));
    }
    grid.appendChild(card);
  }
}

let dashboardRefreshPending = null;
async function loadDashboard() {
  try {
    const options = typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function"
      ? {signal: AbortSignal.timeout(15000)} : {};
    const [healthResponse, metricsResponse] = await Promise.all([
      fetch("/api/services", options),
      fetch("/api/metrics", options)
    ]);

    if (!healthResponse.ok || !metricsResponse.ok) {
      throw new Error("Dashboard API returned an error");
    }

    const health = await healthResponse.json();
    const metrics = await metricsResponse.json();

    for (const name of ["patient", "emergency", "admin"]) {
      const entry = health[name];
      const healthy =
        entry?.http_status === 200 &&
        entry?.response?.status === "healthy";

      const element = $(`${name}-health`);
      element.textContent = healthy ? "Healthy" : "Unavailable";
      element.className =
        `health ${healthy ? "healthy" : "unhealthy"}`;
    }

    showMetrics(metrics);
    if (typeof window !== "undefined") window.workspaceMetrics?.(metrics);
    $("updated").textContent =
      `Updated ${new Date().toLocaleTimeString()}`;

  } catch (error) {
    $("updated").textContent = "Connection error";
    if (typeof window !== "undefined") window.workspaceMetrics?.(null);
    for (const name of ["patient", "emergency", "admin"]) {
      const element = $(`${name}-health`);
      element.textContent = "Unknown";
      element.className = "health unhealthy";
    }
    $("metrics-grid").textContent =
      "Unable to retrieve telemetry: " +
      (error.name === "TimeoutError" ? "Request timed out after 15 seconds" : error.message);
  }
}

function refreshDashboard() {
  if (dashboardRefreshPending) return dashboardRefreshPending;
  dashboardRefreshPending = loadDashboard().finally(() => { dashboardRefreshPending = null; });
  return dashboardRefreshPending;
}

async function refreshNow() {
  const button = $("refresh");
  if (button.disabled) return;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "Refreshing…";
  try { await refreshDashboard(); }
  finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.textContent = "Refresh now";
  }
}

$("service").addEventListener("change", () => {
  populateEventTypes();
  setScenario();
});
$("scenario").addEventListener("change", setScenario);

$("generate-id").addEventListener("click", () => {
  $("event-id").value = newId("DASH");
});

$("submit-event").addEventListener("click", sendTest);
$("refresh").addEventListener("click", refreshNow);

populateEventTypes();
setScenario();
refreshDashboard();
setInterval(refreshDashboard, 5000);



// ==========================================
// CrisisGuard Cloud Event History
// ==========================================

let cloudEvents = [];

async function loadEventHistory() {
  if (loadEventHistory.pending) {
    const requestedLimit = $("history-limit").value;
    const result = await loadEventHistory.pending;
    if (requestedLimit !== loadEventHistory.pendingLimit) return loadEventHistory();
    return result;
  }
  loadEventHistory.pendingLimit = $("history-limit").value;
  loadEventHistory.pending = loadEventHistoryRequest();
  try { return await loadEventHistory.pending; }
  finally { loadEventHistory.pending = null; }
}

async function loadEventHistoryRequest() {
  const limit = $("history-limit").value;
  const countElement = $("history-count");

  countElement.textContent = "Loading events...";

  try {
    const response = await fetch(
      "/api/history?limit=100",
      typeof AbortSignal !== "undefined" ? {signal: AbortSignal.timeout(15000)} : {}
    );

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const data = await response.json();

    if (!Array.isArray(data.events)) throw new Error("Invalid history response");
    if (limit !== $("history-limit").value) return null;
    loadEventHistory.error = null;
    cloudEvents = data.events.slice(0, Number(limit));

    renderEventHistory();
    return data.events;

  } catch (error) {
    cloudEvents = [];
    loadEventHistory.error = `Unable to load event history: ${error.message}`;
    countElement.textContent = loadEventHistory.error;

    $("history-body").replaceChildren();
    return null;
  }
}

function renderEventHistory() {
  if (loadEventHistory.error) {
    $("history-count").textContent = loadEventHistory.error;
    const tbody = $("history-body");
    tbody.replaceChildren();
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 7;
    cell.textContent = "History unavailable. Use Refresh Events to retry.";
    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }
  const search = $("history-search").value.toLowerCase();

  const filtered = cloudEvents.filter(event => {
    const content = [
      event.event_id,
      event.site_id,
      event.event_type,
      event.status
    ].join(" ").toLowerCase();

    return content.includes(search);
  });

  $("history-count").textContent =
    `Showing ${filtered.length} of ${cloudEvents.length} recent events`;

  const tbody = $("history-body");
  tbody.replaceChildren();

  if (filtered.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");

    cell.colSpan = 7;
    cell.textContent = "No matching events found.";

    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }

  for (const event of filtered) {
    const row = document.createElement("tr");

    function addCell(text, className = "") {
      const cell = document.createElement("td");
      cell.textContent = String(text ?? "");
      if (className) cell.className = className;
      row.appendChild(cell);
      return cell;
    }

    addCell(event.event_id, "event-id");
    addCell(event.site_id);
    addCell(event.event_type);
    addCell(event.base_priority);

    const statusCell = addCell("");
    const statusBadge = document.createElement("span");
    statusBadge.className = "event-badge";
    statusBadge.textContent = event.status;
    statusCell.appendChild(statusBadge);

    addCell(
      event.applied_at
        ? new Date(event.applied_at).toLocaleString()
        : "Unknown"
    );

    const detailsCell = addCell("");
    const button = document.createElement("button");

    button.className = "details-button";
    button.textContent = "View";

    button.addEventListener("click", () => {
      showEventDetails(event);
    });

    detailsCell.appendChild(button);

    tbody.appendChild(row);
  }
}

function showEventDetails(event) {
  $("event-details-json").textContent =
    JSON.stringify(event, null, 2);

  $("event-details").hidden = false;

  $("event-details").scrollIntoView({
    behavior: "smooth",
    block: "nearest"
  });
}

$("refresh-history").addEventListener(
  "click",
  loadEventHistory
);

$("history-search").addEventListener(
  "input",
  renderEventHistory
);

$("history-limit").addEventListener(
  "change",
  loadEventHistory
);

$("close-details").addEventListener("click", () => {
  $("event-details").hidden = true;
});

// Load existing PostgreSQL events at startup.
loadEventHistory();


/* Hospital Edge Simulation */

async function refreshEdge() {
  if (refreshEdge.pending) return refreshEdge.pending;
  refreshEdge.pending = (async () => {
  try {
    const response = await fetch("/api/hospitals",
      typeof AbortSignal !== "undefined" ? {signal: AbortSignal.timeout(15000)} : {});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const hospitals = await response.json();
    if (typeof window !== "undefined") window.workspaceHospitals?.(hospitals);
    const tbody = $("edge-events-body");
    tbody.replaceChildren();

    for (const [key, prefix, label] of [
      ["hospital-a", "edge-a", "Hospital A"],
      ["hospital-b", "edge-b", "Hospital B"]
    ]) {
      const hospital = hospitals[key];

      const status = $(`${prefix}-status`);
      const healthy = hospital?.status === "healthy";

      status.textContent = healthy ? "Healthy" : "Unavailable";
      status.className =
        `health ${healthy ? "healthy" : "unhealthy"}`;

      $(`${prefix}-total`).textContent = healthy ? hospital.total : "Unknown";
      $(`${prefix}-pending`).textContent = healthy ? hospital.pending : "Unknown";
      $(`${prefix}-applied`).textContent = healthy ? hospital.applied : "Unknown";

      if (!healthy) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 5;
        cell.textContent = `${label}: unable to retrieve queue. ${hospital?.error ?? "Hospital API unavailable"}`;
        row.appendChild(cell);
        tbody.appendChild(row);
        continue;
      }

      
        for (const event of hospital?.events ?? []) {
        // Hide successfully replayed events by default.
        const showApplied = $("show-applied-edge").checked;

        if (!showApplied && event.status !== "pending") {
            continue;
        }

        const row = document.createElement("tr");

        for (const value of [
          label,
          event.event_id,
          event.event_type,
          event.base_priority,
          event.status
        ]) {
          const cell = document.createElement("td");
          cell.textContent = String(value ?? "");
          row.appendChild(cell);
        }

        tbody.appendChild(row);
      }
    }

    if (!tbody.children.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 5;
      cell.textContent = $("show-applied-edge").checked
        ? "No hospital events recorded yet." : "No pending events.";
      row.appendChild(cell);
      tbody.appendChild(row);
    }

    return hospitals;
  } catch (error) {
    if (typeof window !== "undefined") window.workspaceHospitals?.(null);
    for (const prefix of ["edge-a", "edge-b"]) {
      $(`${prefix}-status`).textContent = "Unknown";
      $(`${prefix}-status`).className = "health unhealthy";
      for (const field of ["total", "pending", "applied"]) $(`${prefix}-${field}`).textContent = "Unknown";
    }
    $("edge-events-body").textContent =
      `Could not retrieve hospital events: ${error.message}`;
    return null;
  }
  })();
  try { return await refreshEdge.pending; }
  finally { refreshEdge.pending = null; }
}

async function captureEdgeEvent() {
  $("edge-result-status").syncOwner = null;
  const hospital = $("edge-hospital").value;
  const siteId =
    hospital === "hospital-a" ? "Hospital_A" : "Hospital_B";
  const prerequisiteIds = $("edge-prerequisites").value
    .split(",")
    .map(value => value.trim())
    .filter(Boolean);

  const event = {
    event_id: $("edge-event-id").value.trim(),
    site_id: siteId,
    event_type: $("edge-event-type").value,
    base_priority: Number($("edge-priority").value),
    prerequisite_ids: prerequisiteIds,
    payload: {
      patient_id: newId("EDGE_PATIENT"),
      test_source: "crisisguard_edge_dashboard"
    }
  };

  const button = $("capture-edge");
  button.disabled = true;

  try {
    const response = await fetch(
      `/api/hospitals/${hospital}/capture`,
      {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(event)
      }
    );

    const data = await response.json();
    const status = Object.hasOwn(data, "http_status")
      ? data.http_status : response.status;

    $("edge-result-status").textContent =
      `HTTP ${status} | ${
        status === 201 ? "Stored Locally (Pending Synchronization)" :
        (status >= 400 && status < 500 ? "Capture rejected" : "Capture unconfirmed")
      }`;

    $("edge-result-status").className =
      `result-status ${status === 201 ? "success" : "error"}`;

    $("edge-result-output").textContent =
      JSON.stringify(data.response ?? data, null, 2);

    if (status === 201) {
      if (typeof window !== "undefined") window.eventSync?.track(event, hospital, $("edge-result-status"));
      $("edge-event-id").value = newId("EDGE");
      $("edge-prerequisites").value = "";
    }

    await refreshEdge();

  } catch (error) {
    $("edge-result-status").textContent = "Capture unconfirmed ? check queue before retrying";
    $("edge-result-status").className = "result-status error";
    $("edge-result-output").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

$("edge-event-id").value = newId("EDGE");

$("refresh-edge").addEventListener("click", refreshEdge);
$("capture-edge").addEventListener("click", captureEdgeEvent);

refreshEdge();
// Queue/history cadence is owned by sync-status.js.



// ==========================================
// Manual Hospital-to-Cloud Replay
// ==========================================

async function replayHospital(hospital) {
  const buttonA = $("replay-a");
  const buttonB = $("replay-b");
  const tokenInput = $("replay-token");
  const operatorToken = tokenInput.value;
  tokenInput.value = "";

  if (!operatorToken) {
    $("replay-status").textContent = "Replay authorization required";
    $("replay-status").className = "result-status error";
    $("replay-output").textContent = "Enter the replay operator token before retrying.";
    return;
  }

  buttonA.disabled = true;
  buttonB.disabled = true;

  $("replay-status").textContent = "Replaying pending events...";
  $("replay-status").className = "result-status waiting";

  try {
    const response = await fetch(
      `/api/hospitals/${hospital}/replay`,
      { method: "POST", headers: {Authorization: `Bearer ${operatorToken}`} }
    );

    const data = await response.json();

    if (!response.ok || data.http_status !== 200) {
      throw new Error(JSON.stringify(data.detail ?? data));
    }

    const result = data.response;
    const applied = result.applied ?? 0;
    const attempted = result.total_attempted ?? 0;

    $("replay-status").textContent =
      `${hospital}: ${applied} applied out of ${attempted} attempted`;

    $("replay-status").className =
      `result-status ${
        applied === attempted ? "success" : "waiting"
      }`;

    $("replay-output").textContent =
      JSON.stringify(result, null, 2);

    // Refresh SQLite event queues and PostgreSQL history.
    await Promise.all([
      refreshEdge(),
      loadEventHistory(),
      refreshDashboard()
    ]);

  } catch (error) {
    $("replay-status").textContent =
      "Replay failed or timed out";

    $("replay-status").className =
      "result-status error";

    $("replay-output").textContent = error.message;

    await refreshEdge();

  } finally {
    buttonA.disabled = false;
    buttonB.disabled = false;
  }
}

$("replay-a").addEventListener("click", () => {
  replayHospital("hospital-a");
});

$("replay-b").addEventListener("click", () => {
  replayHospital("hospital-b");
});



// Refresh button directly above SQLite queue
$("refresh-edge-queue").addEventListener(
  "click",
  refreshEdge
);

// Toggle between pending-only and all events
$("show-applied-edge").addEventListener(
  "change",
  refreshEdge
);


async function previewHospitalScheduler() {
  const hospital = $("scheduler-hospital").value;
  const inheritance = $("scheduler-inheritance").value;
  const selectionMatches = () => $("scheduler-hospital").value === hospital &&
    $("scheduler-inheritance").value === inheritance;

  const button = $("preview-scheduler");
  const summary = $("scheduler-summary");
  const tbody = $("scheduler-events-body");

  button.disabled = true;
  summary.textContent = "Loading scheduler preview...";
  tbody.replaceChildren();

  try {
    const query = inheritance === "true" || inheritance === "false"
      ? `?inheritance=${inheritance}` : "";
    const response = await fetch(
      `/api/hospitals/${hospital}/scheduler/inspect${query}`
    );

    if (!response.ok) {
      throw new Error(
        `Preview unavailable (HTTP ${response.status})`
      );
    }

    const preview = await response.json();
    if (!selectionMatches()) return;

    // Never display applied events.
    const pending = preview.events.filter(
      event => event.reason !== "Already applied"
    );

    // Display current scheduling decisions in useful order.
    pending.sort((a, b) => {
      // 1. Always show the selected event first.
      if (a.selected !== b.selected) {
        return a.selected ? -1 : 1;
      }

      // 2. Other eligible events appear before blocked events.
      if (a.eligible !== b.eligible) {
        return a.eligible ? -1 : 1;
      }

      // 3. Among eligible events, higher priority comes first.
      if (a.eligible && b.eligible) {
        const priorityDifference =
          b.effective_priority - a.effective_priority;

        if (priorityDifference !== 0) {
          return priorityDifference;
        }
      }

      // 4. For equal priorities, older events come first.
      // All timestamps are normalised to UTC by Python.
      if (a.created_at !== b.created_at) {
        return a.created_at < b.created_at ? -1 : 1;
      }

      // 5. Final tie-breaker: event ID.
      return a.event_id < b.event_id
        ? -1
        : a.event_id > b.event_id
          ? 1
          : 0;
    });


    const policy = preview.replay_policy;
    const activePolicy = typeof policy?.inheritance_enabled === "boolean"
      ? `Active replay: inheritance ${policy.inheritance_enabled ? "ON" : "OFF"}, ageing ${policy.ageing_interval_seconds} s`
      : "Active replay policy unavailable";
    const policyMatch = preview.comparison_only === true
      ? "Comparison only; replay policy unchanged"
      : preview.comparison_only === false
        ? "Preview uses active replay policy" : "Policy match unavailable";

    summary.textContent =
      `Scheduler preview (not an executed grant or replay) | ` +
      `Next selection: ${preview.next_event_id ?? "none"} | ` +
      `Pending: ${preview.pending_count} | ` +
      `Inheritance: ${preview.inheritance_enabled ? "ON" : "OFF"} | ` +
      `As of: ${preview.evaluated_at}` +
      (preview.outstanding_event_id ? ` | Outstanding attempt: ${preview.outstanding_event_id}` : "") +
      (preview.replay_wait_reason ? ` | ${preview.replay_wait_reason}` : "") +
      ` | ${activePolicy} | ${policyMatch}` +
      ` | ${preview.warning ?? "Controller permission may still be required."}`;

    if (pending.length === 0) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");

      cell.colSpan = 10;
      cell.textContent = "No pending events.";

      row.appendChild(cell);
      tbody.appendChild(row);
    }

    for (const event of pending) {
      const row = document.createElement("tr");

      if (event.selected) {
        row.style.backgroundColor = "rgba(34, 197, 94, 0.10)";
        row.style.fontWeight = "600";
      }

      const values = [
        event.event_id,
        event.base_priority,
        event.aged_priority,
        event.effective_priority ?? "—",
        event.inherited_from ?? "—",
        event.prerequisite_ids?.length ? event.prerequisite_ids.join(", ") : "None",
        event.waiting_on?.length ? event.waiting_on.join(", ") : "None",
        event.eligible ? "Yes" : "No",
        event.selected ? "Preview selects next" : event.reason,
        event.selected ? "PREVIEW NEXT" : "",
      ];

      for (const value of values) {
        const cell = document.createElement("td");

        cell.textContent = String(value);
        row.appendChild(cell);
      }

      tbody.appendChild(row);
    }

  } catch (error) {
    if (selectionMatches()) {
      summary.textContent = `Preview failed: ${error.message}`;
    }

  } finally {
    button.disabled = false;
  }
}

function clearSchedulerPreview() {
  $("scheduler-summary").textContent = "Selection changed. Load a new scheduler preview.";
  $("scheduler-events-body").replaceChildren();
}

for (const id of ["scheduler-hospital", "scheduler-inheritance"]) {
  $(id).addEventListener("change", clearSchedulerPreview);
}

function openSchedulerInspector() {
  const inspector = $('scheduler-inspector');
  inspector.open = true;
  inspector.scrollIntoView({behavior: 'smooth', block: 'start'});
}
$('open-scheduler-inspector').addEventListener('click', openSchedulerInspector);


$("preview-scheduler").addEventListener(
  "click",
  previewHospitalScheduler
);
