(() => {
  const root = document.documentElement;
  const body = document.body;
  const toggle = document.querySelector('[data-sidebar-toggle]');
  const moreButtons = document.querySelectorAll('[data-mobile-more]');
  const dialog = document.getElementById('mobile-more-dialog');

  try {
    if (localStorage.getItem('ec-sidebar-collapsed') === '1') root.classList.add('sidebar-collapsed');
  } catch (_) {}

  toggle?.addEventListener('click', () => {
    root.classList.toggle('sidebar-collapsed');
    try { localStorage.setItem('ec-sidebar-collapsed', root.classList.contains('sidebar-collapsed') ? '1' : '0'); } catch (_) {}
  });
  const closeDialog = () => {
    if (!dialog) return;
    if (typeof dialog.close === 'function' && dialog.open) dialog.close();
    else dialog.removeAttribute('open');
    dialog.classList.remove('open');
    moreButtons.forEach((button) => button.setAttribute('aria-expanded', 'false'));
  };
  const openDialog = (button) => {
    if (!dialog) return;
    moreButtons.forEach((item) => item.setAttribute('aria-expanded', item === button ? 'true' : 'false'));
    if (typeof dialog.showModal === 'function') {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute('open', '');
      dialog.classList.add('open');
    }
  };
  moreButtons.forEach((button) => button.addEventListener('click', () => openDialog(button)));
  dialog?.querySelector('[data-dialog-close]')?.addEventListener('click', closeDialog);
  dialog?.addEventListener('click', (event) => { if (event.target === dialog) closeDialog(); });

  document.querySelectorAll('[data-back-button]').forEach((button) => {
    button.addEventListener('click', () => {
      const fallback = button.dataset.fallback || (body?.classList.contains('portal-shell') ? '/account' : '/dashboard');
      if (window.history.length > 1) window.history.back();
      else window.location.assign(fallback);
    });
  });

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js', { scope: '/', updateViaCache: 'none' }).catch(() => {}));
  }

  const setOfflineState = () => {
    body?.classList.toggle('is-offline', !navigator.onLine);
    document.querySelectorAll('[data-online-state]').forEach((node) => {
      node.textContent = navigator.onLine ? '在线' : '离线，只读缓存';
      node.classList.toggle('off', !navigator.onLine);
      node.classList.toggle('ok', navigator.onLine);
    });
    document.querySelectorAll('form[method="post"] button[type="submit"]').forEach((button) => {
      if (!navigator.onLine && button.dataset.allowOffline !== 'true') {
        button.disabled = true;
        button.dataset.offlineDisabled = '1';
      }
      if (navigator.onLine && button.dataset.offlineDisabled === '1') {
        button.disabled = false;
        delete button.dataset.offlineDisabled;
      }
    });
  };
  window.addEventListener('online', setOfflineState);
  window.addEventListener('offline', setOfflineState);
  setOfflineState();
})();
