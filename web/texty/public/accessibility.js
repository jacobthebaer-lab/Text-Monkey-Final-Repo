// Restore a useful keyboard position after replacing a view's DOM.
export function focusView(selector = '#main-content') {
  const target = document.querySelector(selector) || document.querySelector('#main-content');
  if (!target) return;
  target.setAttribute?.('tabindex', '-1');
  target.focus?.({preventScroll: true});
  target.scrollIntoView?.({block: 'nearest'});
}
