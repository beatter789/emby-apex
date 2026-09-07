<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  AlertCircle, ArrowLeft, CheckCircle2, Clock3, Copy, Gift, KeyRound, LayoutDashboard,
  LoaderCircle, LogOut, RefreshCw, ScanLine, Server, Settings, ShieldCheck, UserRound,
  WifiOff,
} from 'lucide-vue-next';
import { ApiError, getApi, postApi } from '../shared/api';
import { AppNav, MobileBottomNav } from '../shared/components';
import { formatDateTime } from '../shared/format';
import { goBack as navigateBack } from '../shared/router';

export type AccountStatus = { tone: string; label: string; hint: string };
export type AccountState = {
  csrf_token: string;
  username: string;
  server_name: string;
  status: AccountStatus;
  expires_at: string | null;
  playback_enabled: boolean;
  playback_source: string;
  synced_at: string | null;
  registered_at: string | null;
};
type RedeemCode = { code: string; duration?: string; amount?: number; unit?: string; used_at?: string | null };
type PendingCode = { code: string; generated_at: string | null; expires_at: string | null };
type ActivationState = {
  pending_codes: PendingCode[];
  used_today: number;
  remaining_today: number;
  scan_image_url: string;
  image_updated_at: string | null;
};
type PortalSection = 'overview' | 'requests' | 'activation' | 'scan' | 'settings';
type ActivationTab = 'bill' | 'redeem';
type ScanImageState = 'idle' | 'loading' | 'ready' | 'empty' | 'error';

const portalNav = [
  { path: '/account', label: '账户概览', icon: LayoutDashboard },
  { path: '/requests', label: '求片', icon: Gift },
  { path: '/account#activation', label: '激活', icon: KeyRound },
  { path: '/account#scan', label: '扫码', icon: ScanLine },
  { path: '/account#settings', label: '账户设置', icon: Settings },
];

function sectionFromHash(): PortalSection {
  const hash = window.location.hash.slice(1);
  return hash === 'activation' ? 'activation' : hash === 'scan' ? 'scan' : hash === 'settings' ? 'settings' : 'overview';
}

const account = ref<AccountState | null>(null);
const redeemCodes = ref<RedeemCode[]>([]);
const activation = ref<ActivationState | null>(null);
const section = ref<PortalSection>(sectionFromHash());
const activationTab = ref<ActivationTab>('bill');
const loading = ref(true);
const codesLoading = ref(false);
const activationLoading = ref(false);
const refreshing = ref(false);
const online = ref(typeof navigator === 'undefined' ? true : navigator.onLine);
const error = ref('');
const actionError = ref('');
const actionNotice = ref('');
const activationError = ref('');
const activationNotice = ref('');
const code = ref('');
const billCode = ref('');
const generatedCode = ref('');
const copiedCode = ref('');
const copyError = ref('');
const username = ref('');
const oldPassword = ref('');
const newPassword = ref('');
const newPassword2 = ref('');
const busy = ref('');
const cooldownUntil = ref(0);
const cooldownRemaining = ref(0);
const scanImageState = ref<ScanImageState>('idle');
const scanImageSrc = ref('');
let scanImageObjectUrl = '';
let cooldownTimer: ReturnType<typeof setTimeout> | null = null;

const sourceLabel = computed(() => account.value?.playback_source === 'controller' ? '控制器' : 'Emby');
const cooldownLabel = computed(() => cooldownRemaining.value > 0 ? `刷新 (${Math.ceil(cooldownRemaining.value / 1000)}s)` : '刷新');
const sectionTitle = computed(() => ({ overview: '账户概览', requests: '求片', activation: '激活', scan: '扫码', settings: '账户设置' }[section.value]));
const activePath = computed(() => section.value === 'overview' ? '/account' : section.value === 'requests' ? '/requests' : `/account#${section.value}`);
const quotaLabel = computed(() => `${activation.value?.used_today || 0}/3`);

watch(sectionTitle, (title) => { document.title = `${title} · Emby Apex`; }, { immediate: true });

