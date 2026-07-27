/* Adaptive Light - UI
   Vanilla JS against the JSON API. No framework, no build step: this is
   a handful of screens on a home server, and a Node build stage would
   cost more than it returns. */

const SECTIONS = ["sunrise", "day", "afternoon", "sunset", "night", "sleep"];

const $  = (sel, root = document) => root.querySelector(sel);
const el = (tag, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const k of kids.flat()) {
    if (k != null) n.append(k.nodeType ? k : document.createTextNode(k));
  }
  return n;
};

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw Object.assign(new Error("request failed"), { status: res.status, body });
  return body;
}

function toast(message, kind = "ok") {
  const t = el("div", { className: `toast ${kind}` }, message);
  document.body.append(t);
  setTimeout(() => t.remove(), 4200);
}

const ago = (iso) => {
  if (!iso) return "\u2014";
  const secs = (Date.now() - new Date(iso)) / 1000;
  if (secs < 60) return `${secs | 0}s ago`;
  if (secs < 3600) return `${(secs / 60) | 0}m ago`;
  if (secs < 86400) return `${(secs / 3600) | 0}h ago`;
  return `${(secs / 86400) | 0}d ago`;
};

const clock = (iso) => iso ? new Date(iso).toLocaleTimeString([], {
  hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "\u2014";

/* ------------------------------------------------------------------
   Status strip
   ------------------------------------------------------------------ */

let status = {};

async function refreshStatus() {
  try {
    status = await api("/api/status");
  } catch {
    status = { ha_connected: false, error: "container unreachable" };
  }

  const rooms = Object.values(status.rooms || {});
  const first = rooms[0];
  const dot = $("#conn-dot");

  dot.className = "dot " + (status.error ? "err" : status.ha_connected ? "ok" : "warn");
  $("#s-ha").textContent = status.ha_connected
    ? (status.ha_version || "connected")
    : (rooms.length ? "disconnected" : "idle \u2014 no rooms");
  $("#s-section").textContent = first?.section || "\u2014";
  $("#s-heartbeat").textContent = ago(first?.last_heartbeat);
  $("#s-count").textContent = first ? (first.heartbeats_today ?? 0) : "\u2014";
  // Climbing while nothing else happens is the proof the websocket
  // subscription is alive rather than silently dead.
  $("#s-events").textContent = status.events_seen ?? "\u2014";
}

/* ------------------------------------------------------------------
   Config
   ------------------------------------------------------------------ */

let cfg = null;        // the raw config document being edited
let entities = null;   // pickers, fetched from Home Assistant

const blankRoom = () => ({
  id: "", name: "", enabled: true,
  lux_sensors: [""], presence_sensors: [],
  groups: [{ id: "", name: "", entity_id: "" }],
  scenes: Object.fromEntries(SECTIONS.map(s => [s, { groups: {} }])),
});

function syncSceneGroups(room) {
  // Every group must have a mode in every scene, or the config will not
  // validate. Keeps the scene matrix in step with the group list.
  const ids = room.groups.map(g => g.id).filter(Boolean);
  for (const s of SECTIONS) {
    const scene = room.scenes[s] ||= { groups: {} };
    scene.groups ||= {};
    for (const id of ids) scene.groups[id] ||= { mode: "auto" };
    for (const id of Object.keys(scene.groups)) {
      if (!ids.includes(id)) delete scene.groups[id];
    }
  }
}

function entityPicker(list, value, onChange, placeholder) {
  const id = "dl-" + Math.random().toString(36).slice(2, 8);
  const dl = el("datalist", { id });
  for (const e of list || []) {
    dl.append(el("option", { value: e.entity_id, label: e.name }));
  }
  const input = el("input", {
    value: value || "", placeholder: placeholder || "entity id",
    setAttribute: undefined,
  });
  input.setAttribute("list", id);
  input.oninput = () => {
    const known = (list || []).some(e => e.entity_id === input.value);
    input.classList.toggle("bad", input.value !== "" && !known);
    onChange(input.value);
  };
  if (value) input.dispatchEvent(new Event("input"));
  return el("div", {}, input, dl);
}

function renderConfig() {
  const view = $("#view");
  view.replaceChildren();

  if (!cfg) { view.append(el("div", { className: "empty" }, "Loading\u2026")); return; }

  if (!entities) {
    view.append(el("div", { className: "banner warn" },
      "Not connected to Home Assistant, so entity pickers are unavailable. " +
      "Enter entity ids by hand, or check the container log."));
  }

  /* -- rooms ------------------------------------------------------ */
  for (const [index, room] of cfg.rooms.entries()) {
    syncSceneGroups(room);
    const card = el("div", { className: "card" });

    card.append(el("div", { className: "room-head" },
      (() => {
        const i = el("input", { className: "room-name", value: room.name,
                                placeholder: "Room name" });
        i.oninput = () => {
          room.name = i.value;
          // Derive the id only until it has been set: it is baked into
          // every generated entity name and historical row, so changing
          // it later orphans both.
          if (!room._idLocked) {
            room.id = i.value.toLowerCase().replace(/[^a-z0-9]+/g, "_")
                              .replace(/^_|_$/g, "").slice(0, 30);
            $(".room-id", card).value = room.id;
          }
        };
        return i;
      })(),
      (() => {
        const i = el("input", { className: "room-id mono", value: room.id,
                                placeholder: "room_id" });
        i.oninput = () => { room._idLocked = true; room.id = i.value; };
        return el("label", { style: "flex:0 1 190px" },
                  el("span", {}, "Identifier \u2014 permanent"), i);
      })(),
      el("button", { className: "btn danger sm right", textContent: "Remove room",
                     onclick: () => { cfg.rooms.splice(index, 1); renderConfig(); } })));

    /* sensors */
    card.append(el("div", { className: "row" },
      el("label", {}, el("span", {}, "Illuminance sensor"),
        entityPicker(entities?.illuminance, room.lux_sensors[0],
                     v => room.lux_sensors = v ? [v] : [],
                     "sensor.…_illuminance")),
      el("label", {}, el("span", {}, "Presence sensor \u2014 optional"),
        entityPicker(entities?.presence, room.presence_sensors[0],
                     v => room.presence_sensors = v ? [v] : [],
                     "binary_sensor.…"))));

    card.append(el("p", { className: "hint", style: "margin-top:6px" },
      "With no presence sensor the room counts as always occupied, so every " +
      "heartbeat is eligible for learning."));

    card.append(el("div", { className: "sep" }));

    /* groups */
    card.append(el("h2", {}, "Light groups"));
    card.append(el("p", { className: "hint" },
      "One row per controllable unit. To treat several bulbs as one, make a " +
      "light group in Home Assistant and give its entity here \u2014 Home " +
      "Assistant fans it out."));

    for (const [gi, group] of room.groups.entries()) {
      const nameInput = el("input", { value: group.name, placeholder: "Display name" });
      nameInput.oninput = () => {
        group.name = nameInput.value;
        if (!group._idLocked) {
          group.id = nameInput.value.toLowerCase().replace(/[^a-z0-9]+/g, "_")
                                    .replace(/^_|_$/g, "").slice(0, 30);
        }
      };
      card.append(el("div", { className: "group-row" },
        el("label", {}, el("span", {}, gi === 0 ? "Name" : ""), nameInput),
        el("label", {}, el("span", {}, gi === 0 ? "Entity" : ""),
          entityPicker(entities?.lights, group.entity_id,
                       v => group.entity_id = v, "light.…")),
        el("button", { className: "btn danger sm", textContent: "\u2715",
                       onclick: () => { room.groups.splice(gi, 1); renderConfig(); } })));
    }
    card.append(el("button", { className: "btn sm", textContent: "+ Add group",
      onclick: () => { room.groups.push({ id: "", name: "", entity_id: "" });
                       renderConfig(); } }));

    card.append(el("div", { className: "sep" }));

    /* scene matrix */
    card.append(el("h2", {}, "Scenes"));
    card.append(el("p", { className: "hint" },
      "Auto learns from what you do. Off holds the group off for that section " +
      "and overrides the adaptive system \u2014 the learner never decides this " +
      "for you. Maintenance is off by default for Night and Sleep, where lux " +
      "readings sit at the sensor's noise floor."));

    const head = el("tr", {}, el("th", {}, "Section"),
      ...room.groups.map(g => el("th", {}, g.name || g.id || "\u2014")),
      el("th", {}, "Maintain"), el("th", {}, "Margin"), el("th", {}, "Step %"));
    const body = el("tbody");

    for (const s of SECTIONS) {
      const scene = room.scenes[s];
      const tr = el("tr", {}, el("td", {}, s));

      for (const g of room.groups) {
        const cell = scene.groups[g.id] ||= { mode: "auto" };
        const mkBtn = (mode, label) => {
          const b = el("button", { textContent: label });
          b.className = cell.mode === mode
            ? "on" + (mode === "off" ? " off-state" : "") : "";
          b.onclick = () => { cell.mode = mode; renderConfig(); };
          return b;
        };
        tr.append(el("td", { className: "mode" },
          el("div", { className: "toggle" }, mkBtn("auto", "Auto"), mkBtn("off", "Off"))));
      }

      const maint = el("input", { type: "checkbox",
        checked: scene.maintenance_enabled ?? !["night", "sleep"].includes(s) });
      maint.onchange = () => scene.maintenance_enabled = maint.checked;

      const margin = el("input", { type: "number", step: "0.5", min: "0",
                                   value: scene.lux_margin ?? 5 });
      margin.oninput = () => scene.lux_margin = parseFloat(margin.value);

      const step = el("input", { type: "number", step: "1", min: "1", max: "100",
        value: Math.round((scene.max_step_pct ?? 0.04) * 100) });
      step.oninput = () => scene.max_step_pct = parseFloat(step.value) / 100;

      tr.append(el("td", { className: "mode" }, maint),
                el("td", {}, margin), el("td", {}, step));
      body.append(tr);
    }
    card.append(el("table", { className: "scene-grid" }, el("thead", {}, head), body));
    view.append(card);
  }

  if (!cfg.rooms.length) {
    view.append(el("div", { className: "empty" },
      "No rooms yet. Add one to start observing."));
  }

  /* -- schedule --------------------------------------------------- */
  const sched = el("div", { className: "card" });
  sched.append(el("h2", {}, "Sections"));
  sched.append(el("p", { className: "hint" },
    "Four boundaries follow the sun and two are fixed, so they can cross. " +
    "A fixed time states something about your routine that holds whatever " +
    "the sun does, so it wins \u2014 the loser is skipped for that day and " +
    "recorded with a reason."));
  sched.append(el("div", { id: "preview" }, el("span", { className: "faint" },
    "Loading today\u2019s boundaries\u2026")));
  view.append(sched);

  /* -- actions ---------------------------------------------------- */
  view.append(el("div", { className: "card" },
    el("div", { className: "row tight" },
      el("button", { className: "btn", textContent: "+ Add room",
        onclick: () => { cfg.rooms.push(blankRoom()); renderConfig(); } }),
      el("button", { className: "btn primary right", textContent: "Save configuration",
                     onclick: saveConfig }),
      el("button", { className: "btn", textContent: "Deploy to Home Assistant",
                     onclick: deploy })),
    el("p", { className: "hint", style: "margin:14px 0 0" },
      "Saving starts observation. Deploying creates the helpers and " +
      "automations \u2014 disable any previous system first, or two will " +
      "drive the same lights."),
    el("div", { id: "deploy-report" })));

  loadPreview();
}

async function loadPreview() {
  const box = $("#preview");
  if (!box) return;
  try {
    const p = await api("/api/schedule/preview");
    box.replaceChildren();

    const times = el("div", { className: "preview-times" });
    for (const b of p.boundaries) {
      times.append(el("div", { className: "slot" + (b.outcome === "ran" ? "" : " collapsed"),
                               title: b.reason || "" },
        el("div", { className: "n" }, b.name),
        el("div", { className: "t" }, b.at || "\u2014")));
    }
    box.append(el("p", { className: "hint", style: "margin:0 0 8px" },
      `Computed for ${p.date}`), times);

    const collisions = Object.entries(p.collisions || {});
    box.append(collisions.length
      ? el("div", { className: "banner warn", style: "margin-top:14px" },
          el("strong", {}, "Section collisions in the next year"),
          el("ul", {}, collisions.map(([s, n]) =>
            el("li", {}, `${s}: skipped on ${n} of ${p.days_scanned} days`))))
      : el("div", { className: "banner ok", style: "margin-top:14px" },
          `No collisions in the next ${p.days_scanned} days \u2014 all six ` +
          `sections run every day at your latitude.`));
  } catch (e) {
    box.replaceChildren(el("span", { className: "faint" },
      e.status === 409 ? "Connect to Home Assistant to preview section times."
                       : "Could not load the preview."));
  }
}

function cleanConfig() {
  // Strip UI bookkeeping before sending.
  const out = JSON.parse(JSON.stringify(cfg));
  for (const room of out.rooms) {
    delete room._idLocked;
    for (const g of room.groups) delete g._idLocked;
    room.lux_sensors = room.lux_sensors.filter(Boolean);
    room.presence_sensors = (room.presence_sensors || []).filter(Boolean);
  }
  return out;
}

async function saveConfig() {
  try {
    const res = await api("/api/config", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cleanConfig()),
    });
    toast(`Saved \u2014 ${res.rooms} room(s) active`);
    await refreshStatus();
    loadPreview();
  } catch (e) {
    const problems = e.body?.detail?.problems;
    const box = $("#deploy-report");
    box?.replaceChildren(el("div", { className: "banner err", style: "margin-top:14px" },
      el("strong", {}, "Configuration not saved"),
      el("ul", {}, (problems || [String(e.body?.detail || e)]).map(p => el("li", {}, p)))));
    toast("Not saved \u2014 see the problems listed", "err");
  }
}

