import AdminApp from './AdminApp.vue';
import LoginApp from './LoginApp.vue';
import { mountApex } from '../shared/mount';

const root = document.getElementById('app');
if (root) mountApex(root, window.location.pathname === '/login' ? LoginApp : AdminApp);
