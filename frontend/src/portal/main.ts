import AccountApp from './AccountApp.vue';
import AuthApp from './AuthApp.vue';
import RequestApp, { type RequestBootstrap } from './RequestApp.vue';
import './portal.css';
import { mountApex } from '../shared/mount';

function readJson<T>(id: string): T | null {
  const element = document.getElementById(id);
  if (!element?.textContent) return null;
  try { return JSON.parse(element.textContent) as T; } catch { return null; }
}

function markReady(root: HTMLElement): void {
  root.closest('#account-page, #request-page')?.classList.add('vue-ready');
}

const path = window.location.pathname;
if (path === '/login' || path === '/register') document.body.classList.add('portal-auth');
const root = document.getElementById('app');
const accountRoot = root || document.getElementById('account-vue');
const requestRoot = root || document.getElementById('request-vue');

if (path === '/account' && accountRoot) {
  mountApex(accountRoot, AccountApp);
  markReady(accountRoot);
} else if (path === '/requests' && requestRoot) {
  const bootstrap = readJson<Partial<RequestBootstrap>>('request-bootstrap') || {};
  const defaults: RequestBootstrap = { csrfToken: '', tab: 'search', mode: 'multi', query: '', year: '', results: [], selected: null, pendingCount: 0, libraryCount: 0, rejectedCount: 0, error: '', notice: '' };
  mountApex(requestRoot, RequestApp, { bootstrap: { ...defaults, ...bootstrap }, standalone: requestRoot.id === 'app' });
  markReady(requestRoot);
} else if (root && path === '/register') {
  mountApex(root, AuthApp, { mode: 'register' });
} else if (root) {
  mountApex(root, AuthApp, { mode: 'login' });
}