async function deploy() {
  const box = $("#deploy-report");
  box.replaceChildren(el("p", { className: "faint" }, "Deploying\u2026"));
  try {
    const r = await api("/api/deploy", { method: "POST" });
    const line = (label, items) => items?.length
      ? el("li", {}, `${label}: `, el("span", { className: "mono" }, items.join(", ")))
      : null;
    box.replaceChildren(el("div",
      { className: "banner " + (r.ok ? "ok" : "err"), style: "margin-top:14px" },
      el("strong", {}, r.ok ? "Deployed" : "Deployment had problems"),
      el("ul", {},
        line("Helpers created", r.helpers_created),
        line("Helpers reused", r.helpers_reused),
        r.automations_written?.length
          ? el("li", {}, `Automations written: ${r.automations_written.length}`) : null,
        line("Automations removed", r.automations_removed),
        line("Missing entities", r.missing_entities),
        ...(r.problems || []).map(p => el("li", {}, p)))));
    toast(r.ok ? "Deployed to Home Assistant" : "Deployment had problems",
          r.ok ? "ok" : "err");
  } catch (e) {
    box.replaceChildren(el("div", { className: "banner err", style: "margin-top:14px" },
      String(e.body?.detail || "Deployment failed")));
    toast("Deployment failed", "err");
  }
}

