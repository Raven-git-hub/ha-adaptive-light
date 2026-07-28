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

// Collapse state persists across the frequent full re-renders of Config.
const collapsed = { profilesSection: false };  // keyed; profiles add their own

function collapsible(key, headContent, buildBody, opts = {}) {
  const isClosed = collapsed[key] ?? opts.defaultClosed ?? false;
  const head = el("div", { className: "collapse-head" + (isClosed ? " closed" : "") },
    el("span", { className: "chev" }, "\u25be"), ...[].concat(headContent));
  const body = el("div", { className: "collapse-body" + (isClosed ? " hidden" : "") });
  if (!isClosed) buildBody(body);
  head.addEventListener("click", (e) => {
    // Clicks on inputs/buttons inside the header must not toggle.
    if (e.target.closest("input, select, button")) return;
    const nowClosed = !head.classList.contains("closed");
    collapsed[key] = nowClosed;
    head.classList.toggle("closed", nowClosed);
    body.classList.toggle("hidden", nowClosed);
    if (!nowClosed && !body.childElementCount) buildBody(body);
  });
  return { head, body };
}

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
    if (!room.scenes[s]) room.scenes[s] = { groups: {} };
    const scene = room.scenes[s];
    if (!scene.groups) scene.groups = {};
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

  /* -- time profiles ---------------------------------------------- */
  ensureProfiles();
  const profCard = el("div", { className: "card" });
  const profSection = collapsible("profilesSection",
    el("h2", { style: "margin:0" }, "Time profiles",
       el("span", { className: "pill", style: "margin-left:8px" },
          `${cfg.schedule_profiles.length}`)),
    (body) => {
      body.append(el("p", { className: "hint" },
        "A profile is a set of section times. Rooms choose a profile, so several " +
        "rooms can share one schedule and a single edit updates them all. Fixed " +
        "times beat sun-relative ones when they collide; the loser is skipped that " +
        "day. Computed times for today are shown on the right as you edit."));
      renderProfiles(body);
    });
  profCard.append(profSection.head, profSection.body);
  view.append(profCard);

  /* -- rooms ------------------------------------------------------ */
  for (const [index, room] of cfg.rooms.entries()) {
    syncSceneGroups(room);
    const card = el("div", { className: "card" });

    // Everything is appended to `card` as before; afterwards the head is
    // kept visible and the remaining children fold into a collapsible.
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
      (() => {
        const sel = el("select", {}, ...cfg.schedule_profiles.map(p =>
          el("option", { value: p.id, textContent: p.name,
                         selected: (room.schedule_profile || "default") === p.id })));
        sel.onchange = () => { room.schedule_profile = sel.value; };
        return el("label", { style: "flex:0 1 200px" },
                  el("span", {}, "Time profile"), sel);
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
        if (!scene.groups[g.id]) scene.groups[g.id] = { mode: "auto" };
        const cell = scene.groups[g.id];
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

    // Presence Rules - reserved for future per-room occupancy logic.
    card.append(el("div", { className: "sep" }));
    const presence = collapsible(`presence:${room.id}`,
      el("h2", { style: "margin:0" }, "Presence rules",
         el("span", { className: "pill accent", style: "margin-left:8px" }, "coming soon")),
      (pb) => pb.append(el("p", { className: "hint" },
        "How occupancy should affect this room \u2014 for example dimming when " +
        "empty, or holding a scene while someone is present. Not yet implemented; " +
        "this space is reserved.")),
      { defaultClosed: true });
    card.append(presence.head, presence.body);

    // Fold the whole room: keep the head visible, collapse the rest.
    const roomHead = card.querySelector(".room-head");
    const rc = collapsible(`room:${room.id}`,
      roomHead,
      (rb) => {
        // Move every card child except the head into the collapsible body.
        const kids = [...card.childNodes].filter(n => n !== roomHead);
        for (const k of kids) rb.append(k);
      },
      { defaultClosed: index !== 0 });
    const shell = el("div", { className: "card" });
    shell.append(rc.head, rc.body);
    view.append(shell);
  }

  if (!cfg.rooms.length) {
    view.append(el("div", { className: "empty" },
      "No rooms yet. Add one to start observing."));
  }

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
   Time profiles
   ------------------------------------------------------------------ */

const SUN_EVENTS = ["sunrise", "sunset"];

function ensureProfiles() {
  // Older configs may still round-trip a bare `schedule`; the loader
  // migrates on save, but the editor always works from the array.
  if (!Array.isArray(cfg.schedule_profiles) || !cfg.schedule_profiles.length) {
    cfg.schedule_profiles = [{
      id: "default", name: "Default",
      sections: SECTIONS.map(id => ({
        id, name: id[0].toUpperCase() + id.slice(1),
        trigger: { type: "clock", time: "12:00" },
      })),
      collision_policy: "collapse", min_section_minutes: 30,
    }];
  }
}

// -- one trigger editor; onChange fires a live re-preview ------------
function triggerEditor(trigger, onChange) {
  const wrap = el("div", { className: "trigger-edit" });

  const kind = el("select", {},
    el("option", { value: "clock", textContent: "Fixed time",
                   selected: trigger.type === "clock" }),
    el("option", { value: "sun", textContent: "Sun offset",
                   selected: trigger.type === "sun" }),
    el("option", { value: "earliest", textContent: "Earliest of",
                   selected: trigger.type === "earliest" }),
    el("option", { value: "latest", textContent: "Latest of",
                   selected: trigger.type === "latest" }));

  const body = el("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap" });

  const rebuildBody = () => {
    body.replaceChildren();
    if (trigger.type === "clock") {
      const t = el("input", { className: "time", type: "time",
                              value: trigger.time || "12:00" });
      t.oninput = () => { trigger.time = t.value; onChange(); };
      body.append(t);
    } else if (trigger.type === "sun") {
      const ev = el("select", {}, ...SUN_EVENTS.map(e =>
        el("option", { value: e, textContent: e, selected: trigger.event === e })));
      ev.onchange = () => { trigger.event = ev.value; onChange(); };
      const off = el("input", { className: "offset", type: "number", step: "5",
                                value: trigger.offset_minutes || 0 });
      off.oninput = () => { trigger.offset_minutes = parseInt(off.value) || 0; onChange(); };
      body.append(ev, off, el("span", { className: "unit" }, "min offset"));
    } else {
      // composite: earliest/latest of N sub-triggers (clock or sun)
      trigger.of ||= [{ type: "clock", time: "05:30" },
                      { type: "sun", event: "sunrise", offset_minutes: 0 }];
      const box = el("div", { className: "composite" });
      trigger.of.forEach((sub, i) => {
        const subRow = el("div", { className: "sub" });
        subRow.append(triggerEditor(sub, onChange));
        if (trigger.of.length > 2) {
          subRow.append(el("button", { className: "btn danger sm", textContent: "\u2715",
            onclick: () => { trigger.of.splice(i, 1); onChange(); renderConfig(); } }));
        }
        box.append(subRow);
      });
      box.append(el("button", { className: "btn sm addsub", textContent: "+ add condition",
        onclick: () => { trigger.of.push({ type: "clock", time: "12:00" });
                         onChange(); renderConfig(); } }));
      body.append(box);
    }
  };

  kind.onchange = () => {
    trigger.type = kind.value;
    // clear fields that don't belong to the new type
    if (trigger.type === "clock") { trigger.time ||= "12:00";
      delete trigger.event; delete trigger.offset_minutes; delete trigger.of; }
    else if (trigger.type === "sun") { trigger.event ||= "sunrise";
      trigger.offset_minutes ||= 0; delete trigger.time; delete trigger.of; }
    else { delete trigger.time; delete trigger.event; delete trigger.offset_minutes; }
    rebuildBody(); onChange();
  };

  rebuildBody();
  wrap.append(kind, body);
  return wrap;
}

function renderProfiles(container) {
  ensureProfiles();
  for (const [pi, profile] of cfg.schedule_profiles.entries()) {
    const inUse = cfg.rooms.filter(r => (r.schedule_profile || "default") === profile.id);
    const card = el("div", { className: "card profile-card" });

    const nameInput = el("input", { className: "pname", value: profile.name });
    nameInput.oninput = () => { profile.name = nameInput.value; };

    const key = `profile:${profile.id}`;
    const pc = collapsible(key,
      el("div", { className: "profile-head", style: "flex:1" },
        nameInput,
        el("span", { className: "pid" }, profile.id),
        el("span", { className: "pill" },
           `${inUse.length} room${inUse.length === 1 ? "" : "s"}`),
        profile.id === "default"
          ? el("span", { className: "pill accent right" }, "always present")
          : el("button", { className: "btn danger sm right", textContent: "Delete profile",
              onclick: () => deleteProfile(profile.id) })),
      (body) => {
        const editor = el("div", { className: "section-editor" });
        const byId = Object.fromEntries(profile.sections.map(s => [s.id, s]));
        for (const sid of SECTIONS) {
          const sec = byId[sid];
          const row = el("div", { className: "srow" });
          row.append(el("div", { className: "sname" }, sec.name || sid));
          const preview = el("div", { className: "computed",
            id: `pv-${profile.id}-${sid}` }, "\u2026");
          row.append(el("div", {}),
            triggerEditor(sec.trigger, () => previewProfile(profile)),
            preview);
          editor.append(row);
        }
        body.append(editor);
        body.append(el("div", { className: "profile-preview",
          id: `pvbanner-${profile.id}` }));
        previewProfile(profile);
      },
      { defaultClosed: profile.id !== "default" });
    // The collapsible already renders a chevron; the profile-head has its
    // own, so drop the duplicate wrapper chevron by using pc.head content.
    card.append(pc.head, pc.body);
    container.append(card);
  }

  container.append(el("button", { className: "btn", textContent: "+ New time profile",
    onclick: addProfile }));
}

async function previewProfile(profile) {
  try {
    const p = await api("/api/schedule/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profile),
    });
    for (const b of p.boundaries) {
      const cell = document.getElementById(`pv-${profile.id}-${b.section}`);
      if (cell) {
        cell.textContent = b.at || "\u2014";
        cell.classList.toggle("collapsed", b.outcome !== "ran");
        cell.title = b.reason || "";
      }
    }
    const banner = document.getElementById(`pvbanner-${profile.id}`);
    if (banner) {
      const collisions = Object.entries(p.collisions || {});
      banner.replaceChildren(collisions.length
        ? el("div", { className: "banner warn" },
            el("strong", {}, "Collisions in the next year"),
            el("ul", {}, collisions.map(([s, n]) =>
              el("li", {}, `${s}: skipped on ${n} of ${p.days_scanned} days`))))
        : el("div", { className: "banner ok" },
            `No collisions in the next ${p.days_scanned} days.`));
    }
  } catch (e) {
    const banner = document.getElementById(`pvbanner-${profile.id}`);
    if (banner) banner.replaceChildren(el("span", { className: "faint" },
      e.status === 409 ? "Connect to Home Assistant to preview times." : "Preview unavailable."));
  }
}

