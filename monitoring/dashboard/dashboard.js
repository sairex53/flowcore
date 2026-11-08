"use strict";

const $ = (id) => document.getElementById(id);
const root = $("view");
const state = {
  token: "", paused: false, timer: null, abort: null, view: "overview", interval: 2,
  interface: null, service: null, range: 120, policyOffset: 0, decision: null,
  incident: null, eventKind: "", incidentOffset: 0, event: null,
  artifacts: {benchmarks: null, multipath: null}, run: {benchmarks: 0, multipath: 0},
  benchmarkOffset: {benchmarks: 0, multipath: 0},
};
const pages = {
  overview: ["Overview", "Network overview", "NETWORK OPERATIONS", "Traffic, queues and application health from the current monitoring scope."],
  slo: ["Service SLOs", "Service objectives", "APPLICATION HEALTH", "Request latency and outcomes from instrumented application windows."],
  controller: ["Controller", "Controller decisions", "CONTROL PLANE", "Follow evaluations, proposed allocations, kernel verification and rollbacks."],
  bottlenecks: ["Bottlenecks", "Bottleneck evidence", "NETWORK DIAGNOSTICS", "See where pressure was observed and what the evidence can support."],
  incidents: ["Incidents", "Incident explorer", "FLIGHT RECORDER", "Inspect retained events and diagnostic findings in their original sequence."],
  multipath: ["Multipath", "Multipath connections", "EXPERIMENTAL TRANSPORT", "Inspect current-namespace MPTCP sockets and recorded path experiments."],
  benchmarks: ["Experiments", "Benchmark explorer", "RESEARCH ARTIFACTS", "Compare independent runs within the selected experiment matrix."],
};
const palette = ["#63e4c7", "#8baefc", "#bc9bff", "#f5bf6e"];
const num = (value, digits = 1) => typeof value === "number" && Number.isFinite(value)
  ? value.toLocaleString(undefined, {maximumFractionDigits: digits}) : "—";
const pct = (value) => value == null ? "—" : num(value * 100, 2) + "%";
const mbps = (value) => value == null ? "—" : num(value / 1000, 3);
const when = (value) => typeof value === "number" ? new Date(value).toLocaleTimeString() : "—";
const label = (value) => value == null ? "Unavailable" : String(value).replaceAll("_", " ");
const yes = (value) => value == null ? "Unknown" : value ? "Yes" : "No";

function node(tag, className, ...children) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.append(...children.filter((value) => value != null));
  return element;
}
function panel(title, subtitle, controls) {
  return node("section", "panel", node("div", "panel-head",
    node("div", "", node("h2", "", title), subtitle ? node("p", "", subtitle) : null), controls));
}
function empty(title, detail) {
  return node("div", "empty", node("strong", "", title), detail);
}
function tag(text, color = "") {
  return node("span", "tag " + color, text);
}
function kv(entries) {
  return node("dl", "key-value", ...entries.flatMap(([title, value]) => [
    node("dt", "", title), node("dd", "", value ?? "—"),
  ]));
}
function table(columns, rows, fallback = "No rows in this view") {
  if (!rows.length) return empty(fallback, "The current source has no matching observations.");
  const view = node("table", "", node("thead", "", node("tr", "", ...columns.map((value) => {
    const head = node("th", "", value);
    head.scope = "col";
    return head;
  }))), node("tbody", "", ...rows.map((values) =>
    node("tr", "", ...values.map((value) => node("td", "", value ?? "—"))))));
  return node("div", "scroller", view);
}
function picker(title, values, value, callback) {
  const select = node("select", "");
  select.setAttribute("aria-label", title);
  for (const [key, text] of values) {
    const option = node("option", "", text);
    option.value = key;
    select.append(option);
  }
  select.value = value ?? "";
  select.addEventListener("change", () => callback(select.value));
  return node("label", "controls", title, select);
}
function button(text, callback, disabled = false) {
  const element = node("button", "quiet", text);
  element.type = "button";
  element.disabled = disabled;
  element.addEventListener("click", callback);
  return element;
}
function details(title, value) {
  return node("details", "", node("summary", "", title), node("pre", "", JSON.stringify(value, null, 2)));
}
function pager(page, move) {
  return node("div", "timeline-header",
    node("span", "", page.total ? String(page.offset + 1) + "–" + (page.offset + page.items.length)
      + " of " + page.total : "0 entries"),
    node("div", "controls", button("Previous", () => move(Math.max(0, page.offset - page.limit)), !page.offset),
      button("Next", () => move(page.offset + page.limit), page.offset + page.limit >= page.total)));
}
function stat(title, value, unit, description) {
  return node("article", "stat", node("div", "stat-title", title),
    node("div", "stat-number", value, node("small", "", unit)), node("div", "stat-description", description));
}

