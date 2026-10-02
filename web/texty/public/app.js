import {
  seed,
  id,
  validateVolunteer,
  previewDecision,
  applyDemo,
  phoneValid,
  processDemoSignup,
} from "./domain.js";
const app = document.querySelector("#app"),
  modal = document.querySelector("#modal");
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const icons = {
  home: '<path d="M3 10 12 3l9 7v10H3z"/><path d="M9 20v-7h6v7"/>',
  people:
    '<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6M18 15a5 5 0 0 1 3 5"/>',
  calendar:
    '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v5M17 3v5M3 11h18M7 15h3M14 15h3"/>',
  chat: '<path d="M21 11a9 9 0 0 1-9 9H3l2-5a9 9 0 1 1 16-4z"/><path d="M8 10h8M8 14h5"/>',
  settings:
    '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  heart:
    '<path d="M20 4c-3-3-7 0-8 2-1-2-5-5-8-2-5 5 3 12 8 16 5-4 13-11 8-16z"/>',
  arrow: '<path d="m9 5 7 7-7 7"/>',
  logo: '<path d="M5 5v8c0 5 4 8 7 8 4 0 7-3 7-8V5"/><path d="M5 9h14M12 3v13"/>',
};
const icon = (n) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[n] || icons.chat}</svg>`;
const initials = (v) => esc(v.first_name[0] + v.last_name[0]);
let config = {
    connected: false,
    name: "Texty",
    provider: "gloo",
    aiReady: false,
    liveSms: false,
  },
  token = null,
  mode = "demo",
  page = "overview",
  state,
  filter = "",
  ministry = "all",
  simPhone = "+12025550100",
  trace = "",
  authView = "login";
const storeKey = "texty.synthetic.v1";
const sessionKey = "texty.coordinator.session.v1";
const rememberSession = (value) => {
  token = value;
  try {
    if (value) sessionStorage.setItem(sessionKey, value);
    else sessionStorage.removeItem(sessionKey);
  } catch {
    // Restricted storage still permits an in-memory sign-in.
  }
};
const savedSession = () => {
  try {
    return sessionStorage.getItem(sessionKey) || null;
  } catch {
    return null;
  }
};
try {
  state = JSON.parse(localStorage.getItem(storeKey)) || seed();
} catch {
  state = seed();
}
const persist = () => {
  if (mode === "demo") localStorage.setItem(storeKey, JSON.stringify(state));
};
const toast = (s) => {
  const el = document.querySelector("#toast");
  el.textContent = s;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 4500);
};
async function api(path, body) {
  const r = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const result = await r.json();
  if (!r.ok) {
    if (token && [401, 403].includes(r.status)) {
      rememberSession(null);
      authView = "login";
      login();
    }
    throw new Error(result.error || result.detail || "Request failed.");
  }
  return result;
}
async function refresh() {
  if (mode === "live") state = await api("/api/state");
  else persist();
  render();
}
let livePollRunning = false;
const editing = () =>
  modal.open || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
setInterval(async () => {
  if (mode !== "live" || !token || document.hidden || editing() || livePollRunning) return;
  livePollRunning = true;
  try {
    const results = await Promise.allSettled([api("/api/state"), api("/api/config")]);
    if (mode !== "live" || !token || editing()) return;
    let changed = false;
    if (results[0].status === "fulfilled") {
      changed = JSON.stringify(state) !== JSON.stringify(results[0].value);
      state = results[0].value;
    }
    if (results[1].status === "fulfilled") {
      changed ||= JSON.stringify(config) !== JSON.stringify(results[1].value);
      config = results[1].value;
    }
    if (changed) render();
  } finally {
    livePollRunning = false;
  }
}, 10000);
const pending = () => state.proposals.filter((p) => p.status === "pending");
const name = (phone) => {
  const v = state.volunteers.find((v) => v.phone === phone);
  return v ? `${v.first_name} ${v.last_name}` : phone;
};
const date = (s) =>
  new Date(s).toLocaleDateString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: "America/Denver",
  });
const time = (s) =>
  new Date(s).toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "America/Denver",
  });
const pill = (s, c = "blue") => `<span class="pill ${c}">${esc(s)}</span>`;
function login() {
  app.innerHTML = `<main class="login"><section class="login-story"><div class="brand">${icon("logo")} ${esc(config.name)}</div><div><h1>A little less scheduling.<br>A lot more people.</h1><p>Your volunteers text. You stay in the loop. Keep every ministry covered, with time left for the people who matter.</p><svg class="login-art" viewBox="0 0 500 210" fill="none" aria-hidden="true"><path d="M20 179h460M110 179V90l130-65 130 65v89" stroke="#829fb3" stroke-width="2"/><path d="M225 179v-63a16 16 0 0 1 32 0v63M185 87h110" stroke="#829fb3" stroke-width="2"/><path d="M240 44v31m-13-17h26" stroke="#567a93" stroke-width="3"/><circle cx="75" cy="132" r="13" fill="#a5bdcd"/><path d="M54 179v-20a21 21 0 0 1 42 0v20" fill="#a5bdcd"/><circle cx="408" cy="130" r="13" fill="#99afa9"/><path d="M387 179v-22a21 21 0 0 1 42 0v22" fill="#99afa9"/><circle cx="448" cy="143" r="10" fill="#b8aed0"/><path d="M432 179v-12a16 16 0 0 1 32 0v12" fill="#b8aed0"/></svg></div><span class="muted">Built for the people who keep church life moving.</span></section><section class="login-form"><div class="login-inner"><h2>${{ login: "Welcome back.", register: "Create your admin account.", recover: "Reset your password.", reset: "Choose a new password." }[authView]}</h2><p class="muted">${authView === "register" ? "For invited administrators. Volunteers sign up entirely by text." : authView === "recover" ? "We’ll email you a link to reset your password." : "Your coordinator workspace."}</p><form id="login-form">${authView !== "reset" ? '<label for="email">Email address</label><input id="email" name="email" type="email" autocomplete="username" value="jacobthebaer@gmail.com" required>' : ""}${authView !== "recover" ? `<label for="password">${authView === "login" ? "Password" : "New password"}</label><input id="password" name="password" type="password" autocomplete="${authView === "login" ? "current-password" : "new-password"}" ${authView !== "login" ? 'minlength="12" maxlength="128"' : ""} required>` : ""}${["register", "reset"].includes(authView) ? '<label for="confirm-password">Confirm password</label><input id="confirm-password" name="confirm_password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required>' : ""}<p id="login-error" class="error" role="status" aria-live="polite"></p><button class="primary" ${!config.connected ? "disabled" : ""}>${{ login: "Sign in", register: "Create account", recover: "Email reset link", reset: "Save new password" }[authView]}</button></form><div class="auth-links">${authView === "login" ? '<button class="quiet small" data-auth="register">Create admin account</button><button class="quiet small" data-auth="recover">Forgot password?</button>' : '<button class="quiet small" data-auth="login">Back to sign in</button>'}</div>${!config.connected ? '<p class="login-foot">Supabase sign-in is being connected. You can explore the demo below.</p>' : ""}<div class="divider">Explore first</div><button data-action="demo">Try the synthetic demo ${icon("arrow")}</button><p class="login-foot">Demo changes stay in this browser. No real people, live AI calls, or text deliveries.</p></div></section></main>`;
}
function title() {
  const labels = {
    overview: [
      "Your ministry, in view.",
      "A clear plan for the week. A little more room for people.",
    ],
    volunteers: [
      "People make it possible.",
      "Manage your volunteer roster, availability, and ministry clearance.",
    ],
    schedule: [
      "Every role has a person.",
      "See what’s covered and where your coordinator needs to step in.",
    ],
    messages: [
      "One text. The next step.",
      "Follow how texts update the roster and coverage.",
    ],
    settings: [
      "A thoughtful set of defaults.",
      "Keep people in control while your agent handles the admin.",
    ],
  };
  return `<header class="page-title"><div><h1>${labels[page][0]}</h1><p>${labels[page][1]}</p></div>${["overview", "volunteers"].includes(page) ? `<button class="primary" data-action="add">${icon("plus")} Add volunteer</button>` : ""}</header>`;
}
function render() {
  const count = pending().length;
  app.innerHTML = `<div class="shell"><aside class="sidebar"><div class="brand">${icon("logo")} ${esc(config.name)}</div><div class="org">Volunteer coordination</div><nav class="nav" aria-label="Main navigation">${[
    ["overview", "home", "Overview"],
    ["volunteers", "people", "Volunteers"],
    ["schedule", "calendar", "Schedule"],
    ["messages", "chat", "Text lab"],
    ["settings", "settings", "Settings"],
  ]
    .map(
      ([key, i, label]) =>
        `<button class="${page === key ? "active" : ""}" data-page="${key}" ${page === key ? 'aria-current="page"' : ""}>${icon(i)}${label}</button>`,
    )
    .join(
      "",
    )}</nav><div class="sidebar-bottom"><div class="profile"><span class="avatar">JB</span><div>Jacob Baer<small>${mode === "demo" ? "Demo coordinator" : "Administrator"}</small></div></div><button class="quiet small" data-action="logout">${mode === "demo" ? "Exit demo" : "Sign out"}</button></div></aside><div class="workspace"><header class="topbar"><div class="topbar-left">${icon("home")}<span>Coordinator workspace</span></div><div class="topbar-right"><span class="muted">${mode === "demo" ? "Sunday, October 4 demo" : "America/Denver"}</span><span class="status"><span class="dot"></span>${mode === "demo" ? "Texting simulated" : config.macBridgeConfigured ? (config.macBridgeConnected ? "Mac texting connected" : "Mac texting awaiting connection") : config.liveSms ? "Live texting enabled" : "Live texting off"}</span></div></header>${mode === "live" && config.humanConfirmationRequired ? '<div class="demo-banner">Human confirmation is active. Every text and automated record change waits for your exact review.</div>' : ""}${mode === "demo" ? `<div class="demo-banner"><span>Synthetic demo · sample rules, no live AI or SMS · changes saved in this browser only</span><button data-action="reset">Reset demo</button></div>` : ""}<main class="content">${title()}${{ overview: overview, volunteers: volunteers, schedule: schedule, messages: messages, settings: settings }[page]()}<p class="footer-note">${mode === "demo" ? "All names, numbers, and ministry records shown here are synthetic." : "Gloo handles coverage. Personal concerns stay with people."}</p></main></div></div>`;
}
function scheduleRows() {
  return state.shifts
    .map((s) => {
      const assigned = state.assignments.filter((a) => a.shift_id === s.id),
        gap = Math.max(0, s.required - assigned.length);
      return `<tr><td><strong>${esc(s.role)}</strong><small>${esc(s.title)}</small></td><td>${esc(s.ministry)}${s.sensitive ? "<small>Background check required</small>" : ""}</td><td>${date(s.starts_at)}<small>${time(s.starts_at)}–${time(s.ends_at)}</small></td><td>${pill(`${assigned.length} / ${s.required} covered`, gap ? "amber" : "green")}</td><td>${
        assigned
          .map((a) => {
            const v = state.volunteers.find((v) => v.id === a.volunteer_id);
            return v ? esc(v.first_name) : "";
          })
          .join(", ") || '<span class="muted">No assignments yet</span>'
      }</td></tr>`;
    })
    .join("");
}
function schedule() {
  return `<section class="panel table-wrap"><table><thead><tr><th>Role</th><th>Ministry</th><th>When</th><th>Coverage</th><th>Serving</th></tr></thead><tbody>${scheduleRows() || '<tr><td colspan="5" class="empty">No shifts yet. Existing schedule data can be connected when the repository arrives.</td></tr>'}</tbody></table></section>${replacementProgress()}<p class="notice section">Cancellations reopen the slot. Eligible replies update the calendar automatically. This dashboard’s text lab always simulates delivery.</p>`;
}
function approval(p) {
  const v = state.volunteers.find((v) => v.phone === p.phone),
    m =
      state.messages.find((m) => m.id === p.message_id) ||
      state.messages.find(
        (m) => m.phone === p.phone && m.direction === "inbound",
      );
  const requires = p.intent === "cancel" && !p.shift_id;
  return `<article class="approval ${p.intent === "care" ? "care" : ""}"><div class="approval-icon">${icon(p.intent === "care" ? "heart" : "chat")}</div><div class="approval-body"><div class="approval-top"><h3>${esc(p.summary)}</h3>${pill(p.intent === "care" ? "Human follow-up" : p.confidence < 0.7 ? "Needs clarification" : "Ready to review", p.intent === "care" ? "amber" : p.confidence < 0.7 ? "gray" : "purple")}</div><p>${esc(name(p.phone))} · ${p.provider ? esc(p.provider) : "Sample rules"}${p.shift_id ? " · " + esc(state.shifts.find((s) => s.id === p.shift_id)?.role || "Assigned shift") : ""}</p>${m ? `<div class="quote">“${esc(m.body)}”</div>` : ""}${p.confirmation_required ? `<p><strong>Recipient:</strong> ${esc(p.phone || "Record change")}</p><p><strong>Reason:</strong> ${esc(p.reason)}</p><p><strong>Expires:</strong> ${esc(p.expires_at)}</p>` : ""}${p.reply ? `<div class="quote">Exact text: ${esc(p.reply)}</div>` : ""}${p.record_change ? `<p>Record: ${esc(p.record_change.record)}</p><pre>Before: ${esc(JSON.stringify(p.record_change.before))}\nAfter: ${esc(JSON.stringify(p.record_change.after))}</pre>` : ""}${requires ? '<p class="error">Shift is unclear. Reject this proposal and clarify with the volunteer before cancelling.</p>' : ""}<div class="approval-actions"><button class="primary small" data-approve="${esc(p.id)}" ${requires ? "disabled" : ""}>${icon("check")}${p.confirmation_required ? (p.record_change ? "Approve exact change" : "Approve exact text") : p.intent === "care" ? "Mark reviewed" : p.intent === "accept" ? "Review offer" : "Approve action"}</button><button class="quiet small" data-reject="${esc(p.id)}">Dismiss</button></div>${p.intent === "care" ? '<p class="muted">Coordinator follow-up required. No pastor is contacted automatically.</p>' : ""}${p.intent === "accept" ? '<p class="muted">This confirms review of the offer. No assignment is made.</p>' : ""}</div></article>`;
}
function replacementProgress() {
  const fills = (state.fills || []).filter(f => !["filled", "skipped"].includes(f.state));
  const labels = {in_progress: "Asking replacements", waiting_quiet: "Waiting until morning", waiting_approval: "Clearance approval needed", escalated: "Needs your help", open: "Search starting"};
  const status = fills.map(f => {
    const shift = state.shifts.find(s => s.id === f.shift_id);
    return `<article class="insight"><h3>${esc(shift?.role || "Shift")} · ${esc(labels[f.state] || f.state)}</h3><p>Batch ${f.batch} · ${f.asked} invited · ${f.declined} declined${f.next_action_at ? ` · Next check ${esc(fmt(f.next_action_at))}` : ""}</p></article>`;
  }).join("");
  const events = (state.staffing || []).map(e => `<article class="insight"><h3>${esc(e.title)} ${pill(e.fully_staffed ? "Fully staffed" : "Needs cover", e.fully_staffed ? "green" : "amber")}</h3><p>${e.covered}/${e.required} required spots covered${e.gaps.length ? ` · ${e.gaps.map(g => `${esc(g.role)}: ${g.open} open`).join(" · ")}` : ""}</p></article>`).join("");
  if (!status && !events) return "";
  return `<section class="section"><div class="section-heading"><h2>Coverage in motion</h2>${pill("Updates automatically", "gray")}</div><div class="panel">${events}${status}</div><p class="muted">Roster and calendar changes appear here immediately. Coordinator texts combine changes after 5 minutes, with at least 15 minutes between status updates.</p></section>`;
}
function timingPanel() {
  return `<section class="section panel settings-panel"><h2>A considerate texting rhythm</h2><table><thead><tr><th>Time until the shift</th><th>Wait before the next batch</th></tr></thead><tbody><tr><td>More than 48 hours</td><td>4 hours; later batches 6 hours</td></tr><tr><td>12–48 hours</td><td>1 hour; later batches 2 hours</td></tr><tr><td>2–12 hours</td><td>20 minutes; later batches 30 minutes</td></tr><tr><td>Under 2 hours</td><td>10 minutes</td></tr></tbody></table><p>Start with 3 people; later batches ask up to 5. Under 2 hours, start with up to 5. No repeated nudges for the same offer.</p><p>One ask per person per 24 hours, at most ${state.timing?.monthly_ask_limit || 4} asks per month. Routine invitations pause 9pm–7am; same-day urgent invitations pause 9:30pm–6:30am. Replies to a text someone just sent can be immediate.</p><p>The first eligible YES gets the spot and a confirmation. Other invitees receive one closure; late YES replies never add a second volunteer.</p></section>`;
}
function overview() {
  const needed = state.shifts.reduce((n, s) => n + s.required, 0),
    covered = state.shifts.reduce((n, shift) => n + Math.min(shift.required, state.assignments.filter(a => a.shift_id === shift.id).length), 0),
    open = Math.max(0, needed - covered),
    list = pending();
  return `<div class="overview"><section class="welcome"><div><h2>Sunday is coming.<br>Let’s make everyone feel welcome.</h2><p>${open ? `${open} open roles need a little attention. ${config.humanConfirmationRequired && mode === "live" ? "Eligible replacements are proposed for your exact review. Each invitation waits for your approval." : "Eligible replacements are invited automatically. Review only the items that need your help."}` : "Your current roster covers every role. Review incoming texts to keep it that way."}</p><button data-page="schedule">View the schedule ${icon("arrow")}</button></div><div class="date-card"><span>Oct</span><strong>4</strong><span>Sunday</span></div></section><section class="health"><h3>Ahead of the week</h3><div class="health-line"><span>Roles covered</span><strong>${covered} / ${needed}</strong></div><progress class="meter" value="${covered}" max="${needed || 1}" aria-label="Roles covered"></progress><div class="health-line"><span>Active volunteers</span><strong>${state.volunteers.filter((v) => v.status === "active").length}</strong></div><div class="health-line"><span>Waiting on your review</span><strong>${list.length}</strong></div></section></div>${mode === "live" && state.escalations?.length ? `<section class="section panel">${state.escalations.map((e) => `<article class="insight"><h3>${icon("heart")}Human follow-up · ${esc(e.severity)}</h3><p>${esc(e.summary)}</p></article>`).join("")}</section>` : ""}${replacementProgress()}<div class="columns"><section class="section"><div class="section-heading"><h2>Your next steps <span class="count">${list.length}</span></h2><button class="quiet small" data-page="messages">Open text lab ${icon("arrow")}</button></div><div class="panel">${list.length ? list.slice(0, 5).map(approval).join("") : '<div class="empty">You’re all caught up.<p>Try a sample text to see a new proposed action.</p><button class="quiet" data-page="messages">Try a text</button></div>'}</div></section><section class="section"><div class="section-heading"><h2>A little foresight</h2>${pill("From your roster", "gray")}</div><div class="panel"><article class="insight"><h3>${icon("people")} Clear before they serve</h3><p>${state.volunteers.filter((v) => v.ministry === "Kids" && !v.qualified).length} kids ministry volunteer(s) still need coordinator clearance. Texting YES never grants a qualification.</p><button class="quiet small" data-page="volunteers">Review volunteers ${icon("arrow")}</button></article><article class="insight"><h3>${icon("calendar")} ${open} roles need cover</h3><p>Gloo selects eligible replacements in small batches. Qualifications, availability, and text limits apply before each invitation.</p><button class="quiet small" data-page="schedule">Review coverage ${icon("arrow")}</button></article><article class="insight"><h3>${icon("heart")} Care stays personal</h3><p>Messages about illness, grief, or personal concerns go to a human review queue.</p></article></div></section></div><section class="section"><div class="section-heading"><h2>Coming up</h2><button class="quiet small" data-page="schedule">All shifts ${icon("arrow")}</button></div><div class="panel table-wrap"><table><thead><tr><th>Role</th><th>Ministry</th><th>When</th><th>Coverage</th><th>Serving</th></tr></thead><tbody>${scheduleRows()}</tbody></table></div></section>`;
}
function volunteers() {
  const list = state.volunteers.filter(
    (v) =>
      `${v.first_name} ${v.last_name} ${v.phone}`
        .toLowerCase()
        .includes(filter.toLowerCase()) &&
      (ministry === "all" || v.ministry === ministry),
  );
  return `<div class="toolbar"><input id="search" aria-label="Search volunteers" placeholder="Search names or phone numbers" value="${esc(filter)}"><select id="ministry-filter" aria-label="Filter by ministry"><option value="all">All ministries</option>${[...new Set(state.volunteers.map((v) => v.ministry))].map((m) => `<option ${m === ministry ? "selected" : ""}>${esc(m)}</option>`).join("")}</select></div><div class="panel table-wrap"><table><thead><tr><th>Volunteer</th><th>Ministry</th><th>Availability</th><th>Clearance</th><th>Status</th><th></th></tr></thead><tbody>${list.map((v) => `<tr><td><div class="person"><span class="avatar">${initials(v)}</span><div><strong>${esc(v.first_name)} ${esc(v.last_name)}</strong><small>${esc(v.phone)}</small></div></div></td><td>${esc(v.ministry)}</td><td>${esc(v.availability)}</td><td>${pill(v.qualified ? "Coordinator cleared" : "Needs review", v.qualified ? "green" : "amber")}${v.background_check_until ? `<small>Check until ${esc(v.background_check_until)}</small>` : ""}</td><td>${pill(v.status, v.status === "active" ? "green" : "gray")}<small>${v.consent ? "Text consent recorded" : "No text consent"}</small>${v.onboarding_stage && v.onboarding_stage !== "complete" ? `<small>Setup: ${esc(v.onboarding_stage)}</small>` : ""}</td><td>${mode === "live" && v.can_start_text_setup ? `<button class="quiet small" data-text-setup="${esc(v.id)}">${v.onboarding_stage === "not_started" ? "Start text setup" : "Restart text setup"}</button>` : ""}<button class="quiet small" data-edit="${esc(v.id)}">Edit</button></td></tr>`).join("") || '<tr><td colspan="6" class="empty">No volunteers match your search.</td></tr>'}</tbody></table></div>`;
}
function messages() {
  const thread = state.messages.filter((m) => m.phone === simPhone);
  return `<div class="split"><section class="panel composer"><h2>Try an incoming text</h2><p class="muted">${mode === "demo" ? "Sample rules demonstrate the workflow. Connect the backend to test Gloo decisions." : `Incoming texts are processed by ${esc(config.provider)}. Proposed changes require review.`}</p><form id="simulate-form"><label for="sim-phone">Volunteer phone</label><input id="sim-phone" name="phone" value="${esc(simPhone)}" required><label for="sim-body">Incoming message</label><textarea id="sim-body" name="body" placeholder="I can’t make the nursery shift on Sunday." required maxlength="1600"></textarea><div class="sample-buttons"><button type="button" data-sample="signup">New volunteer</button><button type="button" data-sample="cancel">Cancellation</button><button type="button" data-sample="availability">Availability</button><button type="button" data-sample="care">Personal concern</button><button type="button" data-sample="stop">Opt out</button></div><p id="sim-error" class="error"></p><button class="primary section">Process test message ${icon("arrow")}</button></form>${trace ? `<div class="trace"><strong>Action trace</strong>${esc(trace)}</div>` : ""}</section><section><div class="section-heading"><h2>Text history</h2>${pill(config.humanConfirmationRequired && mode === "live" ? "Every text requires review" : "Replies follow role policy", "purple")}</div><div class="panel thread">${thread.map((m) => `<div class="message ${esc(m.direction)}"><div class="bubble">${esc(m.body)}</div><small>${esc(m.direction === "inbound" ? name(m.phone) : "Coordinator reply")} · ${esc(m.status)}${mode === "live" && m.status === "draft" ? ` <button class="small" data-send="${esc(m.id)}" ${!config.liveSms ? "disabled" : ""}>Send approved text</button>` : ""}</small></div>`).join("") || '<div class="empty">Choose a sample to start a conversation.</div>'}</div></section></div><section class="section"><div class="section-heading"><h2>Proposed actions <span class="count">${pending().length}</span></h2></div><div class="panel">${pending().map(approval).join("") || '<div class="empty">No pending actions.</div>'}</div></section>`;
}
function settings() {
  return `<div class="settings-grid"><section class="panel settings-panel"><h2>Connections</h2><p>Incoming text → Gloo → validated roster update → calendar and status notifications.</p>${[
    ["Hosting", "Cloudflare Workers", "green"],
    [
      "Volunteer database",
      config.connected
        ? "Supabase connected"
        : "Separate Supabase account pending",
      config.connected ? "green" : "amber",
    ],
    [
      "Text interpretation",
      config.aiReady ? `${config.provider} connected` : "Gloo backend pending",
      config.aiReady ? "green" : "amber",
    ],
    [
      "Text delivery",
      config.macBridgeConfigured
        ? config.macBridgeConnected
          ? "Mac texting connected"
          : "Mac texting awaiting connection"
        : config.liveSms
          ? "Live texting enabled"
          : "Live texting off",
      config.liveSms ? "amber" : "green",
    ],
  ]
    .map(
      ([k, v, c]) =>
        `<div class="setting-row"><span>${k}</span>${pill(v, c)}</div>`,
    )
    .join(
      "",
    )}</section><section class="panel settings-panel"><h2>People stay in control</h2><p>These guardrails are part of the demo’s behavior.</p>${[
    ["Ordinary role replacements", "Automatic after an eligible YES"],
    ["Delivery mode", config.macBridgeConfigured || config.liveSms ? "Test phones only" : "Delivery paused; simulations only"],
    ["Human confirmation", config.humanConfirmationRequired ? "Active: review every exact text and automated record change" : "Off: existing role approval rules apply"],
    ["Background searches", mode === "demo" ? "Simulated" : config.automationEnabled ? "Running automatically" : "Paused for testing"],
    ["Ministry qualifications", "Verified individually by coordinator"],
    ["Personal concerns", "Human review; no AI counseling"],
    ["Opt-outs", "STOP takes effect immediately"],
    ["Quiet hours", "Configured by the scheduling core"],
    ["Message limit", "Configured monthly outreach budget"],
  ]
    .map(
      ([k, v]) =>
        `<div class="setting-row"><span>${k}</span><strong>${v}</strong></div>`,
    )
    .join(
      "",
    )}</section></div>${timingPanel()}<p class="notice section">Volunteers use a phone number and reply by text. Signup collects consent, interests, availability, and serving frequency. The backend saves their profile and recognizes their number on future messages.</p>`;
}
function volunteerModal(v) {
  modal.innerHTML = `<div class="modal-heading"><h2>${v ? "Edit volunteer" : "Add a volunteer"}</h2><button class="quiet small" data-close aria-label="Close dialog">✕</button></div><form id="volunteer-form" data-id="${v?.id || ""}"><div class="form-row"><div><label for="first_name">First name</label><input id="first_name" name="first_name" value="${esc(v?.first_name)}" required maxlength="80"></div><div><label for="last_name">Last name</label><input id="last_name" name="last_name" value="${esc(v?.last_name)}" required maxlength="80"></div></div><label for="phone">Phone number</label><input id="phone" name="phone" type="tel" placeholder="+13035550123" value="${esc(v?.phone)}" required ${v ? "readonly" : ""}><label for="ministry">Preferred ministry</label><select id="ministry" name="ministry">${["Welcome", "Kids", "Food pantry", "Production", "Care", "Youth"].map((m) => `<option ${v?.ministry === m ? "selected" : ""}>${m}</option>`).join("")}</select>${v ? `<label for="availability">Availability</label><input id="availability" name="availability" value="${esc(v.availability)}" maxlength="500"><label for="status">Status</label><select id="status" name="status">${["active", "pending", "paused", "opted_out"].map((s) => `<option ${v.status === s ? "selected" : ""}>${s}</option>`).join("")}</select><label class="check"><input name="qualified" type="checkbox" ${v.qualified ? "checked" : ""}>Sample clearance (preview only)</label><label for="background">Background check valid through</label><input id="background" name="background_check_until" type="date" value="${esc(v.background_check_until)}">` : ""}<label class="check"><input name="consent" type="checkbox" ${v?.consent ? "checked" : ""}>I have verified this person agreed to receive scheduling texts.</label><p class="muted">Adding a phone never grants an administrator login.</p><p id="volunteer-error" class="error"></p><div class="modal-actions"><button type="button" data-close>Cancel</button><button class="primary">Save volunteer</button></div></form>`;
  modal.showModal();
  if (mode === "live" && v) {
    frozenQualifications();
  }
}
function frozenQualifications() {
  for (const field of ["qualified", "background_check_until"]) {
    const input = modal.querySelector(`[name="${field}"]`);
    if (input) input.disabled = true;
  }
  const note = document.createElement("p");
  note.className = "notice";
  note.textContent =
    "Individual qualifications remain managed in the existing coordinator application. Texty cannot grant blanket clearance.";
  modal.querySelector("form").append(note);
}
const samples = {
  signup: ["+12025550199", "Join Alex Morgan"],
  cancel: ["+12025550100", "I can’t make the nursery shift on Sunday."],
  availability: [
    "+12025550101",
    "I am available on second and fourth Sundays after 9 AM.",
  ],
  care: [
    "+12025550100",
    "My dad is in the hospital and I can’t make it Sunday.",
  ],
  stop: ["+12025550101", "STOP"],
};
document.addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.auth) {
      authView = b.dataset.auth;
      login();
    }
    if (b.dataset.page) {
      page = b.dataset.page;
      render();
    }
    if (b.dataset.action === "demo") {
      mode = "demo";
      page = "overview";
      render();
    }
    if (b.dataset.action === "logout") {
      if (mode === "live" && token) await api("/api/logout", {});
      rememberSession(null);
      authView = "login";
      login();
    }
    if (b.dataset.action === "reset") {
      state = seed();
      persist();
      render();
      toast("Synthetic demo reset.");
    }
    if (b.dataset.action === "add") volunteerModal();
    if (b.dataset.edit)
      volunteerModal(state.volunteers.find((v) => v.id === b.dataset.edit));
    if (b.dataset.textSetup) {
      b.disabled = true;
      const setup = await api(`/api/volunteers/${b.dataset.textSetup}/text-setup`, {});
      await refresh();
      toast(setup.approval_id ? "Setup text awaits your exact review." : "Setup text queued. Their replies finish the profile automatically.");
    }
    if (b.hasAttribute("data-close")) modal.close();
    if (b.dataset.sample) {
      [simPhone] = samples[b.dataset.sample];
      document.querySelector("#sim-phone").value = simPhone;
      document.querySelector("#sim-body").value = samples[b.dataset.sample][1];
    }
    if (b.dataset.approve || b.dataset.reject) {
      b.disabled = true;
      const pid = b.dataset.approve || b.dataset.reject,
        p = state.proposals.find((p) => p.id === pid);
      if (mode === "demo") {
        if (b.dataset.approve) applyDemo(state, p);
        else p.status = "rejected";
      } else
        await api(
          `/api/proposals/${pid}/${b.dataset.approve ? "approve" : "reject"}`,
          p.confirmation_required ? { content_hash: p.content_hash } : {},
        );
      await refresh();
      toast(
        b.dataset.approve
          ? "Action reviewed. Check text history for the outcome."
          : "Proposal dismissed.",
      );
    }
    if (b.dataset.send) {
      b.disabled = true;
      await api(`/api/messages/${b.dataset.send}/send`, {});
      await refresh();
      toast("Approved text sent through Twilio.");
    }
  } catch (error) {
    b.disabled = false;
    toast(error.message);
  }
});
document.addEventListener("change", (e) => {
  if (e.target.id === "ministry-filter") {
    ministry = e.target.value;
    render();
  }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "search") {
    const pos = e.target.selectionStart;
    filter = e.target.value;
    render();
    const s = document.querySelector("#search");
    s.focus();
    s.setSelectionRange(pos, pos);
  }
});
document.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target,
    data = Object.fromEntries(new FormData(f)),
    b = f.querySelector("button.primary");
  if (b) b.disabled = true;
  try {
    if (f.id === "login-form") {
      if (
        ["register", "reset"].includes(authView) &&
        data.password !== data.confirm_password
      )
        throw new Error("The passwords don’t match.");
      const route = {
        login: "login",
        register: "register",
        recover: "recover",
        reset: "reset-password",
      }[authView];
      const result = await api(`/api/${route}`, data);
      f.reset();
      if (authView === "login") {
        token = result.access_token;
        mode = "live";
        page = "overview";
        await refresh();
        rememberSession(token);
      } else {
        if (authView === "reset") {
          rememberSession(null);
          authView = "login";
          login();
        }
        document.querySelector("#login-error").textContent = result.message;
      }
    }
    if (f.id === "volunteer-form") {
      data.consent = f.elements.consent.checked;
      const valid = validateVolunteer(data);
      if (f.dataset.id) {
        Object.assign(valid, {
          qualified: f.elements.qualified.checked,
          status: data.status,
          availability: data.availability,
          background_check_until: data.background_check_until || null,
        });
        if (mode === "demo") {
          const v = state.volunteers.find((v) => v.id === f.dataset.id);
          if (
            v.status === "opted_out" &&
            (valid.consent || valid.status !== "opted_out")
          )
            throw new Error(
              "An opted-out phone needs a separate re-subscription workflow.",
            );
          Object.assign(v, valid);
        } else await api(`/api/volunteers/${f.dataset.id}`, valid);
      } else if (mode === "demo") {
        if ((state.optouts || []).includes(valid.phone) && valid.consent)
          throw new Error(
            "This phone opted out. Re-subscription must be verified first.",
          );
        if (state.volunteers.some((v) => v.phone === valid.phone))
          throw new Error("This phone already has a profile.");
        state.volunteers.unshift({
          ...valid,
          id: id(),
          qualified: false,
          status: "active",
          availability: "Not provided",
        });
      } else await api("/api/volunteers", valid);
      modal.close();
      await refresh();
      toast("Volunteer saved.");
    }
    if (f.id === "simulate-form") {
      simPhone = data.phone;
      if (!phoneValid(simPhone) || !data.body.trim())
        throw new Error("Enter an international phone number and a message.");
      if (mode === "demo") {
        const d = previewDecision(data.body),
          v = state.volunteers.find((v) => v.phone === simPhone),
          mid = id();
        state.messages.push({
          id: mid,
          phone: simPhone,
          body: data.body,
          direction: "inbound",
          status: "received",
          created_at: new Date().toISOString(),
        });
        if (d.intent === "stop") {
          state.optouts = [...new Set([...(state.optouts || []), simPhone])];
          if (v) {
            v.status = "opted_out";
            v.consent = false;
          }
          state.messages
            .filter((m) => m.phone === simPhone && m.status === "draft")
            .forEach((m) => (m.status = "suppressed"));
          trace =
            "Sample rules → opt-out recorded immediately → queued replies suppressed → no real text sent.";
        } else if (processDemoSignup(state, simPhone, data.body)) {
          trace =
            "Sample signup conversation → profile and consent handled by text → no admin approval or real delivery.";
        } else {
          if (d.intent === "cancel" && v) {
            const assigned = state.assignments.filter(
              (a) => a.volunteer_id === v.id,
            );
            if (assigned.length === 1) d.shift_id = assigned[0].shift_id;
          }
          state.proposals.unshift({
            ...d,
            id: id(),
            message_id: mid,
            phone: simPhone,
            status: "pending",
            provider: "sample rules",
            created_at: new Date().toISOString(),
          });
          trace = `Sample rules → ${d.intent} → proposed action saved → waiting for coordinator review → no real text sent.`;
        }
      } else {
        const result = await api("/api/simulate", data);
        trace = `${result.decision.provider} → ${result.decision.intent} → ${result.notes?.join("; ") || "scheduling core processed the message"} → no real text sent.`;
      }
      await refresh();
      toast("Test message processed.");
    }
  } catch (error) {
    const output = f.querySelector(".error");
    if (output) output.textContent = error.message;
    else toast(error.message);
  } finally {
    if (b) b.disabled = false;
  }
});
try {
  config = await api("/api/config");
} catch {
  toast("Connection unavailable. Synthetic demo is still accessible.");
}
const callback = new URLSearchParams(location.hash.slice(1));
if (location.hash) history.replaceState(null, "", location.pathname);
if (callback.get("access_token")) {
  token = callback.get("access_token");
  if (callback.get("type") === "recovery") {
    authView = "reset";
    login();
  } else {
    try {
      mode = "live";
      await refresh();
      rememberSession(token);
      toast("Email confirmed. Welcome to Texty.");
    } catch (error) {
      rememberSession(null);
      login();
      toast(error.message);
    }
  }
} else if (savedSession() && !callback.get("error_description")) {
  token = savedSession();
  mode = "live";
  try {
    await refresh();
  } catch (error) {
    rememberSession(null);
    login();
    toast(error.message);
  }
} else {
  login();
  if (callback.get("error_description"))
    toast(callback.get("error_description"));
}