function addProfile() {
  const n = cfg.schedule_profiles.length;
  const base = JSON.parse(JSON.stringify(cfg.schedule_profiles.find(p => p.id === "default")));
  base.id = `profile_${n}`;
  base.name = `Profile ${n}`;
  cfg.schedule_profiles.push(base);
  renderConfig();
}

function deleteProfile(id) {
  const inUse = cfg.rooms.filter(r => (r.schedule_profile || "default") === id);
  if (inUse.length) {
    toast(`Can't delete: ${inUse.length} room(s) use this profile`, "err");
    return;
  }
  cfg.schedule_profiles = cfg.schedule_profiles.filter(p => p.id !== id);
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
   Now
   ------------------------------------------------------------------ */

let nowTimer = null;

async function renderNow() {
  const view = $("#view");
  if (nowTimer) { clearInterval(nowTimer); nowTimer = null; }

  const rooms = Object.keys(status.rooms || {});
  if (!rooms.length) {
    view.replaceChildren(el("div", { className: "empty" },
      "No rooms configured yet. Add one on the Config tab."));
    return;
  }

  const paint = async () => {
    // Leaving the Now view cancels the timer; guard against a late paint.
    if (!location.hash.startsWith("#now") && location.hash !== "") return;
    let cards;
    try {
      cards = await Promise.all(rooms.map(id =>
        api("/api/now/" + id).catch(() => null)));
    } catch { return; }
    view.replaceChildren(...cards.filter(Boolean).map(nowCard));
    if (!cards.some(Boolean)) {
      view.replaceChildren(el("div", { className: "empty" },
        "Not connected to Home Assistant."));
    }
  };
  await paint();
  nowTimer = setInterval(paint, 10000);
}

function nowCard(n) {
  const card = el("div", { className: "card now-room" });

  const modePill = n.almanac_mode
    ? el("span", { className: "pill accent" }, n.almanac_mode)
    : el("span", { className: "pill" }, "no almanac");
  card.append(el("h2", {}, n.name,
    el("span", { className: "sub" },
      n.section ? ` \u00b7 ${n.section}` : " \u00b7 waiting for first crossover"),
    modePill,
    n.hold ? el("span", { className: "pill warn" }, "maintenance held") : null,
    n.guard ? el("span", { className: "pill" }, "adjusting\u2026") : null,
    el("span", { className: "pill " + (n.occupied ? "ok" : ""),
                 style: "margin-left:auto" }, n.occupied ? "occupied" : "empty")));

  /* lux gauge */
  const g = el("div", { className: "lux-gauge" });
  const luxTxt = n.lux == null ? "\u2014" : n.lux.toFixed(1);
  g.append(el("div", { className: "readout" },
    el("span", { className: "big" }, luxTxt),
    el("span", { className: "unit" }, "lux measured"),
    n.lux_target != null
      ? el("span", { className: "vs" }, `target ${n.lux_target} \u00b1${n.lux_margin}`)
      : el("span", { className: "vs" }, "no target yet"),
    n.maintenance_enabled === false
      ? el("span", { className: "pill", style: "margin-left:auto" },
           "maintenance off this section")
      : null));

  if (n.lux != null && n.lux_target != null) {
    // Scale to whichever is larger: the reading or twice the target,
    // so the needle and band both stay on-screen through the day.
    const scaleMax = Math.max(n.lux * 1.15, (n.lux_target + n.lux_margin) * 1.6, 10);
    const pct = (v) => Math.max(0, Math.min(100, (v / scaleMax) * 100));
    const band = el("div", { className: "band" });
    const lo = pct(Math.max(0, n.lux_target - n.lux_margin));
    const hi = pct(n.lux_target + n.lux_margin);
    band.append(
      el("div", { className: "target-zone",
                  style: `left:${lo}%;width:${hi - lo}%` }),
      el("div", { className: "target-line", style: `left:${pct(n.lux_target)}%` }),
      el("div", { className: "needle" + (n.in_band ? "" : " out"),
                  style: `left:${pct(n.lux)}%` }),
      el("div", { className: "scale" },
        el("span", {}, "0"), el("span", {}, scaleMax.toFixed(0))));
    g.append(band);
  }
  card.append(g);

  /* groups */
  for (const grp of n.groups) {
    const row = el("div", { className: "grp-row" + (grp.on ? "" : " off") });
    const meter = el("div", { className: "bmeter" });
    if (grp.on && grp.brightness != null) {
      meter.append(el("div", { className: "fill",
        style: `width:${(grp.brightness / 255 * 100).toFixed(0)}%` }));
    }
    if (grp.target != null && grp.target > 0) {
      meter.append(el("div", { className: "tmark",
        style: `left:${(grp.target / 255 * 100).toFixed(0)}%` }));
    }
    const state = grp.mode === "off"
      ? "forced off"
      : grp.on ? `${grp.brightness ?? "on"}` : "off";
    const target = grp.mode === "off" ? "\u2014"
      : grp.target == null ? "unlearned"
      : grp.target === 0 ? "off" : `\u2192 ${grp.target}`;
    row.append(
      el("div", { className: "gname" }, grp.name,
        grp.mode === "off" ? el("span", { className: "pill",
                                          style: "margin-left:8px" }, "off") : null),
      el("div", {}, grp.on
        ? el("span", { className: "pill ok" }, "on")
        : el("span", { className: "pill" }, "off")),
      meter,
      el("div", { className: "bval" }, `${state}  ${target}`));
    card.append(row);
  }

  /* section timeline + countdown */
  card.append(el("div", { className: "sep" }));
  const tl = el("div", { className: "timeline" });
  for (const s of SECTIONS) {
    tl.append(el("div",
      { className: "seg" + (s === n.section ? " current" : "") }, s));
  }
  card.append(tl);

  if (n.next_at) {
    card.append(el("div", { className: "countdown" },
      "Next: ", el("b", {}, n.next_section), " at ",
      el("b", {}, new Date(n.next_at).toLocaleTimeString(
        [], { hour: "2-digit", minute: "2-digit" })),
      " \u00b7 ", countdownText(n.next_at)));
  }
  return card;
}

function countdownText(iso) {
  const mins = Math.max(0, (new Date(iso) - Date.now()) / 60000);
  if (mins < 60) return `in ${mins | 0} min`;
  const h = Math.floor(mins / 60), m = (mins % 60) | 0;
  return `in ${h}h ${m}m`;
}

/* ------------------------------------------------------------------
   Analysis
   ------------------------------------------------------------------ */

let analysisDate = null;
let analysisChart = null;

// One stable colour per group, assigned by position.
const GROUP_COLORS = ["#f0b429", "#4f93d6", "#5fc97a", "#c77dd6", "#d68a4f", "#68c9c1"];

async function renderAnalysis() {
  const view = $("#view");
  const rooms = Object.keys(status.rooms || {});
  if (!rooms.length) {
    view.replaceChildren(el("div", { className: "empty" },
      "No rooms configured yet."));
    return;
  }
  const roomId = rooms[0];

  let data;
  try {
    const q = analysisDate ? `?date=${analysisDate}` : "";
    data = await api(`/api/analysis/${roomId}${q}`);
  } catch (e) {
    view.replaceChildren(el("div", { className: "empty" },
      e.status === 409 ? "No configuration." : "Could not load analysis data."));
    return;
  }
  analysisDate = data.date;

  view.replaceChildren();
  const card = el("div", { className: "card" });

  const dates = data.available_dates.length ? data.available_dates : [data.date];
  const idx = dates.indexOf(data.date);
  const step = (delta) => {
    const target = dates[idx + delta];
    if (target) { analysisDate = target; renderAnalysis(); }
  };

  card.append(el("div", { className: "analysis-head" },
    el("h2", { style: "margin:0" }, data.name),
    el("button", { className: "btn sm", textContent: "\u2039 Earlier",
                   disabled: idx >= dates.length - 1, onclick: () => step(1) }),
    el("span", { className: "date" }, data.date),
    el("button", { className: "btn sm", textContent: "Later \u203a",
                   disabled: idx <= 0, onclick: () => step(-1) }),
    el("span", { className: "muted", style: "margin-left:auto" },
      `${data.heartbeats} heartbeats`)));

  if (!data.heartbeats) {
    card.append(el("div", { className: "empty" },
      "No observations recorded on this day."));
    view.append(card);
    return;
  }

  const host = el("div", { className: "chart-wrap" });
  card.append(host);

  card.append(el("div", { className: "chart-note" },
    el("span", {}, el("span", { className: "swatch",
      style: "background:#e6e9f0" }), "Measured lux"),
    el("span", {}, el("span", { className: "swatch band" }), "Target band"),
    el("span", {}, el("span", { className: "swatch react" }), "You intervened"),
    el("span", { className: "faint" }, "\u2014 brightness on the right axis, 0\u2013255")));

  // Reactive events, listed under the chart for detail.
  if (data.reactives.length) {
    const list = el("div", { className: "reactive-list" });
    list.append(el("h2", { style: "font-size:13px;margin:18px 0 6px" },
      "Interventions"));
    for (const r of data.reactives) {
      list.append(el("div", { className: "rx" },
        el("span", { className: "t" },
          new Date(r.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })),
        el("span", { className: "pill warn" }, r.section),
        el("span", { className: "muted" },
          r.lux_before != null && r.lux_after != null
            ? `lux ${r.lux_before} \u2192 ${r.lux_after}` : ""),
        r.suspended ? el("span", { className: "pill" }, "maintenance paused") : null));
    }
    card.append(list);
  }

  view.append(card);
  drawChart(host, data);
}