async function api(path, params, signal) {
  const search = params ? "?" + new URLSearchParams(params) : "";
  const response = await fetch("/api/v1/" + path + search, {
    headers: {Authorization: "Bearer " + state.token}, cache: "no-store", signal,
  });
  if (response.status === 401) {
    lock("Token rejected. Enter a valid token to continue.");
    throw new Error("Authorization required");
  }
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Monitoring request failed");
  if (body.schema !== 1) throw new Error("Unsupported monitoring API version");
  return body;
}
function svg(tagName, attributes = {}, content) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tagName);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  if (content != null) element.textContent = content;
  return element;
}
function chart(history, traces, options = {}) {
  const points = history?.points ?? [];
  const observed = points.flatMap((point) => traces.map(([, key, transform = (x) => x]) => {
    const value = point.values?.[key];
    return typeof value === "number" ? transform(value) : null;
  })).filter((value) => Number.isFinite(value));
  if (!observed.length) return empty("No historical samples", "Values appear as the collector records eligible observations.");
  const width = 650, height = 215, left = 44, right = 16, top = 12, bottom = 28;
  const plot = svg("svg", {viewBox: "0 0 650 215", role: "img", "aria-label": options.title ?? "Metric history"});
  const start = history.from_ms, end = Math.max(start + 1, history.to_ms);
  const max = Math.max(options.floor ?? 1, ...observed) * 1.07;
  const x = (stamp) => left + (stamp - start) / (end - start) * (width - left - right);
  const y = (value) => height - bottom - Math.max(0, value) / max * (height - top - bottom);
  for (let i = 0; i <= 4; i++) {
    const value = max * i / 4;
    plot.append(svg("line", {x1: left, x2: width - right, y1: y(value), y2: y(value), class: "gridline"}),
      svg("text", {x: left - 6, y: y(value) + 4, "text-anchor": "end"}, num(value, max < .5 ? 3 : 1)));
  }
  for (let i = 0; i < 4; i++) {
    const stamp = start + (end - start) * i / 3;
    plot.append(svg("text", {x: x(stamp), y: height - 5, "text-anchor": i === 3 ? "end" : i ? "middle" : "start"}, when(stamp)));
  }
  if (options.highlight) {
    for (const point of points) {
      if (!point.values?.above_target) continue;
      const startBand = Math.max(left, x(point.time_ms) - 3);
      plot.append(svg("rect", {x: startBand, y: top, width: 6, height: height - top - bottom, class: "threshold"}));
    }
  }
  for (const [index, [title, key, transform = (value) => value]] of traces.entries()) {
    let path = "", previous = null;
    const markers = [];
    for (const point of points) {
      const raw = point.values?.[key];
      const value = typeof raw === "number" ? transform(raw) : null;
      if (!Number.isFinite(value)) {
        previous = null;
        continue;
      }
      const contiguous = previous && previous.scope === point.scope && previous.window_seconds === point.window_seconds
        && point.time_ms - previous.time_ms <= Math.max(history.bucket_ms * 2, state.interval * 3000);
      path += (contiguous ? " L " : " M ") + x(point.time_ms) + " " + y(value);
      previous = point;
      const circle = svg("circle", {cx: x(point.time_ms), cy: y(value), r: 3, fill: palette[index],
        class: "marker"});
      circle.append(svg("title", {}, title + " " + num(value, 3) + " at " + when(point.time_ms)));
      markers.push(circle);
    }
    plot.append(svg("path", {d: path, fill: "none", stroke: palette[index], "stroke-width": 2, "vector-effect": "non-scaling-stroke"}), ...markers);
  }
  return node("div", "chart", plot, node("div", "legend", ...traces.map(([title], i) => node("span", "series" + i, title))),
    node("p", "chart-note", "Latest sample per time bucket. Missing observations break the line."));
}
function scatter(runs) {
  const values = runs.filter((run) => run.measured && typeof run.latency.p99_ms === "number");
  if (!values.length) return empty("No measured P99 values", "Invalid and simulated runs are listed separately from recorded measurements.");
  const plot = svg("svg", {viewBox: "0 0 650 180", role: "img", "aria-label": "P99 by recorded repetition"});
  const modes = [...new Set(values.map((row) => row.mode))];
  const max = Math.max(1, ...values.map((row) => row.latency.p99_ms));
  for (let i = 0; i <= 4; i++) {
    const y = 148 - i * 32;
    plot.append(svg("line", {x1: 50, x2: 633, y1: y, y2: y, class: "gridline"}),
      svg("text", {x: 44, y: y + 4, "text-anchor": "end"}, num(i * max / 4, 1)));
  }
  for (const [index, mode] of modes.entries()) {
    const x = 50 + (index + .5) * 583 / modes.length;
    plot.append(svg("text", {x, y: 169, "text-anchor": "middle"}, String(mode).slice(0, 17)));
    values.filter((run) => run.mode === mode).forEach((run, j) => {
      const dot = svg("circle", {cx: x + (j % 9 - 4) * 2.7, cy: 148 - 128 * run.latency.p99_ms / max, r: 4.3,
        fill: palette[index % 4], class: "marker"});
      dot.append(svg("title", {}, mode + " · seed " + (run.seed ?? "unknown") + " · " + num(run.latency.p99_ms, 3) + " ms"));
      plot.append(dot);
    });
  }
  return node("div", "chart", plot, node("p", "chart-note", "Each point is the successful-request P99 from one recorded run on this page."));
}
function history(kind, id, signal) {
  return api("history", {kind, id, seconds: state.range, points: 180}, signal).then((result) => result.data)
    .catch((error) => { if (error.name === "AbortError") throw error; return null; });
}
function rangePicker() {
  return picker("Range", [["120", "2 minutes"], ["900", "15 minutes"], ["3600", "1 hour"]], String(state.range),
    (value) => { state.range = Number(value); refresh(); });
}
function interfacePicker(rows, selected) {
  return picker("Interface", rows.map((row) => [row.id, row.name + " · " + row.scope]), selected,
    (value) => { state.interface = value; refresh(); });
}

