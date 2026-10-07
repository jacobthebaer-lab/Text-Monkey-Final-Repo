// Separate feature entry point: no app.js edits or access to account tokens.
const appRoot = document.getElementById('app');
if (appRoot && typeof MutationObserver !== 'undefined') {
  const attach = () => {
    const panel = appRoot.querySelector('.settings-connection');
    if (!panel || panel.querySelector('[data-onboarding-copy-link]')) return;
    const link = document.createElement('a');
    const appPath=!(location.hostname || '').endsWith('.pages.dev') && /^\/texty(?:\/|$)/.test(location.pathname) ? '/texty' : '/';
    link.href = '/onboarding-copy.html?return_to='+encodeURIComponent(appPath);
    link.textContent = 'Edit onboarding texts';
    link.dataset.onboardingCopyLink = '';
    panel.append(link);
  };
  new MutationObserver(attach).observe(appRoot, {childList: true, subtree: true});
  attach();
}