function drawChart(host, data) {
  if (analysisChart) { analysisChart.destroy(); analysisChart = null; }

  // uPlot series: [time, lux, ...group brightness]. Brightness shares a
  // second y-scale so a 0-255 line and a 0-50 lux line coexist.
  const series = [
    {},
    { label: "lux", scale: "lux", stroke: "#e6e9f0", width: 2,
      points: { show: false }, value: (u, v) => v == null ? "\u2014" : v.toFixed(1) },
  ];
  const dataArr = [data.times, data.lux];

  data.group_ids.forEach((g, i) => {
    series.push({
      label: data.group_names[g] || g, scale: "bri",
      stroke: GROUP_COLORS[i % GROUP_COLORS.length], width: 1.5,
      points: { show: false },
      value: (u, v) => v == null ? "\u2014" : String(v),
    });
    dataArr.push(data.brightness[g]);
  });

  // Draw target bands and reactive lines behind the series.
  const bandRects = data.sections.filter(s => s.lux_target != null && s.start);
  const drawHook = (u) => {
    const ctx = u.ctx;
    const left = u.bbox.left, right = u.bbox.left + u.bbox.width;
    ctx.save();
    // section target bands (lux scale). Clamped to the plot area: a
    // section's recorded start can predate the day's first plotted
    // heartbeat (e.g. after a restart mid-day), and without clamping
    // that pushes the band off-canvas to the left of the axis.
    for (const s of bandRects) {
      const x0 = Math.max(left, u.valToPos(s.start, "x", true));
      const x1 = Math.min(right,
        u.valToPos(s.end || data.times[data.times.length - 1], "x", true));
      if (x1 <= x0) continue;
      const yHi = u.valToPos(s.lux_target + (s.lux_margin || 5), "lux", true);
      const yLo = u.valToPos(Math.max(0, s.lux_target - (s.lux_margin || 5)), "lux", true);
      ctx.fillStyle = "rgba(240,180,41,0.10)";
      ctx.fillRect(x0, yHi, x1 - x0, yLo - yHi);
      ctx.strokeStyle = "rgba(240,180,41,0.35)";
      ctx.setLineDash([4, 4]);
      const yT = u.valToPos(s.lux_target, "lux", true);
      ctx.beginPath(); ctx.moveTo(x0, yT); ctx.lineTo(x1, yT); ctx.stroke();
      ctx.setLineDash([]);
    }
    // reactive markers
    ctx.strokeStyle = "rgba(210,153,34,0.8)";
    ctx.lineWidth = 2;
    for (const r of data.reactives) {
      const x = u.valToPos(r.t, "x", true);
      if (x < left || x > right) continue;
      ctx.beginPath();
      ctx.moveTo(x, u.bbox.top); ctx.lineTo(x, u.bbox.top + u.bbox.height);
      ctx.stroke();
    }
    ctx.restore();
  };

  const opts = {
    width: host.clientWidth || 960,
    height: 420,
    scales: { x: { time: true }, lux: {}, bri: { range: [0, 255] } },
    axes: [
      { stroke: "#5a6274", grid: { stroke: "#272c3855" },
        ticks: { stroke: "#272c38" } },
      { scale: "lux", stroke: "#8b93a7", label: "lux",
        grid: { stroke: "#272c3844" } },
      { scale: "bri", side: 1, stroke: "#8b93a7", label: "brightness",
        grid: { show: false } },
    ],
    series,
    hooks: { draw: [drawHook] },
    legend: { live: true },
    cursor: { drag: { x: true, y: false } },
  };

  analysisChart = new uPlot(opts, dataArr, host);

  // Redraw on resize so it stays full-width.
  if (!drawChart._resize) {
    drawChart._resize = () => {
      if (analysisChart && host.clientWidth) {
        analysisChart.setSize({ width: host.clientWidth, height: 420 });
      }
    };
    window.addEventListener("resize", drawChart._resize);
  }
}