function readCooldown(): number { try { return Number(localStorage.getItem('apex-refresh-account') || 0); } catch { return 0; } }
function writeCooldown(value: number): void { try { localStorage.setItem('apex-refresh-account', String(value)); } catch { /* optional storage */ } }
function renderCooldown(): void {
  cooldownUntil.value = Math.max(cooldownUntil.value, readCooldown());
  cooldownRemaining.value = Math.max(0, cooldownUntil.value - Date.now());
  if (cooldownTimer) clearTimeout(cooldownTimer);
  if (cooldownRemaining.value > 0) cooldownTimer = setTimeout(renderCooldown, 250);
}
function handleError(reason: unknown, fallback: string): void {
  if (reason instanceof ApiError && reason.status === 401) { window.location.assign('/login'); return; }
  error.value = reason instanceof Error ? reason.message : fallback;
}
function operationError(reason: unknown, fallback: string): string {
  if (!online.value) return '当前离线，操作已禁用，请恢复联网后重试。';
  if (reason instanceof ApiError && reason.status === 401) { window.location.assign('/login'); return '登录已失效，请重新登录。'; }
  if (reason instanceof ApiError && reason.status === 403) return '当前账户没有执行此操作的权限。';
  if (reason instanceof ApiError && reason.status === 409) return '请求正在处理中，请勿重复提交。';
  if (reason instanceof ApiError && reason.status >= 500) return '服务暂时不可用，请稍后重试。';
  if (reason instanceof ApiError && /csrf|安全/i.test(reason.message)) return 'CSRF 校验失败，请刷新页面后重试。';
  return reason instanceof Error ? reason.message : fallback;
}
function goBack(): void { navigateBack('portal'); }
function selectSection(next: PortalSection): void {
  section.value = next;
  window.history.replaceState(null, '', next === 'overview' ? '/account' : `/account#${next}`);
  actionError.value = ''; actionNotice.value = ''; activationError.value = ''; activationNotice.value = '';
  if (next === 'activation' || next === 'scan') void loadActivation();
  if (next === 'activation') void loadRedeemCodes();
}
function syncHashSection(): void {
  const next = sectionFromHash();
  if (section.value !== next) selectSection(next);
}
function goRequests(): void { window.location.assign('/requests'); }

