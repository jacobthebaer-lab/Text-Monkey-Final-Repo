import {createCloudTexting} from '/cloud-texting.js';

let superadmin = true, draft = false, approved = false;
const fresh = () => ({provider:'google_voice',enabled:false,live_enabled:false,paused:true,state:'disabled',gloo_ready:false,test_recipients:0,connection:{connected:false,state:'disabled'},queue:{queued:0},held_inbound:{held_gloo:0}});
let status = fresh();
const notice = message => {document.querySelector('#preview-status').textContent = message;};
const ui = createCloudTexting({
  disconnectedPreview:true, getMode:()=> 'demo', getToken:()=> 'local-preview-role',
  getConfig:()=> ({cloudTextingAvailable:true}), render,
  // In-memory fixtures only. This module never uses fetch or any transport.
  api:async(path,body)=> {
    if (path === '/api/auth/me') return {superadmin};
    if (!superadmin) throw Object.assign(new Error('Preview role has no connection controls'),{status:403});
    if (path === '/api/cloud-texting/pause') {
      status.paused = body.paused;
      notice(status.paused ? 'Sample queue paused. Nothing is connected.' : 'Sample queue resumed. Google Voice remains disconnected, so nothing is sent.');
    } else if (path !== '/api/cloud-texting') throw new Error('This preview accepts no credentials or live requests.');
    return structuredClone(status);
  },
});
function render() {
  document.querySelector('#cloud-controls').innerHTML = ui.screen() || '<section class="panel settings-panel section"><h2>Coordinator view</h2><p>Connection setup and transport controls are available only to superadmins. The real backend verifies the signed-in role on every request.</p><span class="pill amber">Disconnected preview</span></section>';
  document.querySelector('#preview-review').innerHTML = draft
    ? `<section class="section"><h3>Sample review</h3><p><strong>Fictional volunteer:</strong> Sample Volunteer</p><blockquote>Can you help with the welcome team this Sunday?</blockquote><p class="field-hint">Scripted preview text. Gloo did not generate this sample. Live messages must be composed by Gloo and reviewed under the existing rules.</p><button data-preview="approve" ${approved ? 'disabled' : ''}>${approved ? 'Sample added to held queue' : 'Approve sample only'}</button></section>`
    : '<p class="field-hint section">Load a sample draft to preview exact-message review. No phone number or real recipient is attached.</p>';
}
document.addEventListener('click',async event=> {
  const button=event.target.closest('button');
  if (!button || button.disabled) return;
  if (button.dataset.cloudAction) return ui.action(button.dataset.cloudAction);
  switch (button.dataset.preview) {
    case 'draft': draft=true;notice('A scripted sample is ready for review. No Gloo request was made.');break;
    case 'approve':
      if (!draft || approved) return;
      approved=true;status.queue.queued=1;
      notice('Sample approved into the local held queue. The disconnected connector cannot dispatch it.');break;
    case 'gloo':
      status.held_inbound.held_gloo=1;
      notice('Simulated Gloo outage: one sample incoming message is held. No fallback reply is generated.');break;
    case 'reset': status=fresh();draft=false;approved=false;notice('Preview reset. All sample state was cleared.');break;
    default:return;
  }
  await ui.load();render();
});
document.querySelector('#preview-role').addEventListener('change',async event=> {
  superadmin=event.target.value==='superadmin';ui.reset();await ui.load();render();
  notice(`${superadmin ? 'Superadmin' : 'Coordinator'} preview selected. This is a sample role, not a real login.`);
});
await ui.load();render();