/* ------------------------------------------------------------------
   Almanac
   ------------------------------------------------------------------ */

let almanacCharts = [];

function destroyAlmanacCharts() {
  for (const c of almanacCharts) { try { c.destroy(); } catch {} }
  almanacCharts = [];
}

async function renderAlmanac() {
  destroyAlmanacCharts();
  const view = $("#view");
  const rooms = Object.keys(status.rooms || {});
  if (!rooms.length) {
    view.replaceChildren(el("div", { className: "empty" }, "No rooms configured yet."));
    return;
  }
  view.replaceChildren();

  for (const roomId of rooms) {
    let almanac, history;
    try {
      [almanac, history] = await Promise.all([
        api("/api/almanac/" + roomId),
        api(`/api/almanac/${roomId}/history`).catch(() => null),
      ]);
    } catch {
      continue;
    }
    view.append(almanacCard(roomId, status.rooms[roomId]?.name || roomId,
                            almanac, history));
  }
}

function almanacCard(roomId, name, almanac, history) {
  const meta = almanac?._meta || {};
  const card = el("div", { className: "card almanac-room" });

  card.append(el("div", { className: "almanac-meta" },
    el("h2", { style: "margin:0" }, name),
    el("span", { className: `badge ${meta.mode || "provisional"}` },
       meta.mode || "no data"),
    el("span", { className: "muted" },
       meta.days_analysed != null ? `${meta.days_analysed} day(s) analysed` : ""),
    meta.valid_from ? el("span", { className: "faint" }, `valid from ${meta.valid_from}`) : null,
    el("button", { className: "btn sm right", textContent: "Rebuild now",
                   onclick: () => rebuildAlmanac(roomId) })));

  // sections present in the almanac (skip _meta)
  const sections = SECTIONS.filter(s => almanac && almanac[s] && typeof almanac[s] === "object");
  if (!sections.length) {
    card.append(el("div", { className: "empty" },
      "No almanac yet. It builds from the nightly analysis at 00:15 once there " +
      "are occupied heartbeats to learn from."));
    return card;
  }

  // group ids from the first section entry
  const groupIds = Object.keys(almanac[sections[0]].on_fraction || {});
  const split = el("div", { className: "almanac-split" });

  // -- left: matrix --------------------------------------------------
  const left = el("div", {});
  const head = el("tr", {}, el("th", {}, "Section"),
    ...groupIds.map(g => el("th", {}, cfg?.rooms?.find(r => r.id === roomId)
      ?.groups?.find(gr => gr.id === g)?.name || g)));
  const body = el("tbody");

  for (const sec of sections) {
    const e = almanac[sec];
    const conf = e.high_confidence_days > 2 ? "high"
               : (e.days_contributing > 1 ? "medium" : "low");
    const tr = el("tr", {},
      el("td", { className: "sechead" },
        el("div", {}, sec),
        el("div", { className: "lux" },
           e.lux_target != null ? `${e.lux_target} \u00b1${e.lux_margin}` : "\u2014"),
        el("div", { className: "sub" },
           `${e.days_contributing}d` +
           (e.maintenance_enabled ? "" : " \u00b7 maint off"))));

    for (const g of groupIds) {
      const val = e[g];
      const frac = (e.on_fraction || {})[g];
      const cell = el("td", { className: `cell conf-${conf}` });
      const briClass = val === 0 ? "bri off" : val == null ? "bri none" : "bri";
      cell.append(el("div", { className: briClass },
        val === 0 ? "off" : val == null ? "\u2014" : String(val)));
      if (frac != null) {
        cell.append(el("div", { className: "onfrac",
          title: `on ${Math.round(frac * 100)}% of samples` },
          el("div", { className: "fill", style: `width:${Math.round(frac * 100)}%` })));
      }
      // auto/off toggle, writing straight into cfg so Save persists it
      const roomCfg = cfg?.rooms?.find(r => r.id === roomId);
      if (roomCfg) {
        const sceneG = roomCfg.scenes?.[sec]?.groups?.[g];
        if (sceneG) {
          const tg = el("div", { className: "toggle" });
          const mk = (mode, label) => {
            const b = el("button", { textContent: label });
            b.className = sceneG.mode === mode
              ? "on" + (mode === "off" ? " off-state" : "") : "";
            b.onclick = async () => {
              sceneG.mode = mode;
              await saveConfig();
              renderAlmanac();
            };
            return b;
          };
          tg.append(mk("auto", "Auto"), mk("off", "Off"));
          cell.append(tg);
        }
      }
      tr.append(cell);
    }
    body.append(tr);
  }
  left.append(el("table", { className: "almanac" }, el("thead", {}, head), body));
  split.append(left);

  // -- right: trend charts ------------------------------------------
  const right = el("div", {});
  if (!history || !history.snapshots) {
    right.append(el("div", { className: "trend-empty" },
      "Trends appear once a few nightly analyses have accumulated. " +
      "Right now there is a single snapshot \u2014 nothing to trend yet."));
  } else {
    right.append(el("p", { className: "hint", style: "margin:0 0 8px" },
      `Target and trust over the last ${history.snapshots} snapshot(s). ` +
      `Trust rises with recent, confident, consistent data; the dashed lines ` +
      `are the medium and high thresholds.`));
    for (const sec of sections) {
      const sd = history.sections[sec];
      const holder = el("div", { className: "trend-section" });
      holder.append(el("div", { className: "thead" },
        el("span", { className: "nm" }, sec),
        el("span", { className: "now" },
           sd ? `${sd.trust_weight.at(-1)} trust` : "")));
      const chart = el("div", { className: "trend-chart" });
      holder.append(chart);
      right.append(holder);
      if (sd && sd.generated_at.length > 1) {
        queueMicrotask(() => drawTrend(chart, sd, history.thresholds));
      } else {
        chart.replaceChildren(el("div", { className: "trend-empty" },
          "one point so far"));
      }
    }
  }
  split.append(right);

  card.append(split);
  return card;
}