async function load(): Promise<void> {
  loading.value = true; error.value = '';
  try {
    const response = await getApi<AccountState>('/account');
    account.value = response.data;
    username.value = response.data?.username || '';
  } catch (reason) { handleError(reason, '账户加载失败，请稍后重试。'); }
  finally { loading.value = false; }
}
async function loadRedeemCodes(): Promise<void> {
  if (!online.value) { actionError.value = '当前离线，兑换记录暂时不可加载。'; return; }
  codesLoading.value = true;
  try {
    const response = await getApi<{ codes?: RedeemCode[] }>('/account/redeem-codes');
    redeemCodes.value = response.data?.codes || [];
  } catch (reason) { actionError.value = operationError(reason, '兑换记录加载失败，请稍后重试。'); }
  finally { codesLoading.value = false; }
}
function releaseScanImage(): void {
  if (scanImageObjectUrl) URL.revokeObjectURL(scanImageObjectUrl);
  scanImageObjectUrl = ''; scanImageSrc.value = '';
}
async function loadScanImage(url: string): Promise<void> {
  releaseScanImage();
  if (!url) { scanImageState.value = 'empty'; return; }
  if (!online.value) { scanImageState.value = 'error'; return; }
  scanImageState.value = 'loading';
  try {
    const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'image/*' } });
    if (response.status === 401) { window.location.assign('/login'); return; }
    if (response.status === 404) { scanImageState.value = 'empty'; return; }
    if (!response.ok) throw new Error(`扫码图片加载失败（${response.status}）`);
    const blob = await response.blob();
    scanImageObjectUrl = URL.createObjectURL(blob);
    scanImageSrc.value = scanImageObjectUrl;
    scanImageState.value = 'ready';
  } catch (reason) {
    scanImageState.value = 'error';
    activationError.value = operationError(reason, '扫码图片加载失败，请稍后重试。');
  }
}
async function loadActivation(): Promise<void> {
  if (!online.value) { activationError.value = '当前离线，激活数据暂时不可加载。'; return; }
  activationLoading.value = true; activationError.value = '';
  try {
    const response = await getApi<ActivationState>('/account/activation');
    activation.value = response.data;
    if (section.value === 'scan') await loadScanImage(response.data?.scan_image_url || '');
  } catch (reason) {
    activationError.value = operationError(reason, '激活数据加载失败，请稍后重试。');
  } finally { activationLoading.value = false; }
}
async function refresh(): Promise<void> {
  if (!account.value || refreshing.value || cooldownUntil.value > Date.now() || !online.value) return;
  refreshing.value = true; actionError.value = ''; actionNotice.value = '';
  cooldownUntil.value = Date.now() + 10000; writeCooldown(cooldownUntil.value); renderCooldown();
  try {
    const response = await getApi<Partial<AccountState>>('/account/status');
    account.value = { ...account.value, ...(response.data || {}) };
    actionNotice.value = '账户状态已更新。';
  } catch (reason) { actionError.value = operationError(reason, '刷新失败，请稍后重试。'); }
  finally { refreshing.value = false; renderCooldown(); }
}
async function submit(action: string, payload: Record<string, unknown>, clear: () => void, message: string): Promise<void> {
  if (!account.value || busy.value || !online.value) return;
  busy.value = action; actionError.value = ''; actionNotice.value = '';
  try {
    const response = await postApi(`/account/${action}`, payload, account.value.csrf_token);
    if (response.data && typeof response.data === 'object' && 'account' in response.data) account.value = { ...account.value, ...(response.data as { account: Partial<AccountState> }).account };
    clear(); actionNotice.value = message;
    if (action === 'redeem') { await loadRedeemCodes(); await loadActivation(); }
  } catch (reason) { actionError.value = operationError(reason, '操作失败，请稍后重试。'); }
  finally { busy.value = ''; }
}
function redeem(): void { void submit('redeem', { code: code.value.trim() }, () => { code.value = ''; }, '兑换成功，播放权限已更新。'); }
async function activateBillCode(): Promise<void> {
  const value = billCode.value.trim();
  activationError.value = ''; activationNotice.value = ''; copyError.value = '';
  if (!value) { activationError.value = '账单码无效，请检查后重试。'; return; }
  if ((activation.value?.remaining_today ?? 3) <= 0) { activationError.value = '今日账单码使用次数已达上限，请明天再试。'; return; }
  if (!account.value || busy.value || activationLoading.value || !online.value) {
    if (!online.value) activationError.value = '当前离线，提交操作已禁用。';
    return;
  }
  busy.value = 'bill-code';
  try {
    const response = await postApi<{ redeem_code: string; generated_at: string | null; expires_at: string | null; used_today: number; remaining_today: number }>(
      '/account/activation/bill-code', { bill_code: value }, account.value.csrf_token,
    );
    const result = response.data;
    generatedCode.value = result?.redeem_code || '';
    billCode.value = '';
    activation.value = {
      ...(activation.value || { pending_codes: [], scan_image_url: '', image_updated_at: null }),
      used_today: result?.used_today ?? ((activation.value?.used_today || 0) + 1),
      remaining_today: result?.remaining_today ?? Math.max(0, 3 - ((activation.value?.used_today || 0) + 1)),
      pending_codes: [
        ...(activation.value?.pending_codes || []).filter((item) => item.code !== result?.redeem_code),
        { code: result?.redeem_code || '', generated_at: result?.generated_at || null, expires_at: result?.expires_at || null },
      ].filter((item) => item.code),
    };
    activationNotice.value = '账单码验证成功，兑换码已生成。请复制兑换码后，到“兑换码激活”区域手动提交。账户不会自动激活。';
  } catch (reason) { activationError.value = operationError(reason, '账单码提交失败，请稍后重试。'); }
  finally { busy.value = ''; }
}
async function copyText(value: string): Promise<void> {
  if (!value) return;
  copyError.value = '';
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value);
    else {
      const input = document.createElement('textarea'); input.value = value; input.style.position = 'fixed'; input.style.opacity = '0';
      document.body.appendChild(input); input.select(); document.execCommand('copy'); input.remove();
    }
    copiedCode.value = value;
    window.setTimeout(() => { if (copiedCode.value === value) copiedCode.value = ''; }, 1800);
  } catch { copyError.value = '复制失败，请手动选择兑换码复制。'; }
}
function sanitizeBillCode(): void { /* Keep server-side validation authoritative. */ }
function changeUsername(): void { void submit('username', { new_username: username.value }, () => {}, '用户名已修改。'); }
function changePassword(): void { void submit('password', { old_password: oldPassword.value, new_password: newPassword.value, new_password2: newPassword2.value }, () => { oldPassword.value = ''; newPassword.value = ''; newPassword2.value = ''; }, '密码已修改。'); }
async function logout(): Promise<void> {
  if (!account.value || busy.value) return;
  busy.value = 'logout';
  try { await postApi('/auth/logout', {}, account.value.csrf_token); window.location.assign('/login'); }
  catch (reason) { actionError.value = operationError(reason, '退出失败，请稍后重试。'); }
  finally { busy.value = ''; }
}
function updateOnline(): void { online.value = navigator.onLine; }
function handleScanImageError(): void { scanImageState.value = 'error'; activationError.value = '扫码图片内容无法显示，请刷新页面后重试。'; }
onMounted(() => {
  document.title = `${sectionTitle.value} · Emby Apex`;
  cooldownUntil.value = readCooldown(); renderCooldown();
  window.addEventListener('online', updateOnline); window.addEventListener('offline', updateOnline); window.addEventListener('hashchange', syncHashSection);
  void load();
  if (section.value === 'activation' || section.value === 'scan') void loadActivation();
  if (section.value === 'activation') void loadRedeemCodes();
});
onBeforeUnmount(() => {
  window.removeEventListener('online', updateOnline); window.removeEventListener('offline', updateOnline); window.removeEventListener('hashchange', syncHashSection);
  if (cooldownTimer) clearTimeout(cooldownTimer); releaseScanImage();
});
</script>