function serviceTable(services) {
  return table(["Service", "Class", "P50 ms", "P95 ms", "P99 ms", "Error", "Deadline miss", "Reports/s", "Samples"],
    services.map((row) => [node("strong", "", row.name), row.traffic_class, num(row.p50_ms, 2), num(row.p95_ms, 2),
      row.above_target === true ? tag(num(row.p99_ms, 2), "warn") : num(row.p99_ms, 2),
      pct(row.error_rate), pct(row.deadline_miss_rate), num(row.requests_per_second), num(row.samples, 0)]),
    "No instrumented services");
}
function classTable(classes) {
  return table(["Class", "Direction", "Served Mbit/s", "Backlog B", "Drops Δ", "Guarantee kbit/s"],
    classes.map((row) => [row.name, row.direction, mbps(row.served_kbit), num(row.backlog_bytes, 0),
      num(row.dropped_packets, 0), num(row.guaranteed_kbit, 0)]), "No classified traffic observed");
}
async function overview(signal) {
  const data = (await api("overview", null, signal)).data;
  const selected = data.interfaces.find((row) => row.id === state.interface)
    ?? data.interfaces.find((row) => row.scope === "lab")
    ?? data.interfaces.find((row) => row.name !== "lo") ?? data.interfaces[0];
  if (selected) state.interface = selected.id;
  const egress = selected?.directions?.egress ?? {};
  const critical = data.services.find((row) => row.traffic_class === "critical" && row.p99_ms != null)
    ?? data.services.find((row) => row.traffic_class === "critical");
  const background = data.classes.find((row) => row.name === "background" && row.direction === "egress");
  const series = selected ? await history("interface", selected.id, signal) : null;
  const stats = node("div", "stats",
    stat("Egress throughput", mbps(egress.throughput_kbit), "Mbit/s", selected ? selected.name + " · " + selected.scope : "No monitored interface"),
    stat("Queue backlog", num(egress.backlog_bytes, 0), "B", egress.dropped_packets != null ? num(egress.dropped_packets, 0) + " drops in last sample" : "Queue evidence unavailable"),
    stat("Critical service P99", num(critical?.p99_ms, 2), "ms", critical ? critical.name + " · " + critical.samples + " samples" : "No critical service data"),
    stat("Background throughput", mbps(background?.served_kbit), "Mbit/s", background ? "Measured egress class" : "No background class counters"));
  const traffic = panel("Interface throughput", "Local directional rates in Mbit/s",
    node("div", "controls", selected ? interfacePicker(data.interfaces, selected.id) : null, rangePicker()));
  traffic.append(chart(series, [["Egress", "egress_kbit", (n) => n / 1000], ["Ingress", "ingress_kbit", (n) => n / 1000]], {title: "Interface throughput in Mbit/s", floor: .01}));
  const capacity = panel("Queue and capacity", "Readback and observation provenance");
  capacity.append(kv([["Interface", selected?.name], ["Queue discipline", egress.qdisc],
    ["Observed capacity", egress.capacity_kbit == null ? "Unknown" : mbps(egress.capacity_kbit) + " Mbit/s"],
    ["Capacity source", label(egress.capacity_source)], ["Headroom estimate", egress.idle_headroom_kbit == null ? "Unknown" : mbps(egress.idle_headroom_kbit) + " Mbit/s"],
    ["Controller", tag(label(data.controller.state) + " · " + label(data.controller.mode), data.controller.allocation_verified ? "good" : "")],
    ["Kernel allocation verified", yes(data.controller.allocation_verified)]]));
  const links = panel("Interfaces", "Host inventory and owned lab scope");
  links.append(table(["Device", "Scope", "Link", "MTU", "Egress Mbit/s", "Ingress Mbit/s"],
    data.interfaces.map((row) => [row.name, row.scope, tag(row.up == null ? "Unknown" : row.up ? "Up" : "Down", row.up ? "good" : ""),
      num(row.mtu, 0), mbps(row.directions?.egress?.throughput_kbit), mbps(row.directions?.ingress?.throughput_kbit)])));
  const classes = panel("Traffic classes", "Class rates and queue counters from the owned shaper");
  classes.append(classTable(data.classes));
  const services = panel("Service snapshot", "Application reports over " + data.window_seconds + " seconds");
  services.append(serviceTable(data.services));
  return node("div", "", stats, node("div", "grid", traffic, capacity), node("div", "grid equal", links, classes), services);
}
async function slo(signal) {
  const page = (await api("services", {limit: 64}, signal)).data;
  const selected = page.items.find((row) => row.id === state.service)
    ?? page.items.find((row) => row.traffic_class === "critical" && row.samples > 0) ?? page.items[0];
  if (!selected) return empty("No service telemetry", "Report request outcomes through the FLOWCORE SDK to populate this view.");
  state.service = selected.id;
  const series = await history("service", selected.id, signal);
  const controls = node("div", "controls", picker("Service", page.items.map((row) => [row.id, row.name]),
    selected.id, (value) => { state.service = value; refresh(); }), rangePicker());
  const counts = node("div", "stats", stat("P99 latency", num(selected.p99_ms, 2), "ms", selected.above_target ? "Above configured target" : "Successful-request sample"),
    stat("Deadline misses", pct(selected.deadline_miss_rate), "", selected.deadline_reported + " requests with deadlines"),
    stat("Request errors", pct(selected.error_rate), "", selected.attempts + " reported attempts"),
    stat("Window samples", num(selected.samples, 0), "", selected.window_seconds + " s rolling window"));
  const latency = panel("Request latency", "P50 / P95 / P99 in milliseconds. Red bands mark observed P99 above target.", controls);
  latency.append(chart(series, [["P50", "p50_ms"], ["P95", "p95_ms"], ["P99", "p99_ms"], ["Target", "target_ms"]],
    {highlight: true, title: "Application request latency and SLO target"}));
  const failures = panel("Request outcomes", "Errors / attempts and deadline misses / deadline-bearing requests");
  failures.append(chart(series, [["Error %", "error_rate", (n) => n * 100], ["Deadline miss %", "deadline_miss_rate", (n) => n * 100]]));
  const volume = panel("Sample volume", "Observed reports per second and successful latency samples");
  volume.append(chart(series, [["Reports / s", "requests_per_second"]]), chart(series, [["Samples per window", "samples"]]));
  const summary = panel("All monitored services", "Counts and the latest configured rolling window");
  summary.append(serviceTable(page.items));
  return node("div", "", counts, latency, node("div", "grid equal", failures, volume), summary);
}
function disposition(result) {
  const value = String(result ?? "");
  if (/rollback|reject|fault|degrad|failed/.test(value)) return "bad";
  if (/verif|retain|restor|success/.test(value)) return "good";
  if (/hypothetical|shadow|observed/.test(value)) return "info";
  if (/pending|inconclusive/.test(value)) return "warn";
  return "";
}
function eventList(rows, selected, key, title, subtitle, stamp, update) {
  return node("div", "timeline", ...rows.map((row) => {
    const id = key(row);
    const item = node("button", "event", tag(label(title(row)), disposition(title(row))),
      node("span", "event-label", node("strong", "", subtitle(row) ?? "Observation")), node("small", "", stamp(row)));
    item.type = "button";
    item.setAttribute("aria-pressed", String(id === selected));
    item.addEventListener("click", () => update(id));
    return item;
  }));
}
async function controller(signal) {
  const [status, log] = await Promise.all([api("controller", null, signal),
    api("policy-history", {offset: state.policyOffset, limit: 24}, signal)]);
  const current = status.data, page = log.data;
  const summary = panel("Controller state", "Current local control mode and readback");
  summary.append(kv([["State", tag(label(current.state), current.allocation_verified ? "good" : "")], ["Mode", current.mode],
    ["Strategy", current.strategy], ["Verified allocation", yes(current.allocation_verified)], ["Applied actions", num(current.actions, 0)],
    ["Reason", current.error ?? current.state_reason], ["Reversals", num(current.reversals, 0)]]));
  if (current.allocation) summary.append(details("Current allocation", current.allocation));
  const decisions = panel("Decision timeline", "Newest decisions first. Select a row for the proposal and outcome.");
  decisions.append(pager(page, (offset) => { state.policyOffset = offset; state.decision = null; refresh(); }));
  const key = (row) => String(row.controller_session) + "/" + row.time_ns;
  let selected = page.items.find((row) => key(row) === state.decision) ?? page.items[0];
  if (selected) {
    state.decision = key(selected);
    decisions.append(eventList(page.items, state.decision, key, (row) => row.result,
      (row) => label(row.action ?? row.reason ?? row.service), (row) => when(row.time_unix == null ? null : row.time_unix * 1000),
      (value) => { state.decision = value; refresh(); }));
  } else decisions.append(empty("No policy decisions", "Start a supported observation or research run to collect controller events."));
  const evidence = panel("Decision detail", "Proposed, applied and verified values retain their recorded distinction");
  if (selected) {
    evidence.append(kv([["Outcome", tag(label(selected.result), disposition(selected.result))], ["Action", label(selected.action)],
      ["Service", selected.service], ["Generation", num(selected.generation, 0)], ["Reason", selected.reason ?? selected.conflict ?? selected.bottleneck_gate],
      ["Feasible", yes(selected.feasible)], ["Comparable", yes(selected.comparable)]]));
    for (const [title, field] of [["Previous allocation", "previous"], ["Proposed allocation", "proposed"],
      ["Recorded allocation", "allocation"], ["Applied value", "applied"], ["Verification timing", "timing"]]) {
      if (selected[field] != null) evidence.append(details(title, selected[field]));
    }
  } else evidence.append(empty("Select a decision", "Details appear when an event is available."));
  return node("div", "", summary, node("div", "grid", decisions, evidence));
}
const advice = {
  OBSERVE: "Collect more eligible samples and check the telemetry scope.",
  REVIEW_EGRESS_ALLOCATION: "Review the owned egress allocation and measured class backlog.",
  REVIEW_EGRESS_SHAPER: "Review the owned egress shaper and observed capacity.",
  REVIEW_INGRESS_IFB: "Review the owned ingress IFB and packet drops.",
  ADVISE: "Investigate the suspected path or receiver with additional evidence.",
};
async function bottlenecks(signal) {
  const page = (await api("bottlenecks", {limit: 64}, signal)).data;
  const row = page.items.find((item) => item.id === state.interface)
    ?? page.items.find((item) => item.scope === "lab") ?? page.items.find((item) => item.interface !== "lo") ?? page.items[0];
  if (!row) return empty("No bottleneck observations", "Wait for an eligible interface sample.");
  state.interface = row.id;
  const overview = panel("Attribution", "Diagnostic evidence and uncertainty",
    interfacePicker(page.items.map((item) => ({...item, name: item.interface})), row.id));
  overview.append(kv([["Location", tag(label(row.classification), row.confidence === "observed_local" ? "good" : "warn")],
    ["State", label(row.state)], ["Confidence", label(row.confidence)], ["Interface", row.interface + " · " + row.scope],
    ["Evidence", row.reason], ["Sample age", row.sample_age_seconds == null ? "Unknown" : num(row.sample_age_seconds) + " s"]]),
    node("p", "note", advice[row.action] ?? "Review the recorded observation and its limitations."));
  const direction = panel("Directional observations", "Capacity is local shaper readback, nominal link speed, or unknown.");
  direction.append(table(["Direction", "Rate Mbit/s", "Capacity Mbit/s", "Backlog B", "Drops Δ", "Provenance"],
    Object.entries(row.directions ?? {}).map(([name, item]) => [name, mbps(item.throughput_kbit), mbps(item.capacity_kbit),
      num(item.backlog_bytes, 0), num(item.dropped_packets, 0), label(item.capacity_uncertainty)])));
  if (row.transport) direction.append(details("Matched TCP evidence", row.transport));
  if (row.application?.length) direction.append(details("Service and class observations", row.application));
  return node("div", "grid", overview, direction);
}
function incidentDescription(row) {
  const data = row.data;
  if (row.kind === "window" && data.p99_us != null) return (row.service ?? "Network") + " · P99 " + num(data.p99_us / 1000, 2) + " ms";
  if (row.kind === "request" && data.duration_us != null) return (row.service ?? "Request") + " · " + label(data.outcome) + " · " + num(data.duration_us / 1000, 2) + " ms";
  if (row.kind === "queue") return [data.interface, data.direction, data.traffic_class,
    data.backlog == null ? null : num(data.backlog, 0) + " B queued",
    data.drops == null ? null : num(data.drops, 0) + " drops"].filter((item) => item != null).join(" · ");
  if (row.kind === "interface") return (data.interface ?? "Interface") + " · " + (data.up == null ? label(data.change) : data.up ? "Up" : "Down");
  return data.action ?? data.result ?? data.reason ?? row.service ?? data.interface ?? "Observation";
}
async function incidents(signal) {
  const list = (await api("incidents", {limit: 16}, signal)).data;
  const index = panel("Retained incidents", list.evicted + " older incidents evicted by flowcored");
  if (!list.items.length) {
    index.append(empty("No retained incidents", "The flight recorder creates bundles when it observes eligible regressions."));
    const live = (await api("overview", null, signal)).data;
    const recent = panel("Recent interface events", "Events visible in the current daemon scope");
    recent.append(table(["Source", "Interface", "Link state"], live.link_events.map((row) => [row.source, row.name, yes(row.up)])));
    return node("div", "", index, recent);
  }
  if (!list.items.some((row) => row.id === state.incident)) state.incident = list.items[0].id;
  index.append(picker("Incident", list.items.map((row) => [row.id, (row.service ?? "Network") + " · " + row.event_count + " events · " + row.id]),
    state.incident, (id) => { state.incident = id; state.incidentOffset = 0; state.eventKind = ""; state.event = null; refresh(); }));
  const params = {id: state.incident, offset: state.incidentOffset, limit: 48};
  if (state.eventKind) params.kind = state.eventKind;
  const page = (await api("incident", params, signal)).data;
  const timeline = panel("Incident timeline", "Elapsed time from the first retained event in this bundle",
    picker("Event kind", [["", "All kinds"], ...page.kinds.map((kind) => [kind, label(kind)])], state.eventKind,
      (value) => { state.eventKind = value; state.incidentOffset = 0; state.event = null; refresh(); }));
  timeline.append(pager(page, (offset) => { state.incidentOffset = offset; state.event = null; refresh(); }));
  const chosen = page.items.find((row) => row.seq === state.event) ?? page.items[0];
  if (chosen) {
    state.event = chosen.seq;
    timeline.append(eventList(page.items, state.event, (row) => row.seq, (row) => row.kind,
      incidentDescription,
      (row) => "+" + num((row.time_ns - page.origin_ns) / 1000000, 1) + " ms",
      (id) => { state.event = id; refresh(); }));
  } else timeline.append(empty("No events in this filter", "Choose another event category."));
  const diagnosis = panel("Diagnostic findings", "Candidate explanations from the recorded evidence");
  diagnosis.append(kv([["Assessment", label(page.report.assessment)], ["Ranking", label(page.report.ranking)],
    ["Signals", page.report.signals.join(" · ") || "None recorded"],
    ["Missing evidence", page.report.missing_evidence.join(" · ") || "None listed"]]));
  for (const candidate of page.report.candidates ?? []) {
    diagnosis.append(node("div", "list-item", tag(label(candidate.strength), "info"),
      node("p", "", label(candidate.name)), details("Evidence and suggested action", candidate)));
  }
  if (chosen) {
    const eventDetail = details("Selected event · " + label(chosen.kind), chosen.data);
    eventDetail.open = true;
    diagnosis.append(eventDetail);
  }
  return node("div", "", index, node("div", "grid", timeline, diagnosis));
}