function drawTrend(host, sd, thresholds) {
  const xs = sd.generated_at.map(t => Math.floor(new Date(t).getTime() / 1000));
  const opts = {
    width: host.clientWidth || 420, height: 90,
    scales: { x: { time: true }, lux: {}, trust: {} },
    axes: [
      { stroke: "#5a6274", size: 24, grid: { stroke: "#272c3833" } },
      { scale: "lux", stroke: "#8b93a7", size: 34, grid: { stroke: "#272c3822" } },
      { scale: "trust", side: 1, stroke: "#f0b429", size: 30, grid: { show: false } },
    ],
    series: [
      {},
      { label: "lux", scale: "lux", stroke: "#e6e9f0", width: 2, points: { show: true, size: 4 } },
      { label: "trust", scale: "trust", stroke: "#f0b429", width: 1.5,
        points: { show: false }, dash: [] },
    ],
    hooks: { draw: [(u) => {
      // threshold reference lines on the trust scale
      const ctx = u.ctx; ctx.save();
      ctx.setLineDash([3, 3]); ctx.strokeStyle = "rgba(240,180,41,0.3)";
      for (const key of ["medium", "high"]) {
        const y = u.valToPos(thresholds[key], "trust", true);
        ctx.beginPath(); ctx.moveTo(u.bbox.left, y);
        ctx.lineTo(u.bbox.left + u.bbox.width, y); ctx.stroke();
      }
      ctx.restore();
    }] },
    legend: { show: false },
    cursor: { show: true },
  };
  const data = [xs, sd.lux_target, sd.trust_weight];
  almanacCharts.push(new uPlot(opts, data, host));
}

