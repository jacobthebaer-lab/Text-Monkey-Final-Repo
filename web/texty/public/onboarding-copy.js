export const COPY_LABELS = {
  interests: 'Role selection',
  availability: 'Availability question',
  clarification: 'Availability follow-up / clarification',
  completion: 'Preferences saved confirmation',
};
const DEMO_KEY = 'textmonkey.onboarding-copy.demo.v1';
const SESSION_KEY = 'texty.coordinator.session.v1';
const esc = text => String(text ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

export function renderCopy(text, preview = {}) {
  return String(text).replaceAll('{first_name}', preview.first_name || 'Alex')
    .replaceAll('{roles}', preview.roles || '1: Greeter, 2: Usher, 3: Production');
}

export function createCopyDraft(snapshot) {
  let saved = {...snapshot.messages};
  let draft = {...saved};
  let revision = snapshot.revision;
  let defaults = {...snapshot.defaults};
  return {
    messages: () => ({...draft}),
    revision: () => revision,
    dirty: () => JSON.stringify(draft) !== JSON.stringify(saved),
    edit(field, text) {
      if (!Object.hasOwn(COPY_LABELS, field)) throw new Error('Unknown message field.');
      draft[field] = text;
    },
    reset() { draft = {...defaults}; },
    replace(next) {
      saved = {...next.messages}; draft = {...saved}; revision = next.revision;
      defaults = {...next.defaults};
    },
    async save(write) {
      const result = await write({messages: {...draft}, revision});
      this.replace(result);
      return result;
    },
  };
}

export function mountCopyEditor(root, {request, demo = false, storage = null}) {
  const fields = root.querySelector('#copy-fields');
  const preview = root.querySelector('#copy-preview');
  const status = root.querySelector('#copy-status');
  const error = root.querySelector('#copy-error');
  const form = root.querySelector('#copy-form');
  const reset = root.querySelector('#copy-reset');
  const reload = root.querySelector('#copy-reload');
  const save = root.querySelector('#copy-save');
  let model, sample, defaults, busy = true;
  const controls = () => [...fields.querySelectorAll('textarea'), reset, reload, save];
  const setBusy = value => { busy = value; controls().forEach(control => {control.disabled = value;}); };
  const drawPreview = () => {
    preview.innerHTML = Object.entries(model.messages()).map(([key, text]) =>
      `<article><h3>${esc(COPY_LABELS[key])}</h3><p>${esc(renderCopy(text, sample))}</p></article>`).join('');
  };
  const draw = () => {
    fields.innerHTML = Object.entries(model.messages()).map(([key, text]) =>
      `<label for="copy-${key}">${esc(COPY_LABELS[key])}</label><textarea id="copy-${key}" name="${key}" rows="4" maxlength="600" required>${esc(text)}</textarea>`).join('');
    drawPreview();
    setBusy(false);
  };
  const demoSnapshot = messages => ({messages, defaults, revision: 0,
    preview: {first_name: 'Alex', roles: '1: Greeter, 2: Usher, 3: Production'}});
  const load = async () => {
    defaults = await request('/onboarding-copy-defaults.json');
    if (!demo) return request('/api/setup/onboarding-copy');
    let messages = {...defaults};
    try {
      const cached = JSON.parse(storage?.getItem(DEMO_KEY) || 'null');
      if (cached && Object.keys(defaults).every(key => typeof cached[key] === 'string' && cached[key].length <= 600)) {
        messages = Object.fromEntries(Object.keys(defaults).map(key => [key, cached[key]]));
      }
    } catch { /* A malformed local preview cannot overwrite canonical defaults. */ }
    return demoSnapshot(messages);
  };
  const write = async data => {
    if (!demo) return request('/api/setup/onboarding-copy', data);
    if (!storage) throw new Error('Browser storage is unavailable. Your preview cannot be saved here.');
    storage.setItem(DEMO_KEY, JSON.stringify(data.messages));
    return demoSnapshot(data.messages);
  };
  fields.addEventListener('input', event => {
    if (!model || busy || !Object.hasOwn(COPY_LABELS, event.target.name)) return;
    model.edit(event.target.name, event.target.value);
    error.textContent = '';
    status.textContent = model.dirty() ? 'Unsaved changes' : 'Saved copy';
    drawPreview();
  });
  reset.addEventListener('click', () => {
    if (busy || !model) return;
    model.reset(); draw(); error.textContent = '';
    status.textContent = 'Current defaults restored in the boxes. Save copy to keep this change.';
  });
  reload.addEventListener('click', async () => {
    if (busy) return;
    setBusy(true); error.textContent = '';
    try {
      const snapshot = await load();
      model.replace(snapshot); sample = snapshot.preview; draw(); status.textContent = 'Saved copy loaded';
    } catch (failure) { error.textContent = failure.message; setBusy(false); }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !model || !form.reportValidity()) return;
    setBusy(true); error.textContent = ''; status.textContent = 'Saving…';
    try {
      const result = await model.save(write);
      sample = result.preview; draw();
      status.textContent = demo ? 'Preview copy saved in this browser.' : 'Administrator copy draft saved.';
    } catch (failure) {
      error.textContent = failure.message; status.textContent = 'Your edits are still in the boxes.';
    } finally { setBusy(false); }
  });
  const ready = load().then(snapshot => {
    model = createCopyDraft(snapshot); sample = snapshot.preview; draw();
    status.textContent = snapshot.saved_at ? 'Saved administrator draft loaded' : 'Current copy loaded';
  }).catch(failure => { error.textContent = failure.message; });
  return {ready, model: () => model};
}

async function start() {
  const root = document.getElementById('onboarding-copy-editor');
  if (!root) return;
  let token = null, storage = null;
  try { token = sessionStorage.getItem(SESSION_KEY); } catch { /* Sign-in remains required. */ }
  try { storage = localStorage; } catch { /* Preview remains editable without persistence. */ }
  const request = async (path, data) => {
    const headers = {};
    if (path.startsWith('/api/setup/') && token) headers.Authorization = 'Bearer ' + token;
    if (data) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, {method: data ? 'POST' : 'GET', headers, body: data ? JSON.stringify(data) : undefined});
    let result;
    try { result = await response.json(); } catch { throw new Error('Could not load the text settings. Reload or sign in again.'); }
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not save text settings.');
    return result;
  };
  try {
    const config = await request('/api/config');
    const demo = Boolean(config.publicDemo || !config.connected);
    root.querySelector('#copy-back').href = demo ? '/' : '/texty';
    if (!demo && !token) {
      root.querySelector('#copy-scope').textContent = 'Sign in to Text Monkey, then open these settings again.';
      return;
    }
    root.querySelector('#copy-scope').textContent = demo
      ? 'Fictional preview. Save keeps this copy in your browser. No texts are sent.'
      : 'Saved for your signed-in administrator account as draft copy. Saving and previewing do not send texts.';
    mountCopyEditor(root, {request, demo, storage});
  } catch (failure) { root.querySelector('#copy-error').textContent = failure.message; }
}

if (typeof document !== 'undefined') start();