async function experiment(view, signal) {
  const library = (await api("benchmarks", null, signal)).data;
  const ids = library.items.map((row) => row.id);
  const id = ids.includes(state.artifacts[view]) ? state.artifacts[view]
    : (view === "multipath" ? ids.find((name) => /mptcp|multipath/i.test(name)) : ids.find((name) => !/mptcp|multipath/i.test(name))) ?? ids[0];
  state.artifacts[view] = id;
  const heading = panel(view === "multipath" ? "Recorded path experiments" : "Recorded experiment matrices",
    "Only datasets explicitly configured by the operator appear here.",
    id ? picker("Dataset", ids.map((name) => [name, name]), id,
      (value) => { state.artifacts[view] = value; state.run[view] = 0; state.benchmarkOffset[view] = 0; refresh(); }) : null);
  if (!id) {
    heading.append(empty("No configured artifacts", "Start the dashboard with an explicit experiment JSON file to enable comparison."));
    return heading;
  }
  const page = (await api("benchmark", {id, limit: 32, offset: state.benchmarkOffset[view]}, signal)).data;
  heading.append(node("div", "controls", tag(label(page.kind), "info"),
    tag(page.provenance === "simulated" ? "Simulated · excluded from measured comparison" : "Recorded artifact",
      page.provenance === "simulated" ? "warn" : "good"),
    node("small", "", page.measured_runs + " measured runs · " + page.total + " total")));
  heading.append(node("p", "note", page.source_note));
  if (view === "multipath" && page.kind !== "multipath") {
    heading.append(empty("Choose an MPTCP artifact", "This dataset does not contain experimental subflow or path observations."));
    return heading;
  }
  const selected = page.items.find((row) => row.index === state.run[view]) ?? page.items[0];
  if (selected) state.run[view] = selected.index;
  const result = selected ? (await api("benchmark-run", {id, run: selected.index}, signal)).data.run : null;
  const distributions = panel("Successful-request P99", page.units.latency + " · milliseconds");
  distributions.append(scatter(page.items));
  const rows = panel("Run comparison", "Throughput · " + page.units.throughput);
  rows.append(pager(page, (offset) => { state.benchmarkOffset[view] = offset; state.run[view] = offset; refresh(); }),
    table(["Mode / strategy", "Seed", "Status", "P99 ms", "Goodput Mbit/s", "Fairness", "CPU s", "Actions"],
      page.items.map((row) => [button(row.mode ?? "Run " + row.index, () => { state.run[view] = row.index; refresh(); }),
        row.seed, tag(label(row.status), row.measured ? "good" : "warn"), num(row.latency.p99_ms, 2),
        num(row.throughput_mbps, 3), num(row.fairness, 3), num(row.cpu_seconds, 3), num(row.controller_actions, 0)])));
  const detail = panel("Selected run", result ? (result.mode ?? "Run") + " · seed " + (result.seed ?? "unknown") : "");
  if (result) {
    detail.append(kv([["P50 / P95 / P99 ms", [result.latency.p50_ms, result.latency.p95_ms, result.latency.p99_ms].map((n) => num(n, 2)).join(" / ")],
      ["Errors", num(result.latency.errors, 0)], ["Deadline misses", num(result.latency.deadline_misses, 0)],
      ["Throughput", num(result.throughput_mbps, 3) + " Mbit/s"], ["Fairness", num(result.fairness, 3)],
      ["CPU", num(result.cpu_seconds, 3) + " s"]]));
    detail.append(details("Run configuration", result.configuration));
    if (result.transport) {
      detail.append(kv([["Transport", result.transport.transport], ["MPTCP confirmed", yes(result.transport.mptcp_confirmed)],
        ["TCP fallback", yes(result.transport.fallback_observed)], ["Observed subflows", num(result.transport.observed_mptcp_subflows, 0)],
        ["Connection survived", yes(result.transport.connection_survived)],
        ["Subflow sources", result.transport.observed_subflow_source_addresses?.join(" · ") || "Unknown"]]));
      detail.append(table(["Path", "Interface", "RTT P95 ms", "Availability", "Goodput Mbit/s"],
        (result.paths ?? []).map((item) => [item.path_id, item.interface, num(item.rtt_p95_ms, 2),
          pct(item.availability), num(item.throughput_mbps, 2)])));
      const transitions = result.transitions ?? [];
      if (transitions.length) {
        detail.append(table(["Event", "Path", "Local link", "Peer link", "Apply ms"],
          transitions.map((row) => [row.name ?? row.event, row.path_id, yes(row.kernel_state?.link_up),
            yes(row.kernel_state?.peer_link_up), num(row.operation_latency_ns == null ? null : row.operation_latency_ns / 1000000, 2)])));
        detail.append(details("Recorded transition and subflow evidence", transitions));
      }
    }
    if (result.recovery) detail.append(details("Failure and recovery", result.recovery));
    if (result.decisions?.length) {
      detail.append(table(["Order", "Outcome", "Action", "Elapsed s"],
        result.decisions.map((row, i) => [i + 1, label(row.result ?? row.trigger),
          label(row.action ?? row.strategy), num(row.time_ns != null && result.decisions[0].time_ns != null
            ? (row.time_ns - result.decisions[0].time_ns) / 1e9 : null, 3)])));
      detail.append(details("Controller decision evidence", result.decisions));
    }
  }
  return node("div", "", heading, node("div", "grid", distributions, detail), rows);
}
async function multipath(signal) {
  const response = await api("multipath", null, signal);
  const row = response.data;
  const live = panel("Current MPTCP scope", "Read-only socket inventory in the dashboard process network namespace");
  if (!row) live.append(empty("MPTCP probe unavailable", "Wait for the first local socket inventory sample."));
  else {
    live.append(kv([["Kernel enabled", yes(row.kernel_enabled)], ["Sample", when(row.sampled_ms)],
      ["Inventory truncated", yes(row.truncated)], ["Configured endpoints", row.endpoints == null ? "Unknown" : String(row.endpoints.length)]]));
    if (row.connections == null) live.append(empty("Socket inventory unavailable", "The local ss probe is absent, unsupported or denied."));
    else live.append(table(["State", "Local socket", "Peer socket", "Recv-Q", "Send-Q"], row.connections.map((item) =>
      [item.state, item.local, item.peer, item.receive_queue, item.send_queue]), "No MPTCP sockets observed"));
    if (row.endpoints?.length) live.append(details("Current namespace endpoint configuration", row.endpoints));
  }
  return node("div", "", live, await experiment("multipath", signal));
}