async function loadConfig() {
  cfg = await api("/api/config");
  try { entities = await api("/api/entities"); } catch { entities = null; }
  renderConfig();
}

/* ------------------------------------------------------------------
   Log
   ------------------------------------------------------------------ */

let logFilters = { min_severity: "info", category: "", limit: 200 };

async function renderLog() {
  const view = $("#view");
  view.replaceChildren();

  const card = el("div", { className: "card" });
  card.append(el("h2", {}, "Event log"));
  card.append(el("p", { className: "hint" },
    "Everything the container did and when. Heartbeats are recorded at debug " +
    "severity \u2014 144 a day would bury everything else at info."));

  const sev = el("select", {}, ...["debug", "info", "warning", "error"].map(s =>
    el("option", { value: s, textContent: s, selected: s === logFilters.min_severity })));
  sev.onchange = () => { logFilters.min_severity = sev.value; renderLog(); };

  const cat = el("select", {},
    el("option", { value: "", textContent: "all categories" }),
    ...["scene_change", "scene_collapsed", "maintenance", "reactive", "hold",
        "heartbeat", "analysis", "almanac", "connection", "deploy", "config",
        "validation"].map(c =>
      el("option", { value: c, textContent: c, selected: c === logFilters.category })));
  cat.onchange = () => { logFilters.category = cat.value; renderLog(); };

  card.append(el("div", { className: "log-filters" },
    el("label", {}, el("span", {}, "Minimum severity"), sev),
    el("label", {}, el("span", {}, "Category"), cat),
    el("button", { className: "btn right", textContent: "Refresh",
                   onclick: renderLog })));

  const params = new URLSearchParams({ limit: logFilters.limit,
                                       min_severity: logFilters.min_severity });
  if (logFilters.category) params.set("category", logFilters.category);

  let events = [];
  try { ({ events } = await api("/api/events?" + params)); } catch { /* empty */ }

  if (!events.length) {
    card.append(el("div", { className: "empty" }, "Nothing logged at this level yet."));
  } else {
    const body = el("tbody");
    for (const e of events) {
      let detail = "";
      if (e.detail) {
        try {
          const d = JSON.parse(e.detail);
          detail = Object.entries(d)
            .map(([k, v]) => `${k}=${Array.isArray(v) ? v.length : v}`).join("  ");
        } catch { detail = e.detail.slice(0, 60); }
      }
      body.append(el("tr", { className: e.severity },
        el("td", { className: "when" }, clock(e.ts)),
        el("td", { className: "sev" },
          el("span", { className: "pill " + ({ error: "err", warning: "warn",
                                               info: "", debug: "" })[e.severity] },
             e.severity)),
        el("td", { className: "cat" }, e.category),
        el("td", { className: "msg" }, e.message),
        el("td", { className: "detail" }, detail)));
    }
    card.append(el("table", {}, el("thead", {}, el("tr", {},
      el("th", {}, "Time"), el("th", {}, "Severity"), el("th", {}, "Category"),
      el("th", {}, "Message"), el("th", {}, "Detail"))), body));
  }
  view.append(card);
}

/* ------------------------------------------------------------------
   Routing
   ------------------------------------------------------------------ */

const VIEWS = {
  config: loadConfig,
  log: renderLog,
  now: () => stub("Now", "Current section, targets against measured lux, and " +
                         "what each group is actually doing."),
  analysis: () => stub("Analysis", "Measured lux against target with the margin " +
                                   "band, per-group brightness, section bands and " +
                                   "reactive markers."),
  almanac: () => stub("Almanac", "The learned model: sections down, groups across, " +
                                 "with on-fraction and confidence."),
};

function stub(title, description) {
  $("#view").replaceChildren(el("div", { className: "card" },
    el("h2", {}, title, el("span", { className: "pill accent" }, "not built yet")),
    el("p", { className: "hint" }, description)));
}

function go(name) {
  for (const b of $("#tabs").children) b.classList.toggle("active", b.dataset.view === name);
  location.hash = name;
  (VIEWS[name] || VIEWS.config)();
}

$("#tabs").onclick = (e) => { if (e.target.dataset.view) go(e.target.dataset.view); };

refreshStatus();
setInterval(refreshStatus, 10000);
go(location.hash.slice(1) || "config");