<template>
  <div class="portal-workspace">
    <aside class="portal-sidebar" aria-label="用户中心导航">
      <div class="brand-block"><img :src="'/static/brand-logo.png'" alt=""><div class="brand-copy"><strong>Emby Apex</strong><span>用户中心</span></div></div>
      <AppNav class="portal-sidebar-nav" :items="portalNav" :active-path="activePath" aria-label="用户中心导航" />
      <button class="portal-sidebar-logout" type="button" :disabled="busy === 'logout'" @click="logout"><LogOut :size="16" />退出登录</button>
    </aside>
    <main class="portal-main vue-account-app">
      <header class="portal-header bar"><button class="back-button" type="button" aria-label="返回上一页" title="返回上一页" @click="goBack"><ArrowLeft :size="20" /></button><h1 class="portal-brand"><img :src="'/static/brand-logo.png'" alt=""><span>{{ sectionTitle }}</span></h1><div class="actions"><button class="link" type="button" :disabled="refreshing || cooldownRemaining > 0 || !online" @click="refresh"><LoaderCircle v-if="refreshing" class="spin" :size="15" /><RefreshCw v-else :size="15" />{{ refreshing ? '刷新中' : cooldownLabel }}</button><button class="link" type="button" :disabled="busy === 'logout' || !online" @click="logout"><LogOut :size="15" />退出登录</button></div></header>
      <div v-if="error" class="msg error vue-feedback" role="alert"><AlertCircle :size="18" /><span>{{ error }}</span><a v-if="error.includes('登录')" href="/login" class="button-link">重新登录</a></div>
      <p v-if="!online" class="msg notice vue-feedback" role="status"><WifiOff :size="18" />当前离线，写入和状态刷新已禁用。</p>
      <div v-if="loading" class="card vue-state"><LoaderCircle class="spin" :size="22" />正在加载账户…</div>
      <template v-else-if="account">
        <div v-if="actionError" class="msg error vue-feedback" role="alert"><AlertCircle :size="18" /><span>{{ actionError }}</span><a v-if="actionError.includes('登录')" href="/login" class="button-link">重新登录</a></div>
        <div v-if="actionNotice" class="msg notice vue-feedback" role="status"><CheckCircle2 :size="18" /><span>{{ actionNotice }}</span></div>
        <section v-if="section === 'overview'" class="card portal-section"><div class="vue-section-heading"><div><h2>账户状态</h2><p class="hint">当前账户与播放权限概览。</p></div><button class="secondary sm" type="button" :disabled="refreshing || cooldownRemaining > 0 || !online" @click="refresh"><RefreshCw :size="15" />{{ refreshing ? '刷新中' : cooldownLabel }}</button></div><dl class="facts vue-account-facts"><dt><UserRound :size="14" />用户名</dt><dd>{{ account.username || '—' }}</dd><dt><Server :size="14" />所在服务器</dt><dd>{{ account.server_name || '—' }}</dd><dt><ShieldCheck :size="14" />状态</dt><dd><span :class="['pill', account.status.tone]">{{ account.status.label }}</span></dd><dt><Clock3 :size="14" />到期时间</dt><dd>{{ formatDateTime(account.expires_at) }}</dd><dt><ShieldCheck :size="14" />播放权限</dt><dd>{{ account.playback_enabled ? '已开通' : '未开通' }}</dd><dt><ShieldCheck :size="14" />权限来源</dt><dd>{{ sourceLabel }}</dd><dt><RefreshCw :size="14" />最后同步</dt><dd>{{ formatDateTime(account.synced_at) }}</dd><dt><Clock3 :size="14" />注册时间</dt><dd>{{ formatDateTime(account.registered_at) }}</dd></dl><p class="hint">{{ account.status.hint }}</p></section>
        <section v-else-if="section === 'activation'" class="card portal-section activation-page">
          <div v-if="activationError" class="msg error vue-feedback" role="alert"><AlertCircle :size="17" /><span>{{ activationError }}</span><a v-if="activationError.includes('登录')" href="/login" class="button-link">重新登录</a></div>
          <div v-if="activationNotice" class="msg notice vue-feedback" role="status"><CheckCircle2 :size="17" /><span>{{ activationNotice }}</span></div>
          <div class="vue-section-heading"><div><h2>激活中心</h2><p class="hint">账单码先生成兑换码，再由你手动提交兑换，不会自动激活账户。</p></div><KeyRound :size="22" /></div>
          <div class="activation-tabs" role="tablist" aria-label="激活方式"><button type="button" role="tab" :aria-selected="activationTab === 'bill'" :class="{ on: activationTab === 'bill' }" @click="activationTab = 'bill'"><KeyRound :size="16" />账单码激活</button><button type="button" role="tab" :aria-selected="activationTab === 'redeem'" :class="{ on: activationTab === 'redeem' }" @click="activationTab = 'redeem'; loadRedeemCodes()"><Gift :size="16" />兑换码激活</button></div>
          <div v-if="activationLoading && !activation" class="vue-state compact"><LoaderCircle class="spin" :size="20" />正在加载激活数据…</div>
          <div v-else-if="activationTab === 'bill'" class="activation-content">
            <div class="activation-quota"><span><strong>今日账单码</strong><small>已使用 / 每日上限</small></span><b>{{ quotaLabel }}</b><span class="quota-remaining">剩余 {{ activation?.remaining_today ?? 3 }} 次</span></div>
            <form class="activation-form" @submit.prevent="activateBillCode"><label><span>账单码</span><input v-model="billCode" type="text" inputmode="numeric" autocomplete="off" placeholder="请输入账单码" @input="sanitizeBillCode"></label><button class="primary" type="submit" :disabled="busy === 'bill-code' || activationLoading || !online || (activation?.remaining_today ?? 3) <= 0"><LoaderCircle v-if="busy === 'bill-code'" class="spin" :size="16" />{{ busy === 'bill-code' ? '验证中' : '生成兑换码' }}</button></form>
            <div v-if="generatedCode" class="activation-result"><div><span class="eyebrow">新生成的兑换码</span><code>{{ generatedCode }}</code><p>请复制兑换码后，到“兑换码激活”区域手动提交。账户不会自动激活。</p></div><button class="secondary sm" type="button" @click="copyText(generatedCode)"><Copy :size="15" />{{ copiedCode === generatedCode ? '已复制' : '复制' }}</button></div>
            <p v-if="copyError" class="hint activation-copy-error">{{ copyError }}</p>
            <div class="pending-code-section"><div class="vue-section-heading"><div><h3>待使用兑换码</h3><p class="hint">仅显示当前账户尚未使用且仍有效的兑换码。</p></div><button class="secondary sm" type="button" :disabled="activationLoading || !online" @click="loadActivation"><RefreshCw :class="{ spin: activationLoading }" :size="15" />刷新</button></div><div v-if="activationLoading && !activation" class="vue-state compact"><LoaderCircle class="spin" :size="20" />正在加载待使用兑换码…</div><div v-else-if="!activation?.pending_codes?.length" class="empty activation-empty">暂无待使用兑换码。</div><div v-else class="pending-code-list"><article v-for="item in activation.pending_codes" :key="item.code" class="pending-code-row"><div class="pending-code-value"><code>{{ item.code }}</code><span class="pill pending">待使用</span></div><dl><div><dt>生成时间</dt><dd>{{ formatDateTime(item.generated_at) }}</dd></div><div><dt>失效时间</dt><dd>{{ item.expires_at ? formatDateTime(item.expires_at) : '永不过期' }}</dd></div></dl><button class="secondary sm" type="button" @click="copyText(item.code)"><Copy :size="15" />{{ copiedCode === item.code ? '已复制' : '复制' }}</button></article></div></div>
          </div>
          <div v-else class="activation-content"><form class="activation-form" @submit.prevent="redeem"><label><span>输入兑换码</span><input v-model="code" type="text" required autocomplete="off" placeholder="例如 A1B2-C3D4-E5F6"></label><button class="primary" type="submit" :disabled="busy === 'redeem' || !online"><LoaderCircle v-if="busy === 'redeem'" class="spin" :size="16" />{{ busy === 'redeem' ? '兑换中' : '提交兑换' }}</button></form><div class="used-code-section"><div class="vue-section-heading"><div><h3>已使用兑换码</h3><p class="hint">仅显示当前账户的兑换记录。</p></div><button class="secondary sm" type="button" :disabled="codesLoading" @click="loadRedeemCodes"><RefreshCw :class="{ spin: codesLoading }" :size="15" />刷新</button></div><div v-if="codesLoading && !redeemCodes.length" class="vue-state compact"><LoaderCircle class="spin" :size="20" />加载兑换记录…</div><div v-else-if="!redeemCodes.length" class="empty">暂无已使用兑换码。</div><div v-else class="used-code-list"><article v-for="item in redeemCodes" :key="item.code" class="used-code-row"><code>{{ item.code }}</code><span>{{ item.duration || `${item.amount || 0} ${item.unit || ''}` }}</span><time>{{ formatDateTime(item.used_at) }}</time><span class="pill ok">已使用</span></article></div></div></div>
        </section>
        <section v-else-if="section === 'scan'" class="card portal-section scan-page"><div v-if="activationError" class="msg error vue-feedback" role="alert"><AlertCircle :size="17" /><span>{{ activationError }}</span><a v-if="activationError.includes('登录')" href="/login" class="button-link">重新登录</a></div><div class="vue-section-heading"><div><h2>扫码</h2><p class="hint">管理员提供的收款或激活二维码。</p></div><ScanLine :size="22" /></div><div class="scan-detail-scroll"><div v-if="activationLoading || scanImageState === 'loading'" class="scan-state"><LoaderCircle class="spin" :size="26" /><span>正在加载扫码图片…</span></div><div v-else-if="scanImageState === 'empty'" class="scan-state"><ScanLine :size="28" /><strong>暂未上传扫码图片</strong><span class="hint">请联系管理员配置后再试。</span></div><div v-else-if="scanImageState === 'error'" class="scan-state"><AlertCircle :size="28" /><strong>扫码图片加载失败</strong><span class="hint">请检查网络后刷新页面。</span><button class="secondary sm" type="button" @click="loadActivation">重试</button></div><img v-else-if="scanImageSrc" class="scan-image" :src="scanImageSrc" alt="管理员扫码图片" @error="handleScanImageError"><div v-else class="scan-state"><ScanLine :size="28" /><strong>暂未上传扫码图片</strong></div></div><p v-if="activation?.image_updated_at && scanImageState === 'ready'" class="hint scan-updated">更新时间：{{ formatDateTime(activation.image_updated_at) }}</p></section>
        <section v-else class="card portal-section"><h2>账户设置</h2><p class="hint">修改用户名或密码后，请使用新凭据登录 Emby 客户端。</p><div class="setting-section"><h3>修改用户名</h3><form @submit.prevent="changeUsername"><label><span>新用户名</span><input v-model="username" type="text" required autocomplete="off"></label><button class="primary" type="submit" :disabled="busy === 'username' || !online">{{ busy === 'username' ? '保存中' : '保存用户名' }}</button></form></div><div class="setting-section"><h3>修改密码</h3><form @submit.prevent="changePassword"><label><span>当前密码</span><input v-model="oldPassword" type="password" required autocomplete="current-password"></label><label><span>新密码</span><input v-model="newPassword" type="password" required autocomplete="new-password"></label><label><span>确认新密码</span><input v-model="newPassword2" type="password" required autocomplete="new-password"></label><button class="primary" type="submit" :disabled="busy === 'password' || !online">{{ busy === 'password' ? '保存中' : '保存密码' }}</button></form></div></section>
      </template>
    </main>
    <MobileBottomNav :items="portalNav" :active-path="activePath" aria-label="用户中心导航" />
  </div>
</template>