function title() {
  const selected = location.hash.slice(1);
  state.view = Object.hasOwn(pages, selected) ? selected : "overview";
  const [crumb, name, eyebrow, subtitle] = pages[state.view];
  $("crumb").textContent = crumb;
  $("page-title").textContent = name;
  $("eyebrow").textContent = eyebrow;
  $("page-subtitle").textContent = subtitle;
  document.querySelectorAll("[data-view]").forEach((entry) => {
    if (entry.dataset.view === state.view) entry.setAttribute("aria-current", "page");
    else entry.removeAttribute("aria-current");
  });
}
function setHealth(value) {
  const current = state.paused ? "paused" : value.status;
  $("live-status").dataset.state = current;
  $("live-status").lastElementChild.textContent = {
    ok: "Live", stale: "Stale", unavailable: "Source unavailable", paused: "Paused",
  }[current] ?? "Connecting";
  $("sample-meta").textContent = value.meta.captured_ms
    ? "Last sample " + when(value.meta.captured_ms) : "Awaiting flowcored";
  const notices = [];
  if (value.status === "stale") notices.push("Showing the last available sample. Live telemetry is stale.");
  if (value.status === "unavailable") notices.push("The monitoring source is unavailable. Recorded experiments remain accessible.");
  if (value.meta.history_error) notices.push("History storage is unavailable.");
  if (value.data?.quality?.inventory_available === false) notices.push("Interface inventory unavailable.");
  if (value.data?.quality?.truncated) notices.push("The daemon reported truncated telemetry.");
  if (value.data?.quality?.collector_errors?.length) notices.push("Partial telemetry · " + value.data.quality.collector_errors.join(", "));
  $("banner").hidden = !notices.length;
  $("banner").textContent = notices.join(" ");
  state.interval = Math.max(1, Math.min(60, value.meta.poll_seconds ?? 2));
}
function schedule() {
  clearTimeout(state.timer);
  if (!state.token || state.paused) return;
  state.timer = setTimeout(() => {
    // Do not replace controls under an active keyboard or pointer selection.
    if (root.contains(document.activeElement) || ["incidents", "multipath", "benchmarks"].includes(state.view)) {
      schedule();
      return;
    }
    refresh();
  }, state.interval * 1000);
}
async function refresh() {
  if (!state.token) return;
  title();
  clearTimeout(state.timer);
  state.abort?.abort();
  const controller = new AbortController();
  state.abort = controller;
  const signal = controller.signal;
  try {
    const health = await api("health", null, signal);
    if (signal.aborted) return;
    setHealth(health);
    const handler = {overview, slo, controller: controllerView, bottlenecks, incidents, multipath,
      benchmarks: (s) => experiment("benchmarks", s)}[state.view];
    const content = await handler(signal);
    if (!signal.aborted) root.replaceChildren(content);
  } catch (error) {
    if (!signal.aborted) root.replaceChildren(empty("View unavailable", error.message));
  } finally {
    if (state.abort === controller) schedule();
  }
}
const controllerView = controller;
function lock(message = "") {
  state.token = "";
  clearTimeout(state.timer);
  state.abort?.abort();
  root.replaceChildren();
  $("workspace").hidden = true;
  $("login").hidden = false;
  $("login-message").textContent = message;
  $("token").value = "";
  $("token").focus();
}
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = $("token").value;
  $("token").value = "";
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(token)) {
    $("login-message").textContent = "Enter a valid 32–128 character bearer token.";
    return;
  }
  state.token = token;
  try {
    const health = await api("health");
    if (!state.token) return;
    $("login").hidden = true;
    $("workspace").hidden = false;
    setHealth(health);
    await refresh();
  } catch (error) {
    lock(error.message);
  }
});
$("refresh").addEventListener("click", refresh);
$("pause").addEventListener("click", () => {
  state.paused = !state.paused;
  $("pause").textContent = state.paused ? "Resume" : "Pause";
  $("pause").setAttribute("aria-pressed", String(state.paused));
  refresh();
});
$("lock").addEventListener("click", () => lock());
window.addEventListener("hashchange", refresh);