async function rebuildAlmanac(roomId) {
  try {
    await api("/api/analysis/run", { method: "POST" });
    toast("Almanac rebuilt");
    renderAlmanac();
  } catch (e) {
    toast(e.status === 409 ? "Not connected to Home Assistant" : "Rebuild failed", "err");
  }
}

/* ------------------------------------------------------------------
   Routing
   ------------------------------------------------------------------ */

const VIEWS = {
  config: loadConfig,
  log: renderLog,
  now: renderNow,
  analysis: renderAnalysis,
  almanac: renderAlmanac,
};

function stub(title, description) {
  $("#view").replaceChildren(el("div", { className: "card" },
    el("h2", {}, title, el("span", { className: "pill accent" }, "not built yet")),
    el("p", { className: "hint" }, description)));
}

function go(name) {
  if (nowTimer && name !== "now") { clearInterval(nowTimer); nowTimer = null; }
  if (analysisChart && name !== "analysis") { analysisChart.destroy(); analysisChart = null; }
  if (name !== "almanac") destroyAlmanacCharts();
  for (const b of $("#tabs").children) b.classList.toggle("active", b.dataset.view === name);
  location.hash = name;
  (VIEWS[name] || VIEWS.config)();
}

$("#tabs").onclick = (e) => { if (e.target.dataset.view) go(e.target.dataset.view); };

refreshStatus();
setInterval(refreshStatus, 10000);
go(location.hash.slice(1) || "config");
