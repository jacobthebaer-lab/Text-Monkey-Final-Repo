import {createPlanningCenterReview} from './planning-center-review.js';
import {createAdminNotifications, textStatusLabel, reviewOutcomeLabel} from './admin-notifications.js';
import {createPlanningWorkflows, planningAdapter} from './planning-workflows.js';
import { focusView } from './accessibility.js';
import {adminReadiness} from './admin-readiness.js';
import {createCloudTexting} from './cloud-texting.js';
import { createSetup, accountChurchFields, registrationDetails } from "./setup.js";
import {
  seed,
  id,
  validateVolunteer,
  demoBooking,
} from "./domain.js";
const productName = "Text Monkey";
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
};
const icon = (n) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[n] || icons.chat}</svg>`;
const brand = (dark = false) => `<img class="brand-logo" src="/brand/textmonkey-logo-horizontal-${dark ? "dark" : "transparent"}.png" alt="${esc(productName)}" width="2048" height="703">`;
const initials = (v) => esc(v.first_name[0] + v.last_name[0]);
let config = {
    connected: false,
    name: "Text Monkey",
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
  authView = "login",
  replyRecipient = "",
  replyBody = "",
  replyStatus = "",
  replyRequestId = "";
const storeKey = "texty.synthetic.v1";
const sessionKey = "texty.coordinator.session.v1";
const rememberSession = (value) => {
  token = value;
  if (!value) { replyRecipient = replyBody = replyStatus = replyRequestId = ""; adminCheckRequestId = ""; lastReviewOutcome = ""; cloudTexting.reset(); planningCenterReview.reset(); }
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
async function api(path, body, options = {}) {
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
    if (token && (r.status === 401 || (r.status === 403 && !options.keepSessionOnForbidden))) {
      rememberSession(null);
      authView = "login";
      login();
    }
    throw Object.assign(new Error(result.error || result.detail || "Request failed."), {status:r.status});
  }
  return result;
}
const churchSetup = createSetup({ api, getMode: () => mode, getToken: () => token, render, toast, onComplete: async () => { await loadAdminTexts(); page = "settings"; } });
const planningCenterReview = createPlanningCenterReview({api,getMode:()=>mode,getToken:()=>token,getVolunteers:()=>state.volunteers,render});
const cloudTexting = createCloudTexting({api, getMode:()=>mode, getToken:()=>token, getConfig:()=>config, render});
let adminTexts = null, adminTextsError = "", adminTextsSaving = false, adminCheckRequestId = "";
const planningWorkflows = createPlanningWorkflows({adapter:planningAdapter(api), getMode:()=>mode, getToken:()=>token, render, onChanged:async()=>{state=await api("/api/state");}});
let lastReviewOutcome = "";
const adminNotifications = createAdminNotifications({api, getMode:()=>mode, getToken:()=>token, getConfig:()=>config, render});
async function loadAdminTexts() {
  if (mode !== "live" || !token) { adminTexts = null; adminTextsError = ""; return; }
  try { adminTexts = await api('/api/setup/admin-texts'); adminCheckRequestId ||= adminTexts.pending_check?.request_id || ''; adminTextsError = ""; }
  catch (error) { adminTexts = null; adminTextsError = error.message; }
}
const deliveryLabel = status => config.messagingTransport === 'google_voice' && status === 'queued' ? 'Saved queued record' : config.messagingTransport === 'google_voice' && status === 'submitted' ? 'Historical submission record' : textStatusLabel(status);
function adminTextPanel() {
  if (mode === 'demo') return `<section class="panel settings-panel admin-text-panel"><h2>Admin text updates</h2><p>Real admin updates use your saved mobile number, Gloo AI and the laptop’s Messages connection.</p><p class="notice">This offline dashboard has no texting connection. Open the connected admin console to save your number and send real texts.</p></section>`;
  return `<section class="panel settings-panel admin-text-panel"><div class="section-heading"><h2>Keep me updated by text</h2>${pill(adminTexts?.ready?'Ready':adminTexts?.enabled?'Needs attention':'Off',adminTexts?.ready?'green':'amber')}</div><p>Get a status text ${adminTexts?.pre_event_hours || 3} hours before each event: what’s covered, what’s missing, and whether you need to act. Coverage changes and approval requests keep you in the loop between events.</p>${adminTextsError?`<p class="error" role="alert">Couldn’t check admin updates: ${esc(adminTextsError)}</p><button data-action="reload-admin-texts">Retry connection check</button>`:''}${adminReadiness(adminTexts, esc)}${adminTexts?.review_required?'<p class="notice">Competition review is on. Manual and scheduled admin texts wait for exact review in Messages before delivery.</p>':''}<form id="admin-text-form"><label for="admin-mobile">Your mobile number</label><input id="admin-mobile" name="phone" type="tel" autocomplete="tel" maxlength="40" value="${esc(adminTexts?.phone || churchSetup.details().coordinator_phone || '')}" placeholder="(303) 555-0123" required><p class="field-hint">Your own mobile, not the church’s texting line. Include +country code outside the US or Canada.</p><label class="check"><input type="checkbox" name="consent" ${adminTexts?.enabled?'checked':''} required> This is my mobile number and I want admin text updates.</label><p class="field-hint">Reply STOP to stop texts or HELP for help. Quiet hours apply. Saving does not send a test message.</p><p class="error" role="alert"></p><div class="setup-actions"><button class="primary" name="action" value="enable" ${adminTextsSaving?'disabled':''}>${adminTextsSaving?'Saving…':'Save text updates'}</button>${adminTexts?.enabled?'<button name="action" value="pause" formnovalidate>Pause my updates</button>':''}</div></form><button data-action="send-admin-check" class="section" ${!adminTexts?.connection_check_ready?'disabled':''}>${adminCheckRequestId ? 'Retry my connection check' : 'Send me a connection check'}</button>${adminTexts?.recent?.length?`<div class="admin-receipts"><h3>Recent admin texts</h3>${adminTexts.recent.map(row=>`<article><p>${esc(row.body)}</p><small>${esc(deliveryLabel(row.status))} · ${date(row.created_at)} ${time(row.created_at)}</small></article>`).join('')}</div>`:'<p class="field-hint">No admin texts have been recorded for this account yet.</p>'}</section>`;
}
async function openCoordinatorWorkspace() {
  // Verify the restored bearer with the existing roster API before loading setup.
  state = await api("/api/state");
  await churchSetup.load();
  if (!token) throw new Error("Session expired. Sign in again.");
  page = churchSetup.completed() ? "overview" : "setup";
  await loadAdminTexts();
  await cloudTexting.load();
  render();
}
async function refresh() {
  if (mode === "live") { state = await api("/api/state"); await loadAdminTexts(); if (page === "settings") await cloudTexting.load(); if(page === "schedule") { await planningWorkflows.load(); await adminNotifications.load(); } }
  else persist();
  render();
}
let livePollRunning = false;
const editing = () =>
  ["setup", "import"].includes(page) || modal.open || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
setInterval(async () => {
  if (mode !== "live" || !token || document.hidden || editing() || livePollRunning) return;
  livePollRunning = true;
  try {
    const results = await Promise.allSettled([api("/api/state"), api("/api/config")]);
    if (page === "settings" && mode === "live" && token) await cloudTexting.load();
    if (mode !== "live" || !token || editing()) return;
    let changed = page === "settings";
    if (results[0].status === "fulfilled") {
      changed ||= JSON.stringify(state) !== JSON.stringify(results[0].value);
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
    timeZone: churchSetup.details().timezone || "America/Denver",
  });
const time = (s) =>
  new Date(s).toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: churchSetup.details().timezone || "America/Denver",
  });
const pill = (s, c = "blue") => `<span class="pill ${c}">${esc(s)}</span>`;
function login() {
  if(config.publicDemo) {
    app.innerHTML = `<main id="main-content" class="login" tabindex="-1"><section class="login-story"><div class="brand">${brand()}</div><div><h1>JUST TEXT.<br> WE’LL HANDLE<br> THE MONKEY<br> BUSINESS.</h1><p>Explore how Text Monkey helps church coordinators keep volunteers and shifts together.</p><img class="login-art" src="/brand/textmonkey-mark-transparent.png" width="1500" height="1650" alt="" aria-hidden="true"></div></section><section class="login-form"><div class="login-inner"><h2>Try Text Monkey.</h2><p class="muted">A sample church, volunteer roster and event schedule. No account needed. No real texts are sent.</p><button class="primary section" data-action="demo">Open the demo ${icon('arrow')}</button><p class="field-hint section">Changes stay in this browser. Account creation, live AI and real texting are available only in the connected administrator app.</p></div></section></main>`;
    return;
  }
  app.innerHTML = `<main id="main-content" class="login" tabindex="-1"><section class="login-story"><div class="brand">${brand()}</div><div><h1>JUST TEXT.<br> WE’LL HANDLE<br> THE MONKEY<br> BUSINESS.</h1><p>Volunteer scheduling with a little less chasing. Your people text. You review. Text Monkey keeps the next step clear.</p><img class="login-art" src="/brand/textmonkey-mark-transparent.png" width="1500" height="1650" alt="" aria-hidden="true"></div><span class="muted">Built for the people who keep church life moving.</span></section><section class="login-form"><div class="login-inner"><h2>${{ login: "Welcome back.", register: "Create your account.", recover: "Reset your password.", reset: "Choose a new password." }[authView]}</h2><p class="muted">${authView === "register" ? "Use your invited church email. Add your church details now, then confirm your email to start." : authView === "recover" ? "We’ll email you a link to reset your password." : "Your Text Monkey coordinator workspace."}</p><form id="login-form">${authView !== "reset" ? '<label for="email">Email</label><input id="email" name="email" type="email" autocomplete="username" placeholder="Email" required>' : ""}${authView !== "recover" ? `<label for="password">${authView === "reset" ? "New password" : "Password"}</label><input id="password" name="password" type="password" placeholder="Password" autocomplete="${authView === "login" ? "current-password" : "new-password"}" ${authView !== "login" ? 'minlength="12" maxlength="128"' : ""} required>` : ""}${["register", "reset"].includes(authView) ? '<label for="confirm-password">Confirm password</label><input id="confirm-password" name="confirm_password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required>' : ""}${authView === "register" ? accountChurchFields() : ""}<p id="login-error" class="error" role="status" aria-live="polite"></p><button class="primary" ${!config.connected ? "disabled" : ""}>${{ login: "Sign in", register: "Create account", recover: "Email reset link", reset: "Save new password" }[authView]}</button></form><div class="auth-links">${authView === "login" ? '<button class="quiet small" data-auth="register">Create admin account</button><button class="quiet small" data-auth="recover">Forgot password?</button>' : '<button class="quiet small" data-auth="login">Back to sign in</button>'}</div>${!config.connected ? '<p class="login-foot">Sign-in is awaiting the account connection. Explore the synthetic preview below.</p>' : ""}${!config.connected ? `<div class="divider">Developer preview</div><button data-action="demo">Open synthetic preview ${icon("arrow")}</button>` : ""}</div></section></main>`;
}
const pageNames = { overview: "Home", volunteers: "Volunteers", schedule: "Shifts", messages: "Messages", setup: "Church profile", import: "Import volunteers", settings: "Settings" };
function summary(items, label) {
  return `<section class="workspace-summary" aria-label="${esc(label)}">${items.map(([value, name, hint, tone]) => `<article class="summary-item ${tone || ''}"><span>${esc(name)}</span><strong>${esc(value)}</strong><small>${esc(hint)}</small></article>`).join('')}</section>`;
}
function title() {
  const labels = {
    overview:["Here’s where things stand.","Your next events, what needs you, and the texts keeping you in the loop."],
    volunteers:["Volunteers","The people who make it happen. Keep their details and consent together."],
    schedule:["Shifts","Your upcoming roles, coverage, and the people serving."],
    messages:["Messages","Volunteer replies and anything that needs your attention."],
    setup:["Finish your account","A few church details, then you’re ready."],
    import:["Import volunteers","Review your list before adding anyone."],
    settings:["Settings","Church details and texting preferences."],
  };
  return `<header class="page-title"><div><h1>${labels[page][0]}</h1><p>${labels[page][1]}</p></div>${page==='volunteers'?`<div class="setup-actions"><button data-page="import">Import volunteers</button><button class="primary" data-action="add">${icon('plus')} Add volunteer</button></div>`:''}</header>`;
}
function render() {
  const nav = mode === "live" || config.publicDemo ? [
    ["overview", "home", "Home"], ["volunteers", "people", "Volunteers"], ["schedule", "calendar", "Shifts"], ["messages", "chat", "Messages"],
  ] : [["overview", "home", "Overview"], ["volunteers", "people", "Volunteers"], ["schedule", "calendar", "Shifts"], ["messages", "chat", "Messages"], ["setup", "check", "Church profile"], ["import", "people", "Import"]];
  const cloud = config.messagingTransport === 'google_voice';
  const transportConnected = cloud ? cloudTexting.summary()?.connected === true : config.macBridgeConnected;
  const status = mode === "demo" ? "Texting disconnected" : !config.aiReady ? "AI disconnected" : !transportConnected ? (cloud ? cloudTexting.summary()?.label || 'Cloud connection not checked' : "Texting offline") : !config.automationEnabled ? "Automation paused" : "Texting connected";
  const content = {setup:churchSetup.screen, import:churchSetup.importScreen, overview, volunteers, schedule, messages, settings}[page];
  app.innerHTML = `<div class="shell" data-portal-version="2026-10-03-polish"><aside class="sidebar"><div class="brand">${brand(true)}</div><div class="org">${esc(churchSetup.details().church_name || (mode === "demo" ? "Cedar Hills Community Church" : "Your church"))}</div><nav class="nav" aria-label="Main navigation">${nav.map(([key,i,label])=>`<button class="${page===key?'active':''}" data-page="${key}" ${page===key?'aria-current="page"':''}>${icon(i)} ${label}${key==='messages'&&(pending().length+(state.escalations?.length||0))?`<span class="nav-count">${pending().length+(state.escalations?.length||0)}</span>`:''}</button>`).join('')}</nav><div class="sidebar-bottom"><button class="quiet ${page === "settings" ? "active" : ""}" data-page="settings" ${page === "settings" ? 'aria-current="page"' : ""}>${icon('settings')} Settings</button><div class="profile"><span class="avatar">${mode==='demo'?'SC':'AD'}</span><div>${mode==='demo'?'Sample coordinator':'Signed-in admin'}<small>Administrator</small></div></div><button class="quiet small" data-action="logout">${mode==='demo'?'Exit preview':'Sign out'}</button></div></aside><div class="workspace"><header class="topbar"><div class="topbar-left">${icon('home')}<span class="workspace-name">Coordinator workspace</span><span class="breadcrumb">${esc(pageNames[page])}</span></div><div class="topbar-right"><button class="quiet small mobile-settings" data-page="settings">Settings</button><button class="quiet small mobile-exit" data-action="logout">${mode === "demo" ? "Exit demo" : "Sign out"}</button>${mode==='live'&&config.humanConfirmationRequired?pill('Competition review','amber'):''}<span class="status ${mode === "demo" ? "preview-status" : !config.aiReady || !transportConnected || !config.automationEnabled ? "paused-status" : ""}"><span class="dot"></span>${status}</span></div></header>${mode === "demo" ? '<div class="demo-banner"><strong>Demo workspace</strong><span>Explore with sample data. Changes stay in this browser; no real texts are sent.</span></div>' : ""}<main id="main-content" class="content" tabindex="-1">${title()}${content()}<p class="footer-note">${mode==='demo'?'Synthetic test data. No real text deliveries.':'Your volunteers, shifts and messages. Together.'}</p></main></div></div>`;
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
  const needed = state.shifts.reduce((n, shift) => n + shift.required, 0);
  const covered = state.shifts.reduce((n, shift) => n + Math.min(shift.required, state.assignments.filter(a => a.shift_id === shift.id).length), 0);
  const open = Math.max(0, needed - covered);
  const coverage = summary([[state.shifts.length, "Upcoming shifts", "On your current schedule"], [`${covered} / ${needed}`, "Roles covered", "Confirmed assignments", "positive"], [open, "Open roles", open ? "Still need a volunteer" : "Every role is covered", open ? "attention" : "positive"]], "Schedule coverage");
  const demoControls = mode === "demo" ? `<details class="panel sample-booking section"><summary>Try a sample booking<span>Book or cancel a fictional assignment</span></summary><div class="settings-panel"><h2>Try a sample booking</h2><p>Manual simulation only. Review the sample volunteer’s availability yourself. No automatic replacement search, live AI, or texts run here.</p><form id="demo-booking-form"><label for="demo-shift">Sample shift</label><select id="demo-shift" name="shift">${state.shifts.map(s=>`<option value="${esc(s.id)}">${esc(s.role)} · ${date(s.starts_at)}</option>`).join('')}</select><label for="demo-volunteer">Sample volunteer</label><select id="demo-volunteer" name="volunteer">${state.volunteers.map(v=>`<option value="${esc(v.id)}">${esc(v.first_name+' '+v.last_name)} · ${esc(v.ministry)}${v.qualified?' · qualified':''}</option>`).join('')}</select><label for="demo-action">Action</label><select id="demo-action" name="action"><option value="book">Book selected volunteer</option><option value="cancel">Cancel selected booking</option></select><p class="error" role="alert"></p><button class="primary section">Apply sample booking</button></form></div></details>` : '';
  return `${coverage}${adminNotifications.panel()}${planningWorkflows.panel()}<section class="panel table-wrap" tabindex="0" role="region" aria-label="Shift schedule, scroll horizontally"><table><thead><tr><th>Role</th><th>Ministry</th><th>When</th><th>Coverage</th><th>Serving</th></tr></thead><tbody>${scheduleRows() || '<tr><td colspan="5" class="empty">No shifts to show yet. Check your connected church schedule or return after a schedule is added.</td></tr>'}</tbody></table></section>${demoControls}${replacementProgress()}<p class="notice section">${mode === "demo" ? "Sample cancellations reopen only the selected slot. Book a qualified sample replacement manually to update coverage. Automatic batches and text delivery are not simulated by this screen." : "Cancellations reopen the slot. Eligible replies update the calendar automatically, subject to consent, qualifications and role rules."}</p>`;
}
function approval(p) {
  if (["collect_availability", "confirm_collection"].includes(p.intent)) return `<article class="approval"><div class="approval-body"><h3>Availability collection needs a scope review</h3><p>Review the month and recipients on Schedule. Collection approval and individual text approval are separate decisions.</p><button data-page="schedule">Review collection scope</button></div></article>`;
  const v = state.volunteers.find((v) => v.phone === p.phone),
    m =
      state.messages.find((m) => m.id === p.message_id) ||
      state.messages.find(
        (m) => m.phone === p.phone && m.direction === "inbound",
      );
  const requires = p.intent === "cancel" && !p.shift_id;
  return `<article class="approval ${p.intent === "care" ? "care" : ""}"><div class="approval-icon">${icon(p.intent === "care" ? "heart" : "chat")}</div><div class="approval-body"><div class="approval-top"><h3>${esc(p.summary)}</h3>${pill(p.intent === "care" ? "Human follow-up" : p.confidence < 0.7 ? "Needs clarification" : "Ready to review", p.intent === "care" ? "amber" : p.confidence < 0.7 ? "gray" : "purple")}</div><p>${esc(name(p.phone))} · ${p.provider ? esc(p.provider) : "Sample rules"}${p.shift_id ? " · " + esc(state.shifts.find((s) => s.id === p.shift_id)?.role || "Assigned shift") : ""}</p>${m ? `<div class="quote">“${esc(m.body)}”</div>` : ""}${p.confirmation_required ? `<p><strong>Recipient:</strong> ${esc(p.phone || "Record change")}</p><p><strong>Reason:</strong> ${esc(p.reason)}</p><p><strong>Expires:</strong> ${esc(p.expires_at)}</p>` : ""}${p.reply ? `<div class="quote">Exact text: ${esc(p.reply)}</div>` : ""}${p.record_change ? `<p>Record: ${esc(p.record_change.record)}</p><pre>Before: ${esc(JSON.stringify(p.record_change.before))}\nAfter: ${esc(JSON.stringify(p.record_change.after))}</pre>` : ""}${requires ? '<p class="error">Shift is unclear. Reject this proposal and clarify with the volunteer before cancelling.</p>' : ""}<div class="approval-actions"><button class="primary small" data-approve="${esc(p.id)}" ${requires || mode === "demo" ? "disabled" : ""}>${icon("check")}${p.confirmation_required ? (p.record_change ? "Approve exact change" : "Approve exact text") : p.intent === "care" ? "Mark reviewed" : p.intent === "accept" ? "Review offer" : "Approve action"}</button><button class="quiet small" data-reject="${esc(p.id)}">Dismiss</button></div>${p.intent === "care" ? '<p class="muted">Coordinator follow-up required. No pastor is contacted automatically.</p>' : ""}${p.intent === "accept" ? '<p class="muted">This confirms review of the offer. No assignment is made.</p>' : ""}</div></article>`;
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
  return `<section class="section panel settings-panel"><h2>A considerate texting rhythm</h2><p>Recorded placements can receive a Scheduled notice and one Day-before reminder. Volunteers are not asked to confirm the placement by text.</p><p>Signup preferences are saved quietly. Routine texts follow the saved quiet hours, consent and role rules. Gloo prepares messages; a hold or disconnected laptop prevents sending.</p><p>Admin updates summarize coverage, open roles and your next step three hours before each event. Check the status above for holds, queued texts and delivery evidence.</p></section>`;
}

function overview() {
  const reviews = pending().length, care = state.escalations?.length || 0;
  const fallback = new Map();
  for (const shift of state.shifts) {
    const key = shift.title + shift.starts_at;
    if (!fallback.has(key)) fallback.set(key, {title:shift.title, starts_at:shift.starts_at, covered:0, required:0, gaps:[]});
    const event = fallback.get(key), covered = Math.min(shift.required,state.assignments.filter(a=>a.shift_id===shift.id).length);
    event.covered += covered; event.required += shift.required;
    if (covered < shift.required) event.gaps.push({role:shift.role,open:shift.required-covered});
  }
  const events = [...(state.staffing?.length ? state.staffing : fallback.values())].sort((a,b)=>new Date(a.starts_at)-new Date(b.starts_at));
  const gaps = events.reduce((n,event)=>n+Math.max(0,event.required-event.covered),0);
  const stalled = (state.fills || []).filter(f=>f.state==='escalated').length;
  const searching = (state.fills || []).filter(f=>['open','in_progress','waiting_quiet'].includes(f.state)).length;
  const plansMissing = events.some(event=>!event.required);
  const title = plansMissing ? 'An event needs a staffing plan.' : reviews+care+stalled ? 'A few things need you.' : gaps ? 'We’ve got some gaps to cover.' : events.length ? 'Your upcoming events are covered.' : 'Let’s get your first event ready.';
  const description = plansMissing ? 'Coverage can’t be confirmed until the required roles are saved. Review the event in Shifts.' : reviews+care+stalled ? 'Start with the decisions below. The schedule shows the remaining open roles.' : gaps ? (mode==='demo'?'Explore the sample shifts and book a qualified replacement.':searching?`${searching} replacement search${searching===1?' is':'es are'} running. You’ll see any decisions here.`:'Open the shifts to check coverage. No replacement search is running yet.') : events.length ? 'Check your next event below and keep an eye on new replies.' : 'Finish your church setup, prepare your people, and connect your church schedule.';
  const updates = mode==='demo'?'Texting is disconnected. Open the connected admin console for real delivery.':adminTexts?.ready?`Admin updates are ready for ${adminTexts.phone}.`:adminTexts?.enabled?'Your mobile is saved, but updates need attention.':'Add your mobile number and turn on admin updates.';
  return `<section class="admin-brief"><div><h2>${title}</h2><p>${description}</p><div class="setup-actions"><button class="primary" data-page="${reviews+care?'messages':'schedule'}">${reviews+care?'Review what needs me':'View shifts'}</button><button data-page="volunteers">Manage volunteers</button></div></div><dl class="brief-counts"><div><dt>Open spots</dt><dd>${gaps}</dd></div><div><dt>Decisions waiting</dt><dd>${reviews}</dd></div><div><dt>Human follow-ups</dt><dd>${care}</dd></div></dl></section><section class="admin-update-strip"><div>${icon('chat')}<div><strong>${esc(updates)}</strong><p>${mode==='demo'?'Explore text updates in Settings.':adminTexts?.ready?`Your event summary runs ${adminTexts.pre_event_hours} hours before each event${adminTexts.review_required?', with exact review before delivery':''}.`:esc(adminTextsError || adminTexts?.issues?.[0] || 'Get coverage summaries and a clear next step before each event.')}</p></div></div><button data-page="settings">${adminTexts?.ready?'Manage updates':'Set up my texts'}</button></section><div class="admin-home-grid"><section><div class="section-heading"><h2>Coming up</h2><button class="quiet small" data-page="schedule">All shifts</button></div><div class="panel event-list">${events.slice(0,4).map(event=>`<article class="event-summary"><div class="event-when"><strong>${date(event.starts_at)}</strong><span>${time(event.starts_at)}</span></div><div><div class="event-heading"><h3>${esc(event.title)}</h3>${pill(!event.required?'Plan needed':event.covered>=event.required?'Covered':'Needs cover',!event.required||event.covered<event.required?'amber':'green')}</div><p>${event.required?`${event.covered} of ${event.required} required spots covered`:'No required staffing plan is saved.'}</p>${event.gaps.length?`<p class="event-gaps">${event.gaps.map(g=>`${esc(g.role)}: ${g.open} open`).join(' · ')}</p>`:''}<button class="quiet small" data-page="schedule">Review event coverage</button></div></article>`).join('') || '<div class="empty"><h3>No upcoming events yet.</h3><p>Connect your church schedule to see staffing here.</p><button data-page="settings">Check connections</button></div>'}</div></section><section><div class="section-heading"><h2>What needs me?</h2>${pill(reviews+care+stalled?'Needs attention':'Caught up',reviews+care+stalled?'amber':'green')}</div><div class="panel attention-list">${reviews?`<article class="insight"><h3>${reviews} decision${reviews===1?'':'s'} waiting</h3><p>Review the exact action or text before it goes ahead.</p><button data-page="messages">Review decisions</button></article>`:''}${care?`<article class="insight"><h3>${care} human follow-up${care===1?'':'s'}</h3><p>Scheduling issues and personal concerns need a person. Open Messages for the next step.</p><button data-page="messages">View follow-ups</button></article>`:''}${stalled?`<article class="insight"><h3>${stalled} replacement search${stalled===1?'':'es'} need help</h3><p>The automated search needs your next step.</p><button data-page="schedule">Review open roles</button></article>`:''}${!reviews&&!care&&!stalled?'<div class="empty"><h3>No decisions waiting.</h3><p>New approvals and personal follow-ups will appear here.</p><button class="quiet" data-page="messages">View messages</button></div>':''}</div><div class="panel home-note"><h3>Less checking. Clearer updates.</h3><p>Your admin texts include the event, coverage, open roles and a next step. An all-set update tells you when no action is needed.</p><button class="quiet small" data-page="settings">Choose my mobile number</button></div></section></div>`;
}

function volunteers() {
  const list = state.volunteers.filter(
    (v) =>
      `${v.first_name} ${v.last_name} ${v.phone}`
        .toLowerCase()
        .includes(filter.toLowerCase()) &&
      (ministry === "all" || v.ministry === ministry),
  );
  const total = state.volunteers.length;
  const ready = state.volunteers.filter(v => v.status === "active" && v.consent).length;
  const review = state.volunteers.filter(v => !v.qualified).length;
  return `${summary([[total, "Volunteers", "Across your ministry teams"], [ready, "Ready for texts", "Active with consent recorded", "positive"], [review, "Needs clearance", "Review before assigning a role", review ? "attention" : ""]], "Volunteer summary")}<div class="toolbar"><input id="search" aria-label="Search volunteers" type="search" placeholder="Search by name or phone" value="${esc(filter)}"><select id="ministry-filter" aria-label="Filter by ministry"><option value="all">All ministries</option>${[...new Set(state.volunteers.map((v) => v.ministry))].map((m) => `<option ${m === ministry ? "selected" : ""}>${esc(m)}</option>`).join("")}</select><span class="result-count" role="status">${list.length} of ${total} volunteers</span></div><div class="panel table-wrap" tabindex="0" role="region" aria-label="Volunteer roster, scroll horizontally"><table><thead><tr><th>Volunteer</th><th>Ministry</th><th>Availability</th><th>Clearance</th><th>Status</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>${list.map((v) => `<tr><td><div class="person"><span class="avatar">${initials(v)}</span><div><strong>${esc(v.first_name)} ${esc(v.last_name)}</strong><small>${esc(v.phone)}</small></div></div></td><td>${esc(v.ministry)}</td><td>${esc(v.availability)}</td><td>${pill(v.qualified ? "Coordinator cleared" : "Needs review", v.qualified ? "green" : "amber")}${v.background_check_until ? `<small>Check until ${esc(v.background_check_until)}</small>` : ""}</td><td>${pill(v.status, v.status === "active" ? "green" : "gray")}<small>${v.consent ? "Text consent recorded" : "No text consent"}</small>${v.onboarding_stage && v.onboarding_stage !== "complete" ? `<small>Setup: ${esc(v.onboarding_stage)}</small>` : ""}</td><td>${mode === "live" && v.can_start_text_setup ? `<button class="quiet small" data-text-setup="${esc(v.id)}">${v.onboarding_stage === "not_started" ? "Start text setup" : "Restart text setup"}</button>` : ""}<button class="quiet small" data-edit="${esc(v.id)}">Edit</button></td></tr>`).join("") || '<tr><td colspan="6" class="empty">No volunteers match your search.<p>Try another name or choose All ministries.</p></td></tr>'}</tbody></table></div>`;
}
function adminComposer() {
  if(mode !== 'live') return '';
  const available = config.adminReplyAvailable;
  const eligible = state.volunteers.some(v=>v.consent && v.status==='active');
  return `<section class="panel composer section"><h2>Write a volunteer text</h2><p class="muted">${config.humanConfirmationRequired?'Your exact words are held for competition review.':'Choose a consenting volunteer. We’ll queue your text through the church connection.'}</p>${!available?'<p class="notice">Texting is awaiting a backend update.</p>':''}<form id="admin-reply-form"><label for="reply-recipient">Volunteer</label><select id="reply-recipient" name="volunteer_id" required><option value="">Choose a volunteer</option>${state.volunteers.map(v=>`<option value="${esc(v.id)}" ${String(v.id)===replyRecipient?'selected':''} ${!v.consent||v.status!=='active'?'disabled':''}>${esc(v.first_name+' '+v.last_name)}${!v.consent||v.status!=='active'?' · texting unavailable':''}</option>`).join('')}</select><label for="reply-body">Your text</label><textarea id="reply-body" name="body" required maxlength="1600" placeholder="Write your message.">${esc(replyBody)}</textarea><p class="error" role="alert"></p>${replyStatus?`<p role="status">${esc(replyStatus)}</p>`:''}<button class="primary section" ${!available||!eligible?'disabled':''}>${config.humanConfirmationRequired?'Create text for review':'Queue text'}</button></form></section>`;
}
function escalationCard(review) {
  const context = review.category === 'cancellation_scope' ? review.internal_review : null;
  if (!context) return `<article class="insight"><h3>Human follow-up · ${esc(review.severity)}</h3><p>${esc(review.summary)}</p></article>`;
  return `<article class="insight"><h3>Cancellation review · ${esc(context.recipient_name)}</h3><p class="notice">Internal review only. This review does not queue a volunteer text.</p>${context.scope_changed ? '<p class="notice">The original booking scope changed. The list below shows current bookings.</p>' : ''}<h4>Current bookings</h4><ul>${context.bookings.map(booking => `<li><strong>${esc(booking.role)}</strong> · ${esc(booking.event_title)} · ${date(booking.starts_at)} at ${time(booking.starts_at)} · ${esc({proposed:'Proposed',approved:'Approved',confirmed:'Confirmed'}[booking.status] || 'Status needs review')}</li>`).join('') || '<li>No upcoming bookings remain.</li>'}</ul><p><strong>Next step:</strong> ${esc(context.next_step)}</p><div class="setup-actions section"><button class="quiet" data-page="schedule">Review Shifts</button></div></article>`;
}
function messages() {
  const history=[...state.messages].reverse();
  return `${lastReviewOutcome?`<p class="notice" role="status">${esc(lastReviewOutcome)}</p>`:""}${adminComposer()}${state.escalations?.length?`<section class="panel section"><h2>Needs a person</h2>${state.escalations.map(escalationCard).join('')}</section>`:''}${pending().length?`<section class="section"><h2>Needs your review</h2><div class="panel">${pending().map(approval).join('')}</div></section>`:''}<section class="section"><h2>Conversation history</h2><div class="panel thread">${history.map(m=>`<div class="message ${esc(m.direction)}"><div class="bubble">${esc(m.body)}</div><small>${esc(name(m.phone))} · ${esc(["in", "inbound"].includes(m.direction) ? "Received" : deliveryLabel(m.status))}</small></div>`).join('')||'<div class="empty">No volunteer messages yet.</div>'}</div></section>`;
}
function settings() {
  const details = churchSetup.details();
  const address = [details.address, [details.city, details.region, details.postal_code].filter(Boolean).join(", ")].filter(Boolean).join(" · ");
  return `<div class="settings-grid"><section class="panel settings-panel church-profile"><div class="section-heading"><h2>Your church</h2>${pill(churchSetup.completed() ? "Profile saved" : "Setup available", churchSetup.completed() ? "green" : "gray")}</div><p class="church-profile-name">${esc(details.church_name || (mode === "demo" ? "Cedar Hills Community Church" : "Add your church details"))}</p><dl class="profile-details"><div><dt>Address</dt><dd>${esc(address || "Not added yet")}</dd></div><div><dt>Coordinator</dt><dd>${esc(details.coordinator_name || "Not added yet")}</dd></div><div><dt>Timezone</dt><dd>${esc(details.timezone || "America/Denver")}</dd></div></dl><button data-page="setup">Edit church details ${icon("arrow")}</button></section>${adminTextPanel()}</div>${`<section class="panel settings-panel section settings-connection"><div class="section-heading"><h2>Texting & scheduling</h2>${pill(mode === "demo" ? "Preview only" : config.messagingTransport === "google_voice" ? (cloudTexting.summary()?.label || "Cloud connection not checked") : config.macBridgeConnected ? "Messages online" : "Messages offline", mode !== "demo" && (config.messagingTransport === "google_voice" ? cloudTexting.summary()?.connected : config.macBridgeConnected) ? "green" : "amber")}</div><p>${mode === "demo" ? "Preview only. No texts are sent." : config.messagingTransport === "google_voice" ? cloudTexting.summary()?.continuousSignup ? "Cloud signup responds through Gloo for registered participants. Your laptop can be off. Quiet hours, consent and connection holds still apply." : cloudTexting.summary()?.demoMode ? "Google Voice demo steps use the verified cloud sender. Exact reviewed texts can be submitted through the private connection." : "Google Voice texting is held. Verify the dedicated cloud sender and its authorized mode before delivery." : config.macBridgeConnected ? "The laptop Messages connection is online." : "The laptop Messages connection is offline."}</p><dl class="profile-details"><div><dt>Gloo AI</dt><dd>${config.aiReady ? "Connected" : "Disconnected"}</dd></div><div><dt>Scheduling</dt><dd>${mode === "demo" ? "Sample rules only. No background scheduling or real delivery." : config.humanConfirmationRequired ? cloudTexting.summary()?.continuousSignup ? "Signup replies use your recorded conversation authorization. Manual and scheduled texts still require exact review." : "Optional competition mode: exact review required." : "Routine updates follow your role rules automatically."}</dd></div><div><dt>Background scheduling</dt><dd>${mode === "demo" ? "Preview only" : config.automationEnabled ? "Running" : "Paused"}</dd></div><div><dt>Consent and care</dt><dd>STOP, text consent, qualifications and human care follow-up stay enforced.</dd></div></dl><p class="field-hint">Church preferences are saved with your profile. Updating the live connection and scheduling rules requires the owner’s configuration review.</p></section>`}${cloudTexting.screen()}${planningCenterReview.panel()}`;
}

function volunteerModal(v) {
  modal.innerHTML = `<div class="modal-heading"><h2 id="volunteer-dialog-title">${v ? "Edit volunteer" : "Add a volunteer"}</h2><button class="quiet small" data-close aria-label="Close dialog">✕</button></div><form id="volunteer-form" data-id="${v?.id || ""}"><div class="form-row"><div><label for="first_name">First name</label><input id="first_name" name="first_name" autofocus value="${esc(v?.first_name)}" required maxlength="80"></div><div><label for="last_name">Last name</label><input id="last_name" name="last_name" value="${esc(v?.last_name)}" required maxlength="80"></div></div><label for="phone">Phone number</label><input id="phone" name="phone" type="tel" placeholder="+13035550123" value="${esc(v?.phone)}" required ${v ? "readonly" : ""}><label for="ministry">Preferred ministry</label><select id="ministry" name="ministry">${["Welcome", "Kids", "Food pantry", "Production", "Care", "Youth"].map((m) => `<option ${v?.ministry === m ? "selected" : ""}>${m}</option>`).join("")}</select>${v ? `<label for="availability">Availability</label><input id="availability" name="availability" value="${esc(v.availability)}" maxlength="500"><label for="status">Status</label><select id="status" name="status">${["active", "pending", "paused", "opted_out"].map((s) => `<option ${v.status === s ? "selected" : ""}>${s}</option>`).join("")}</select>${mode === "demo" ? `<label class="check"><input name="qualified" type="checkbox" ${v.qualified ? "checked" : ""}>Sample clearance (preview only)</label>` : ""}<label for="background">Background check valid through</label><input id="background" name="background_check_until" type="date" value="${esc(v.background_check_until)}">` : ""}<label class="check"><input name="consent" type="checkbox" ${v?.consent ? "checked" : ""}>I have verified this person agreed to receive scheduling texts.</label><p class="muted">Adding a phone never grants an administrator login.</p><p id="volunteer-error" class="error" role="alert" tabindex="-1"></p><div class="modal-actions"><button type="button" data-close>Cancel</button><button class="primary">Save volunteer</button></div></form>`;
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
    "Individual qualifications remain managed in the existing coordinator application. Text Monkey cannot grant blanket clearance.";
  modal.querySelector("form").append(note);
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.auth) {
      authView = b.dataset.auth;
      login();
      focusView();
    }
    if (b.dataset.page) {
      churchSetup.collect();
      if (mode === "live" && ["settings", "overview"].includes(b.dataset.page)) await loadAdminTexts();
      if (b.dataset.page === "schedule") { await planningWorkflows.load(); await adminNotifications.load(); }
      if (b.dataset.page === "settings") await cloudTexting.load();
      page = b.dataset.page;
      render();
      focusView();
      globalThis.scrollTo?.(0, 0);
    }
    if (b.hasAttribute?.('data-pco-load')) { await planningCenterReview.load(); return; }
    if (b.dataset.pcoRecord) { await planningCenterReview.record(b.dataset.pcoRecord); return; }
    if (b.dataset.cloudAction) { await cloudTexting.action(b.dataset.cloudAction); return; }
    if (b.hasAttribute?.("data-notification-refresh")) await adminNotifications.refresh();
    if (b.hasAttribute?.("data-notification-more")) await adminNotifications.more();
    if (b.hasAttribute?.("data-planning-refresh")) { await planningWorkflows.load(); render(); }
    if (b.dataset.planningDecision) await planningWorkflows.decide(b.dataset.planningId, b.dataset.planningHash, b.dataset.planningDecision);
    if (b.dataset.action === "demo") {
      mode = "demo";
      try { state = JSON.parse(localStorage.getItem(storeKey)) || seed(); } catch { state = seed(); }
      adminTexts=state.adminTexts || null; adminTextsError='';
      await churchSetup.load();
      page = config.publicDemo ? "overview" : churchSetup.completed() ? "overview" : "setup";
      render();
      focusView();
      globalThis.scrollTo?.(0, 0);
    }
    if (b.dataset.action === "logout") {
      planningWorkflows.reset(); adminNotifications.reset(); lastReviewOutcome = "";
      cloudTexting.reset();
      replyRecipient = replyBody = replyStatus = "";
      if (mode === "live" && token) await api("/api/logout", {});
      rememberSession(null);
      state = seed();
      await churchSetup.load();
      authView = "login";
      login();
      focusView();
      globalThis.scrollTo?.(0, 0);
    }
    if (b.dataset.action === "reset") {
      state = seed();
      churchSetup.reset();
      adminTexts=null; adminTextsError='';
      persist();
      render();
      toast("Synthetic demo reset.");
    }
    if (b.dataset.action === "send-admin-check") {
      b.disabled = true;
      if (mode !== 'live' || !token) throw new Error('Open the connected admin console to send a real text.');
      const result = await api('/api/setup/admin-texts/send-check', {request_id: adminCheckRequestId ||= crypto.randomUUID()});
      await loadAdminTexts();
      if (!['queued_for_mac','queued_for_google_voice'].includes(result.delivery)) { render(); throw new Error(result.retry_at ? `The text is waiting. Retry this same check after ${time(result.retry_at)}; quiet hours and Gloo recovery still apply.` : 'The text was not queued. Check the connection status.'); }
      adminCheckRequestId = ''; render();
      toast('Connection check queued to your saved mobile. Delivery status appears below.');
    }
    if (b.dataset.action === "reload-admin-texts") { await loadAdminTexts(); render(); }
    if (b.dataset.action === "focus-admin-mobile") { document.querySelector("#admin-mobile")?.focus(); }
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
    if (b.dataset.approve || b.dataset.reject) {
      b.disabled = true;
      const pid = b.dataset.approve || b.dataset.reject,
        p = state.proposals.find((p) => p.id === pid);
      if (mode === "demo") {
        if (b.dataset.approve) throw new Error('Open the connected admin console to approve a real action.');
        else p.status = "rejected";
      } else {
        const result = await api(
          `/api/proposals/${pid}/${b.dataset.approve ? "approve" : "reject"}`,
          p.confirmation_required ? { content_hash: p.content_hash } : {},
        );
        lastReviewOutcome = reviewOutcomeLabel(result);
      }
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
      toast("Text queued. Check conversation history for delivery.");
    }
  } catch (error) {
    b.disabled = false;
    toast(error.message);
  }
});
document.addEventListener("change", (e) => {
  if (e.target.id === "pco-review-volunteer") planningCenterReview.select(e.target.value);
  if (e.target.id === "reply-recipient") { replyRecipient = e.target.value; replyRequestId = ""; }
  if (e.target.id === "ministry-filter") {
    ministry = e.target.value;
    render();
  }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "planning-month") planningWorkflows.setMonth(e.target.value);
  if (e.target.id === "reply-body") { replyBody = e.target.value; replyRequestId = ""; }
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
  const f = e.target;
  if (["cloud-session-form", "cloud-demo-recipient-form", "cloud-demo-compose-form", "cloud-demo-window-form"].includes(f.id)) { await cloudTexting.submit(f); return; }
  const data = Object.fromEntries(new FormData(f)),
    b = f.querySelector("button.primary");
  if (b) b.disabled = true;
  try {
    if (f.id === "planning-month-form") { await planningWorkflows.request(data.month); return; }
    if (f.id === "admin-text-form") {
      if (mode !== 'live') throw new Error('Open the connected admin console to save your mobile number.');
      if (!token) throw new Error('Sign in before enabling admin updates.');
      if (adminTextsSaving) return;
      adminTextsSaving = true;
      try {
        const enabled = e.submitter?.value !== 'pause';
        adminTexts = await api('/api/setup/admin-texts', {phone:data.phone, enabled, consent:enabled && data.consent === 'on'});
        adminTextsError = '';
        adminCheckRequestId = '';
        await churchSetup.load();
        adminTextsSaving = false;
        render();
        toast(enabled ? (adminTexts.ready?'Admin text updates saved.':'Mobile saved. Check the connection steps above.') : 'Your admin text updates are paused.');
      } finally { adminTextsSaving = false; }
    }
    if (f.id === "admin-reply-form") {
      if (mode !== "live" || !token || !config.adminReplyAvailable)
        throw new Error("Sign in before writing a volunteer text.");
      const recipient = state.volunteers.find(v => String(v.id) === data.volunteer_id);
      if (!recipient || !recipient.consent || recipient.status !== "active")
        throw new Error("Choose an active roster volunteer with text consent.");
      if (!data.body?.trim() || data.body.length > 1600)
        throw new Error("Enter a text of 1–1,600 characters.");
      const payload = {volunteer_id:Number(recipient.id), body:data.body};
      if(!config.humanConfirmationRequired) payload.request_id = replyRequestId ||= crypto.randomUUID();
      const result = await api("/api/reply", payload);
      if (config.humanConfirmationRequired ? result.delivery !== "awaiting_confirmation" || !result.approval_id : !["queued_for_mac","queued_for_google_voice","simulated"].includes(result.delivery) || !result.message_id)
        throw new Error("The backend did not confirm this text. Check Messages before trying again.");
      replyRecipient = String(recipient.id);
      replyBody = "";
      replyStatus = result.delivery === "awaiting_confirmation" ? "Text is held for exact review. Nothing has been sent." : ["queued_for_mac","queued_for_google_voice"].includes(result.delivery) ? "Text queued." : "Preview recorded. No text delivered.";
      replyRequestId = "";
      await refresh();
      toast(replyStatus);
    }
    if (f.id === "demo-booking-form" && mode === "demo") {
      const outcome = demoBooking(state, data.shift, data.volunteer, data.action);
      await refresh();
      toast(outcome);
    }
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
      const payload = authView === "register" ? {email:data.email, password:data.password, church_details:registrationDetails(data)} : data;
      const result = await api(`/api/${route}`, payload);
      f.reset();
      if (authView === "login") {
        token = result.access_token;
        mode = "live";
        await openCoordinatorWorkspace();
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
          qualified: mode === "demo" ? f.elements.qualified.checked : undefined,
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
      document.querySelector(f.dataset.id ? `[data-edit="${f.dataset.id}"]` : '[data-action="add"]')?.focus?.();
      toast("Volunteer saved.");
    }
  } catch (error) {
    const output = f.querySelector(".error");
    if (output) {
      output.textContent = error.message;
      output.setAttribute?.("tabindex", "-1");
      output.focus?.({preventScroll:true});
      output.scrollIntoView?.({block:"nearest"});
    }
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
      await openCoordinatorWorkspace();
      rememberSession(token);
      toast("Email confirmed. Welcome to Text Monkey.");
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
    await openCoordinatorWorkspace();
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
