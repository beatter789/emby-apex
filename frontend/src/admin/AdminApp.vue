<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  AlertCircle, ArrowLeft, Bell, Building2, CalendarClock, CheckCircle2, ChevronDown, Clock3,
  ChevronLeft, ChevronRight, Film, ImagePlus, LayoutDashboard, LoaderCircle, LogOut, MessageCircle, MoreHorizontal, RefreshCw,
  Save, Search, Server, Settings, ShieldCheck, Star, Trash2, Users, Upload, Webhook, WifiOff, X,
} from 'lucide-vue-next';
import { ApiError, deleteApi, getApi, patchApi, postApi } from '../shared/api';
import { AppNav } from '../shared/components';
import { formatDateTime } from '../shared/format';
import { goBack as navigateBack } from '../shared/router';

type DashboardData = {
  summary: { servers: number; users: number; disabled: number; expiring: number; playing: number };
  sessions: Array<Record<string, any>>;
  watch_time: { date: string; min_date: string; max_date: string; hours: number; seconds: number; users: Array<{ username: string; plays: number; hours: number; seconds?: number; server_id?: number; emby_user_id?: string; server_name?: string }> };
  trend: Array<{ date: string; plays: number; hours: number }>;
  servers: Array<Record<string, any>>;
  logs: Array<Record<string, any>>;
  poll_interval: number;
};
type RequestGroup = Record<string, any>;
type SettingRow = { name: string; label: string; hint?: string; kind: string; value: any; min?: number | null; max?: number | null; configured?: boolean; sensitive?: boolean };
type ActivationImageMeta = { scan_image_url: string; image_updated_at: string | null; mime?: string | null; size?: number; target_width: number; target_height: number };
type ImageDimensions = { width: number; height: number };
type SettingsTab = 'general' | 'notifications';
type NotificationCategory = 'registration' | 'expiry' | 'media_request' | 'activation' | 'general';
type NotificationChannel = 'webhook' | 'telegram' | 'wecom';
type NotificationState = { busy: string; notice: string; error: string };
type WatchCalendarDay = { iso: string; label: string; inMonth: boolean; disabled: boolean };

const notificationCategories: Array<{ key: NotificationCategory; label: string; description: string }> = [
  { key: 'registration', label: '注册通知', description: '新用户注册、认领或开通时提醒管理员。' },
  { key: 'expiry', label: '到期通知', description: '用户即将到期、停用或回收时提醒管理员。' },
  { key: 'media_request', label: '求片通知', description: '用户提交求片或求片状态变化时提醒管理员。' },
  { key: 'activation', label: '账单码激活通知', description: '用户成功提交账单码并生成兑换码时提醒管理员。' },
  { key: 'general', label: '其他通知', description: '同步、异常和其他运营事件的提醒。' },
];
const notificationChannels: Array<{ key: NotificationChannel; label: string; description: string; icon: any; fields: string[] }> = [
  { key: 'webhook', label: 'Webhook', description: '向自定义地址推送注册、求片和激活事件。', icon: Webhook, fields: ['notify_webhook_url', 'notify_webhook_use_proxy'] },
  { key: 'telegram', label: 'Telegram', description: '通过 Telegram Bot 向指定会话发送运营提醒。', icon: MessageCircle, fields: ['telegram_bot_token', 'telegram_chat_id', 'notify_telegram_use_proxy'] },
  {
    key: 'wecom', label: '企业微信', description: '使用企业微信自建应用向管理员发送通知。', icon: Building2,
    fields: ['wecom_corp_id', 'wecom_agent_id', 'wecom_secret', 'wecom_api_base_url', 'wecom_token', 'wecom_encoding_aes_key', 'wecom_admin_whitelist', 'notify_wecom_use_proxy'],
  },
];
const sharedNotificationFields = ['notify_proxy_url'];
const notificationFieldNames = new Set([...notificationChannels.flatMap((channel) => channel.fields), ...sharedNotificationFields]);
const makeNotificationState = (): NotificationState => ({ busy: '', notice: '', error: '' });

const nav = [
  { path: '/dashboard', label: '总览', icon: LayoutDashboard, group: '开始', accent: '#60a5fa' },
  { path: '/users', label: '用户', icon: Users, group: '系统', accent: '#84cc16' },
  { path: '/servers', label: '服务器', icon: Server, group: '整理', accent: '#eab308' },
  { path: '/requests', label: '求片', icon: Clock3, group: '订阅', accent: '#a855f7' },
  { path: '/codes', label: '兑换码', icon: CheckCircle2, group: '订阅', accent: '#f97316' },
  { path: '/history', label: '历史', icon: Clock3, group: '整理', accent: '#38bdf8' },
  { path: '/logs', label: '日志', icon: AlertCircle, group: '系统', accent: '#f43f5e' },
  { path: '/settings', label: '设置', icon: Settings, group: '系统', accent: '#8b5cf6' },
];

const adminNavGroups = [
  { label: '开始', items: [nav[0]] },
  { label: '订阅', items: [nav[3], nav[4]] },
  { label: '整理', items: [nav[2], nav[5]] },
  { label: '系统', items: [nav[1], nav[6], nav[7]] },
];

const currentPath = ref(window.location.pathname || '/dashboard');
const data = ref<DashboardData | null>(null);
const watchDate = ref('');
const watchTimeLoading = ref(false);
const watchTimeError = ref('');
const watchDatePickerOpen = ref(false);
const watchDatePickerMonth = ref('');
const watchWeekdays = ['日', '一', '二', '三', '四', '五', '六'];
const watchDateMonthLabel = computed(() => {
  const month = watchDatePickerMonth.value || data.value?.watch_time.max_date?.slice(0, 7) || '';
  if (!month) return '';
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long', timeZone: 'UTC' })
    .format(new Date(`${month}-01T00:00:00Z`));
});
const watchDateDays = computed<WatchCalendarDay[]>(() => {
  const minDate = data.value?.watch_time.min_date || '';
  const maxDate = data.value?.watch_time.max_date || '';
  const month = watchDatePickerMonth.value || maxDate.slice(0, 7);
  if (!month) return [];
  const [year, monthNumber] = month.split('-').map(Number);
  const first = new Date(Date.UTC(year, monthNumber - 1, 1));
  const start = new Date(Date.UTC(year, monthNumber - 1, 1 - first.getUTCDay()));
  return Array.from({ length: 42 }, (_, index) => {
    const current = new Date(start);
    current.setUTCDate(start.getUTCDate() + index);
    const iso = current.toISOString().slice(0, 10);
    return {
      iso,
      label: String(current.getUTCDate()),
      inMonth: current.getUTCMonth() === monthNumber - 1,
      disabled: iso < minDate || iso > maxDate,
    };
  });
});
const watchDatePreviousDisabled = computed(() => {
  const minMonth = data.value?.watch_time.min_date?.slice(0, 7) || '';
  return !watchDatePickerMonth.value || watchDatePickerMonth.value <= minMonth;
});
const watchDateNextDisabled = computed(() => {
  const maxMonth = data.value?.watch_time.max_date?.slice(0, 7) || '';
  return !watchDatePickerMonth.value || watchDatePickerMonth.value >= maxMonth;
});
let dashboardRequestId = 0;
const pageData = ref<Record<string, any> | null>(null);
const settings = ref<SettingRow[]>([]);
const registrationServers = ref<Array<Record<string, any>>>([]);
const registrationServerId = ref<number | null>(null);
const loading = ref(true);
const error = ref('');
const online = ref(typeof navigator === 'undefined' ? true : navigator.onLine);
const refreshing = ref(false);
const csrfToken = ref('');
const settingsTab = ref<SettingsTab>('general');
const expandedNotifications = ref<Record<NotificationCategory, boolean>>({
  registration: true,
  expiry: false,
  media_request: false,
  activation: false,
  general: false,
});
const notificationState = ref<Record<NotificationCategory, NotificationState>>({
  registration: makeNotificationState(),
  expiry: makeNotificationState(),
  media_request: makeNotificationState(),
  activation: makeNotificationState(),
  general: makeNotificationState(),
});
const notificationDialogChannel = ref<NotificationChannel | null>(null);
const notificationDialogNotice = ref('');
const notificationDialogError = ref('');
const notificationDialogBusy = ref('');
const requestState = ref('pending');
const rejectReasons = ref<Record<string, string>>({});
const serverForm = ref({ name: '', base_url: '', api_key: '', verify_ssl: false });
const userForm = ref({ server_id: '', username: '', password: '', ordinary_registration: true, allow_playback: true });
const codeForm = ref({ amount: 30, unit: 'day', quantity: 1, note: '' });
const busy = ref('');
const moreOpen = ref(false);
const moreSearchOpen = ref(false);
const moreSearchQuery = ref('');
const areaNotice = ref('');
const areaError = ref('');
const settingsBusy = ref('');
const settingsNotice = ref('');
const settingsError = ref('');
const activationImage = ref<ActivationImageMeta | null>(null);
const activationImageSrc = ref('');
const activationImageLoading = ref(false);
const activationImageBusy = ref('');
const activationImageNotice = ref('');
const activationImageError = ref('');
const activationFileInput = ref<HTMLInputElement | null>(null);
const activationImageDimensions = ref<ImageDimensions | null>(null);
let activationPreviewObjectUrl = '';
const wecomMenu = ref<Record<string, unknown> | null>(null);
const menuBusy = ref('');
const menuNotice = ref('');
const menuError = ref('');
const logLevel = ref('all');
const logPage = ref(1);
const selectedUser = ref<Record<string, any> | null>(null);
const userDialogOpen = ref(false);
const userDialogBusy = ref(false);
const userDialogNotice = ref('');
const userDialogError = ref('');
const userEdit = ref({ expires_at: '', permanent: false, disabled: false, playback_enabled: false, portal_enabled: false, portal_password: '', note: '' });
const selectedRequest = ref<RequestGroup | null>(null);
const requestDetail = ref<Record<string, any> | null>(null);
const requestDetailBusy = ref(false);
const requestDetailNotice = ref('');
const requestDetailError = ref('');
const cooldownUntil = ref(0);
const cooldownRemaining = ref(0);
let cooldownTimer: ReturnType<typeof setTimeout> | null = null;
let pollTimer: ReturnType<typeof setInterval> | null = null;

const pageTitle = computed(() => nav.find((item) => item.path === currentPath.value)?.label || '总览');
const filteredNavGroups = computed(() => {
  const query = moreSearchQuery.value.trim().toLocaleLowerCase();
  return adminNavGroups
    .map((group) => ({
      ...group,
      items: query ? group.items.filter((item) => item.label.toLocaleLowerCase().includes(query)) : group.items,
    }))
    .filter((group) => group.items.length > 0);
});
const cooldownLabel = computed(() => cooldownRemaining.value > 0 ? `刷新 (${Math.ceil(cooldownRemaining.value / 1000)}s)` : '刷新');
const maxPlays = computed(() => Math.max(1, ...(data.value?.trend || []).map((point) => point.plays)));
const users = computed(() => pageData.value?.users || []);
const servers = computed(() => pageData.value?.servers || []);
const requestGroups = computed<RequestGroup[]>(() => pageData.value?.requests || []);
const history = computed(() => pageData.value?.records || []);
const logs = computed(() => pageData.value?.logs || []);
const logsHasNext = computed(() => Boolean(pageData.value?.has_next));
const codes = computed(() => pageData.value?.codes || []);
const wecomConfigured = computed(() => {
  const corpId = String(settingValue('wecom_corp_id') || '').trim();
  const agentId = String(settingValue('wecom_agent_id') || '').trim();
  const secret = settings.value.find((row) => row.name === 'wecom_secret');
  return Boolean(corpId && agentId && secret?.configured);
});
const wecomMenuItems = computed(() => {
  const buttons = Array.isArray(wecomMenu.value?.button) ? wecomMenu.value.button : [];
  return buttons.map((button) => {
    const item = button as Record<string, unknown>;
    const children = Array.isArray(item.sub_button) ? item.sub_button as Array<Record<string, unknown>> : [];
    return { name: String(item.name || '未命名'), children: children.map((child) => String(child.name || '未命名')) };
  });
});
const generalSettings = computed(() => settings.value.filter((row) => !row.name.includes('.') && !notificationFieldNames.has(row.name)));
const registrationEnabled = computed(() => Boolean(settings.value.find((row) => row.name === 'registration_enabled')?.value));

function rowsFor(names: string[]): SettingRow[] {
  return names.map((name) => settings.value.find((row) => row.name === name)).filter((row): row is SettingRow => Boolean(row));
}
function categoryToggleRows(category: NotificationCategory): SettingRow[] {
  return rowsFor(notificationChannels.map((channel) => `${category}.${channel.key}`));
}
function notificationCategoryEnabled(category: NotificationCategory): boolean {
  return categoryToggleRows(category).some((row) => Boolean(row.value));
}
function channelEnabled(channel: NotificationChannel): boolean {
  return channelConfigured(channel) && notificationCategories.some((category) => {
    const row = settings.value.find((item) => item.name === `${category.key}.${channel}`);
    return Boolean(row?.value);
  });
}
function channelConfigured(channel: NotificationChannel): boolean {
  if (channel === 'webhook') return Boolean(String(settingValue('notify_webhook_url') || '').trim());
  if (channel === 'telegram') {
    const token = settings.value.find((row) => row.name === 'telegram_bot_token');
    return Boolean(token?.configured && String(settingValue('telegram_chat_id') || '').trim());
  }
  return wecomConfigured.value;
}
function channelFields(channel: NotificationChannel): SettingRow[] {
  const definition = notificationChannels.find((item) => item.key === channel);
  return rowsFor(definition?.fields || []);
}
function channelLabel(channel: NotificationChannel): string {
  return notificationChannels.find((item) => item.key === channel)?.label || channel;
}
function channelIcon(channel: NotificationChannel): any {
  return notificationChannels.find((item) => item.key === channel)?.icon || Bell;
}
function openNotificationDialog(channel: NotificationChannel): void {
  notificationDialogChannel.value = channel;
  notificationDialogNotice.value = '';
  notificationDialogError.value = '';
}
function closeNotificationDialog(): void {
  if (!notificationDialogBusy.value) notificationDialogChannel.value = null;
}
function setNotificationToggle(row: SettingRow, channel: NotificationChannel): void {
  if (row.value && !channelConfigured(channel)) {
    row.value = false;
    notificationDialogError.value = `${channelLabel(channel)}尚未配置完整，请先完成渠道配置。`;
  }
}
function categoryState(category: NotificationCategory): NotificationState {
  return notificationState.value[category];
}
function toggleNotification(category: NotificationCategory): void {
  expandedNotifications.value[category] = !expandedNotifications.value[category];
}
function showsSharedChannelConfig(category: NotificationCategory): boolean {
  // Connection credentials are global to a channel, while switches belong to
  // each notification category. Keep the credentials and one test action in
  // the catch-all card so the same secret fields are not repeated per category.
  return category === 'general';
}
function settingLabel(row: SettingRow): string {
  const labels: Record<string, string> = {
    telegram_bot_token: 'Telegram 机器人密钥',
    wecom_corp_id: '企业 ID',
    wecom_agent_id: '应用 AgentId',
    wecom_secret: '应用 Secret',
    wecom_api_base_url: '代理地址',
    wecom_token: 'Token',
    wecom_encoding_aes_key: 'EncodingAESKey',
    wecom_admin_whitelist: '管理员白名单',
    notify_wecom_use_proxy: '使用代理',
  };
  return labels[row.name] || row.label;
}
function settingPlaceholder(row: SettingRow): string {
  return row.sensitive || row.kind === 'secret' ? (row.configured ? '已配置，留空保持' : '请输入后保存') : '';
}
function settingHint(row: SettingRow): string {
  const wecomHints: Record<string, string> = {
    wecom_corp_id: '企业微信后台企业信息中的企业 ID',
    wecom_agent_id: '企业微信自建应用的 AgentId',
    wecom_secret: '企业微信自建应用的 Secret',
    wecom_api_base_url: '微信消息的转发代理地址；不使用代理时保留默认地址',
    wecom_token: '企业微信自建应用 API 接收消息配置中的 Token',
    wecom_encoding_aes_key: '企业微信自建应用 API 接收消息配置中的 EncodingAESKey',
    wecom_admin_whitelist: '可使用管理员菜单及命令的用户 ID 列表，多个 ID 使用分隔符分隔',
    notify_wecom_use_proxy: '启用后通过代理地址发送企业微信通知',
  };
  if (wecomHints[row.name]) {
    if ((row.sensitive || row.kind === 'secret') && row.configured) return `${wecomHints[row.name]}；已配置时留空保持原值。`;
    return wecomHints[row.name];
  }
  if (row.sensitive || row.kind === 'secret') return row.configured ? '已配置，留空保持原值。' : '填写后安全保存。';
  return row.hint || '';
}
function settingIcon(row: SettingRow): any | null {
  const icons: Record<string, any> = {
    notify_webhook_url: Webhook,
    notify_webhook_use_proxy: Server,
    telegram_bot_token: ShieldCheck,
    telegram_chat_id: MessageCircle,
    notify_telegram_use_proxy: Server,
    wecom_corp_id: Building2,
    wecom_agent_id: Settings,
    wecom_secret: ShieldCheck,
    wecom_api_base_url: Server,
    wecom_token: ShieldCheck,
    wecom_encoding_aes_key: ShieldCheck,
    wecom_admin_whitelist: Users,
    notify_wecom_use_proxy: Server,
    notify_proxy_url: Server,
  };
  return icons[row.name] || null;
}
function localDateTimeSeconds(value: unknown): string {
  if (!value) return '';
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return '';
  const pad = (part: number) => String(part).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
async function openUserDetails(user: Record<string, any>): Promise<void> {
  selectedUser.value = user;
  userEdit.value = {
    expires_at: localDateTimeSeconds(user.expires_at),
    permanent: !user.expires_at,
    disabled: Boolean(user.is_disabled),
    playback_enabled: Boolean(user.playback_enabled),
    portal_enabled: Boolean(user.portal_enabled),
    portal_password: '',
    note: String(user.note || ''),
  };
  userDialogNotice.value = '';
  userDialogError.value = '';
  userDialogOpen.value = true;
  try {
    const response = await getApi<Record<string, any>>(`/users/${user.id}`);
    if (response.data && userDialogOpen.value && selectedUser.value?.id === user.id) selectedUser.value = response.data;
  } catch (reason) {
    if (userDialogOpen.value && selectedUser.value?.id === user.id) userDialogError.value = safeOperationError(reason, '用户详情加载');
  }
}
function closeUserDetails(): void {
  if (!userDialogBusy.value) userDialogOpen.value = false;
}
async function saveUserDetails(): Promise<void> {
  const user = selectedUser.value;
  if (!user || userDialogBusy.value || !online.value) {
    if (!online.value) userDialogError.value = '当前离线，用户修改已禁用。';
    return;
  }
  if (user.is_admin && userEdit.value.disabled) {
    userDialogError.value = '管理员账号不可停用。';
    return;
  }
  if (!userEdit.value.permanent && !userEdit.value.expires_at) {
    userDialogError.value = '请设置到期时间，或选择永久账户。';
    return;
  }
  userDialogBusy.value = true;
  userDialogNotice.value = '';
  userDialogError.value = '';
  try {
    await patchApi(`/users/${user.id}`, {
      expires_at: userEdit.value.permanent ? null : userEdit.value.expires_at,
      disabled: userEdit.value.disabled,
      playback_enabled: userEdit.value.playback_enabled,
      portal_enabled: userEdit.value.portal_enabled,
      ...(userEdit.value.portal_password ? { password: userEdit.value.portal_password } : {}),
      note: userEdit.value.note,
    }, csrfToken.value);
    await loadResource(true);
    userDialogNotice.value = '用户信息已保存。';
    const updated = users.value.find((item: Record<string, any>) => item.id === user.id);
    if (updated) selectedUser.value = updated;
  } catch (reason) {
    userDialogError.value = safeOperationError(reason, '用户保存');
  } finally {
    userDialogBusy.value = false;
  }
}
async function deleteSelectedUser(): Promise<void> {
  const user = selectedUser.value;
  if (!user || user.is_admin || userDialogBusy.value) return;
  if (!window.confirm(`确认永久删除用户“${user.username}”？此操作不可撤销。`)) return;
  userDialogBusy.value = true;
  userDialogNotice.value = '';
  userDialogError.value = '';
  try {
    await deleteApi(`/users/${user.id}`, csrfToken.value);
    userDialogOpen.value = false;
    await loadResource(true);
  } catch (reason) {
    userDialogError.value = safeOperationError(reason, '用户删除');
  } finally {
    userDialogBusy.value = false;
  }
}
function closeRequestDetails(): void {
  if (!requestDetailBusy.value) {
    selectedRequest.value = null;
    requestDetail.value = null;
    requestDetailError.value = '';
    requestDetailNotice.value = '';
  }
}
async function openRequestDetails(group: RequestGroup): Promise<void> {
  selectedRequest.value = group;
  requestDetail.value = null;
  requestDetailBusy.value = true;
  requestDetailError.value = '';
  requestDetailNotice.value = '';
  try {
    const response = await getApi<Record<string, any>>(`/requests/tmdb/${encodeURIComponent(String(group.media_type))}/${encodeURIComponent(String(group.tmdb_id))}`);
    requestDetail.value = response.data;
  } catch (reason) {
    if (reason instanceof ApiError && reason.status === 401) {
      requestDetailError.value = '登录已失效，请重新登录。';
    } else if (reason instanceof ApiError && reason.status === 400) {
      requestDetailError.value = '作品详情请求无效。';
    } else if (!online.value) {
      requestDetailError.value = '当前离线，无法加载作品详情。';
    } else {
      requestDetailError.value = '作品详情加载失败，请稍后重试。';
    }
  } finally {
    requestDetailBusy.value = false;
  }
}
function detailRuntime(detail: Record<string, any>): string {
  const minutes = Number(detail.runtime_minutes || 0);
  if (!minutes) return '—';
  return `${Math.floor(minutes / 60) ? `${Math.floor(minutes / 60)} 小时 ` : ''}${minutes % 60} 分钟`;
}
function peopleNames(value: unknown, limit = 99): string {
  if (!Array.isArray(value)) return '';
  return value.slice(0, limit).map((person) => {
    if (person && typeof person === 'object') return String((person as Record<string, unknown>).name || '');
    return String(person || '');
  }).filter(Boolean).join('、');
}
function requestUserNames(group: RequestGroup): string {
  return Array.isArray(group.items) ? group.items.map((item: Record<string, any>) => String(item.username || '')).filter(Boolean).join('、') : '';
}
function requestNotes(group: RequestGroup): string {
  return Array.isArray(group.items) ? group.items.map((item: Record<string, any>) => String(item.note || '')).filter(Boolean).join('；') : '';
}
function requestRejectionReasons(group: RequestGroup): string {
  return Array.isArray(group.items) ? group.items.map((item: Record<string, any>) => String(item.rejection_reason || '')).filter(Boolean).join('；') : '';
}
function notificationFeedback(category: NotificationCategory, notice: string, errorMessage = ''): void {
  const state = notificationState.value[category];
  state.notice = notice;
  state.error = errorMessage;
}

function readCooldown(): number { try { return Number(localStorage.getItem('apex-refresh-admin') || 0); } catch { return 0; } }
function writeCooldown(value: number): void { try { localStorage.setItem('apex-refresh-admin', String(value)); } catch { /* optional storage */ } }
function renderCooldown(): void {
  cooldownUntil.value = Math.max(cooldownUntil.value, readCooldown());
  cooldownRemaining.value = Math.max(0, cooldownUntil.value - Date.now());
  if (cooldownTimer) clearTimeout(cooldownTimer);
  if (cooldownRemaining.value > 0) cooldownTimer = setTimeout(renderCooldown, 250);
}
function fail(reason: unknown, fallback: string): void {
  if (reason instanceof ApiError && reason.status === 401) { window.location.assign('/login'); return; }
  error.value = reason instanceof Error ? reason.message : fallback;
}
async function loadCsrf(): Promise<void> { csrfToken.value = (await getApi<{ csrf_token: string }>('/auth/csrf')).data?.csrf_token || ''; }
async function loadDashboard(silent = false): Promise<void> {
  const requestId = ++dashboardRequestId;
  if (!silent) loading.value = true;
  error.value = '';
  try {
    const query = watchDate.value ? `?date=${encodeURIComponent(watchDate.value)}` : '';
    const response = await getApi<DashboardData>(`/dashboard${query}`);
    if (requestId !== dashboardRequestId) return;
    data.value = response.data; pageData.value = null;
    watchTimeError.value = '';
    if (data.value) { if (pollTimer) clearInterval(pollTimer); pollTimer = setInterval(() => { if (currentPath.value === '/dashboard' || currentPath.value === '/') void loadDashboardSilently(); }, Math.max(5, data.value.poll_interval) * 1000); }
  } catch (reason) {
    if (requestId !== dashboardRequestId) return;
    if (watchTimeLoading.value) watchTimeError.value = safeOperationError(reason, '观看时长加载');
    else fail(reason, '总览加载失败，请稍后重试。');
  } finally {
    if (requestId === dashboardRequestId) { loading.value = false; watchTimeLoading.value = false; }
  }
}
async function loadDashboardSilently(): Promise<void> {
  if (loading.value || watchTimeLoading.value || currentPath.value !== '/dashboard' && currentPath.value !== '/') return;
  await loadDashboard(true);
}
function changeWatchDate(value: string): void {
  watchDate.value = value;
  watchTimeLoading.value = true;
  watchTimeError.value = '';
  void loadDashboard(true);
}
function openWatchDatePicker(): void {
  const selected = data.value?.watch_time.date || data.value?.watch_time.max_date || '';
  watchDatePickerMonth.value = selected.slice(0, 7);
  watchDatePickerOpen.value = true;
}
function closeWatchDatePicker(): void {
  watchDatePickerOpen.value = false;
}
function shiftWatchDateMonth(offset: number): void {
  if (!watchDatePickerMonth.value) return;
  const [year, month] = watchDatePickerMonth.value.split('-').map(Number);
  const next = new Date(Date.UTC(year, month - 1 + offset, 1));
  watchDatePickerMonth.value = next.toISOString().slice(0, 7);
}
function selectWatchDate(day: WatchCalendarDay): void {
  if (day.disabled) return;
  changeWatchDate(day.iso);
}
function returnToToday(): void {
  const today = data.value?.watch_time.max_date || '';
  if (!today) return;
  watchDatePickerMonth.value = today.slice(0, 7);
  if (data.value?.watch_time.date !== today) changeWatchDate(today);
}
function playbackDuration(value: number | undefined): string {
  const seconds = Math.max(0, Math.floor(value || 0));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes} 分钟` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`;
}
async function loadResource(silent = false): Promise<void> {
  if (!silent) loading.value = true;
  error.value = ''; if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  try {
    let query = '';
    if (currentPath.value === '/requests') query = `?state=${encodeURIComponent(requestState.value)}`;
    if (currentPath.value === '/logs') query = `?level=${encodeURIComponent(logLevel.value)}&page=${logPage.value}`;
    const response = await getApi<Record<string, any>>(`${currentPath.value}${query}`);
    pageData.value = response.data; data.value = null;
    if (currentPath.value === '/settings') {
      settings.value = response.data?.fields || [];
      registrationServers.value = response.data?.registration_servers || [];
      registrationServerId.value = response.data?.registration_server_id ?? null;
      void loadActivationImage();
    }
  } catch (reason) {
    const fallback = currentPath.value === '/history' ? '播放历史加载失败，请稍后重试。' : '页面加载失败，请稍后重试。';
    fail(reason, fallback);
  }
  finally { if (!silent) loading.value = false; }
}
function activationImageUrl(path: string, version: unknown = Date.now()): string {
  return path ? `${path}${path.includes('?') ? '&' : '?'}v=${encodeURIComponent(String(version || Date.now()))}` : '';
}
function releaseActivationPreview(): void {
  if (activationPreviewObjectUrl) URL.revokeObjectURL(activationPreviewObjectUrl);
  activationPreviewObjectUrl = '';
}
function setActivationImageMeta(meta: ActivationImageMeta | null): void {
  activationImage.value = meta?.scan_image_url ? meta : null;
  if (!activationImage.value) activationImageDimensions.value = null;
  activationImageSrc.value = meta?.scan_image_url ? activationImageUrl(meta.scan_image_url, meta.image_updated_at) : '';
}
function readActivationImageDimensions(event: Event): void {
  const image = event.target as HTMLImageElement;
  if (image.naturalWidth && image.naturalHeight) activationImageDimensions.value = { width: image.naturalWidth, height: image.naturalHeight };
}
async function loadActivationImage(): Promise<void> {
  if (activationImageLoading.value || !online.value) return;
  activationImageLoading.value = true;
  activationImageError.value = '';
  try {
    const response = await getApi<ActivationImageMeta>('/settings/activation-image');
    setActivationImageMeta(response.data);
  } catch (reason) {
    activationImageError.value = safeOperationError(reason, '扫码图片读取');
  } finally { activationImageLoading.value = false; }
}
function chooseActivationImage(): void { activationFileInput.value?.click(); }
async function uploadActivationImage(file: File): Promise<void> {
  activationImageNotice.value = ''; activationImageError.value = '';
  if (!online.value) { activationImageError.value = '当前离线，图片上传已禁用。'; return; }
  const accepted = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
  if (!accepted.has(file.type.toLowerCase())) { activationImageError.value = '只允许上传 PNG、JPEG、WEBP 或 GIF 图片。'; return; }
  if (file.size <= 0 || file.size > 5 * 1024 * 1024) { activationImageError.value = '图片大小必须不超过 5 MiB。'; return; }
  releaseActivationPreview();
  activationImageDimensions.value = null;
  activationPreviewObjectUrl = URL.createObjectURL(file);
  activationImageSrc.value = activationPreviewObjectUrl;
  activationImageBusy.value = 'upload';
  try {
    const form = new FormData();
    form.append('csrf_token', csrfToken.value);
    form.append('file', file, file.name);
    const response = await fetch('/api/v1/settings/activation-image', {
      method: 'POST', credentials: 'same-origin', headers: { Accept: 'application/json', 'X-CSRF-Token': csrfToken.value }, body: form,
    });
    let payload: unknown = null;
    try { payload = await response.json(); } catch { payload = null; }
    const envelope = payload as { ok?: boolean; data?: ActivationImageMeta | null; error?: string } | null;
    if (!response.ok || envelope?.ok !== true) throw new ApiError(envelope?.error || `上传失败（${response.status}）`, response.status, payload);
    releaseActivationPreview();
    setActivationImageMeta(envelope.data || null);
    activationImageNotice.value = '扫码图片已上传并替换，用户端将立即显示新图片。';
  } catch (reason) {
    releaseActivationPreview();
    activationImageSrc.value = activationImage?.value?.scan_image_url ? activationImageUrl(activationImage.value.scan_image_url, activationImage.value.image_updated_at) : '';
    activationImageError.value = safeOperationError(reason, '扫码图片上传');
  } finally { activationImageBusy.value = ''; }
}
async function onActivationFileChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = '';
  if (file) await uploadActivationImage(file);
}
async function clearActivationImage(): Promise<void> {
  if (activationImageBusy.value || !online.value) { if (!online.value) activationImageError.value = '当前离线，图片清空已禁用。'; return; }
  if (!activationImage.value?.scan_image_url) { activationImageNotice.value = '当前没有可清空的扫码图片。'; return; }
  if (!window.confirm('确认清空当前扫码图片？用户端将显示空状态。')) return;
  activationImageBusy.value = 'clear'; activationImageNotice.value = ''; activationImageError.value = '';
  try {
    const response = await deleteApi<ActivationImageMeta>('/settings/activation-image', csrfToken.value);
    releaseActivationPreview(); setActivationImageMeta(response.data || null);
    activationImageNotice.value = '扫码图片已清空。';
  } catch (reason) { activationImageError.value = safeOperationError(reason, '扫码图片清空'); }
  finally { activationImageBusy.value = ''; }
}
async function changeLogLevel(value: string): Promise<void> {
  logLevel.value = value;
  logPage.value = 1;
  await loadResource();
}
async function changeLogPage(delta: number): Promise<void> {
  const next = Math.max(1, logPage.value + delta);
  if (next === logPage.value || (delta > 0 && !logsHasNext.value)) return;
  logPage.value = next;
  await loadResource();
}
async function load(): Promise<void> {
  if (!online.value) { error.value = '当前离线，无法加载管理数据。'; loading.value = false; return; }
  await loadCsrf().catch(() => undefined);
  if (currentPath.value === '/dashboard' || currentPath.value === '/') await loadDashboard(); else await loadResource();
}
async function refresh(): Promise<void> {
  if (refreshing.value || cooldownUntil.value > Date.now() || !online.value) return;
  refreshing.value = true; areaNotice.value = ''; areaError.value = ''; cooldownUntil.value = Date.now() + 10000; writeCooldown(cooldownUntil.value); renderCooldown();
  await (currentPath.value === '/dashboard' || currentPath.value === '/' ? loadDashboard(true) : loadResource(true)); refreshing.value = false; renderCooldown();
}
async function run(action: string, fn: () => Promise<void>): Promise<boolean> {
  if (busy.value || !online.value) return false;
  busy.value = action; error.value = ''; areaError.value = ''; areaNotice.value = '';
  try { await fn(); areaNotice.value = '操作已完成。'; return true; } catch (reason) { areaError.value = reason instanceof Error ? reason.message : '操作失败，请稍后重试。'; if (reason instanceof ApiError && reason.status === 401) fail(reason, '操作失败，请稍后重试。'); return false; } finally { busy.value = ''; }
}
async function stopSession(session: Record<string, any>): Promise<void> { await run('stop', async () => { await postApi(`/sessions/${session.server_id}/${encodeURIComponent(String(session.session_id || ''))}/stop`, {}, csrfToken.value); await loadDashboardSilently(); }); }
async function toggleServer(server: Record<string, any>): Promise<void> { await run(`server-${server.id}`, async () => { await patchApi(`/servers/${server.id}`, { enabled: !server.enabled }, csrfToken.value); await loadResource(true); }); }
async function syncServer(server: Record<string, any>): Promise<void> { await run(`sync-${server.id}`, async () => { await postApi(`/servers/${server.id}/sync`, {}, csrfToken.value); await loadResource(true); }); }
async function deleteServer(server: Record<string, any>): Promise<void> { if (!window.confirm(`确认删除服务器“${server.name}”？`)) return; await run(`delete-server-${server.id}`, async () => { await deleteApi(`/servers/${server.id}`, csrfToken.value); await loadResource(true); }); }
async function addServer(): Promise<void> { await run('add-server', async () => { await postApi('/servers', serverForm.value, csrfToken.value); serverForm.value = { name: '', base_url: '', api_key: '', verify_ssl: false }; await loadResource(true); }); }
async function updateUser(user: Record<string, any>, patch: Record<string, unknown>): Promise<void> { await run(`user-${user.id}`, async () => { await patchApi(`/users/${user.id}`, patch, csrfToken.value); await loadResource(true); }); }
async function deleteUser(user: Record<string, any>): Promise<void> { if (!window.confirm(`确认删除用户“${user.username}”？`)) return; await run(`delete-user-${user.id}`, async () => { await deleteApi(`/users/${user.id}`, csrfToken.value); await loadResource(true); }); }
async function addUser(): Promise<void> { await run('add-user', async () => { await postApi('/users', { ...userForm.value, server_id: Number(userForm.value.server_id) }, csrfToken.value); userForm.value = { server_id: '', username: '', password: '', ordinary_registration: true, allow_playback: true }; await loadResource(true); }); }
function groupKey(group: RequestGroup): string { return `${group.server_id}:${group.media_type}:${group.tmdb_id}`; }
async function confirmRequest(group: RequestGroup): Promise<void> {
  const succeeded = await run(`confirm-${groupKey(group)}`, async () => {
    await postApi(`/requests/${group.server_id}/${group.media_type}/${group.tmdb_id}/confirm`, {}, csrfToken.value);
    await loadResource(true);
  });
  if (succeeded && selectedRequest.value && groupKey(selectedRequest.value) === groupKey(group)) {
    selectedRequest.value = { ...selectedRequest.value, status: 'in_library' };
    requestDetailNotice.value = '已确认入库，当前列表已更新。';
  }
}
async function rejectRequest(group: RequestGroup): Promise<void> {
  const succeeded = await run(`reject-${groupKey(group)}`, async () => {
    await postApi(`/requests/${group.server_id}/${group.media_type}/${group.tmdb_id}/reject`, { reason: rejectReasons.value[groupKey(group)] || '' }, csrfToken.value);
    await loadResource(true);
  });
  if (succeeded && selectedRequest.value && groupKey(selectedRequest.value) === groupKey(group)) {
    selectedRequest.value = { ...selectedRequest.value, status: 'rejected' };
    requestDetailNotice.value = '已拒绝求片，当前列表已更新。';
  }
}
async function generateCodes(): Promise<void> { await run('generate-codes', async () => { await postApi('/codes', codeForm.value, csrfToken.value); await loadResource(true); }); }
async function deleteCode(code: Record<string, any>): Promise<void> { await run(`delete-code-${code.id}`, async () => { await deleteApi(`/codes/${code.id}`, csrfToken.value); await loadResource(true); }); }
async function saveSettings(): Promise<boolean> {
  if (busy.value || !online.value) {
    if (!online.value) {
      if (settingsTab.value === 'notifications') notificationCategories.forEach((category) => notificationFeedback(category.key, '', '当前离线，设置保存已禁用。'));
      else settingsError.value = '当前离线，设置保存已禁用。';
    }
    return false;
  }
  busy.value = 'save-settings'; settingsNotice.value = ''; settingsError.value = '';
  notificationCategories.forEach((category) => notificationFeedback(category.key, '', ''));
  const values: Record<string, unknown> = {};
  for (const row of settings.value) if ((!row.sensitive && row.kind !== 'secret') || row.value) values[row.name] = row.value;
  try {
    if (registrationEnabled.value) values.registration_server_id = registrationServerId.value ?? '';
    await patchApi('/settings', values, csrfToken.value);
    await loadResource(true);
    if (settingsTab.value === 'notifications') notificationCategories.forEach((category) => notificationFeedback(category.key, '通知设置已保存。'));
    else settingsNotice.value = '设置已保存。';
    return true;
  } catch (reason) {
    const message = safeOperationError(reason, '设置保存');
    if (settingsTab.value === 'notifications') notificationCategories.forEach((category) => notificationFeedback(category.key, '', message));
    else settingsError.value = message;
    return false;
  } finally { busy.value = ''; }
}
function settingValue(name: string): unknown { return settings.value.find((row) => row.name === name)?.value ?? ''; }
function safeOperationError(reason: unknown, action: string): string {
  if (!online.value) return '当前离线，操作已禁用。';
  if (reason instanceof ApiError && reason.status === 401) return '登录已失效，请重新登录。';
  if (reason instanceof ApiError && reason.status === 400) {
    return /csrf|安全/i.test(reason.message) ? 'CSRF 校验失败，请刷新页面后重试。' : '配置不完整或请求无效。';
  }
  if (reason instanceof ApiError && reason.status >= 500) return `${action}服务暂不可用，请稍后重试。`;
  return `${action}失败，请稍后重试。`;
}
async function testSetting(kind: 'tmdb' | 'moviepilot' | 'wecom' | 'webhook' | 'telegram', category?: NotificationCategory): Promise<boolean> {
  const localState = category ? notificationState.value[category] : null;
  if ((localState?.busy || settingsBusy.value)) return false;
  if (!online.value) {
    if (localState) localState.error = '当前离线，连接测试已禁用。';
    else settingsError.value = '当前离线，连接测试已禁用。';
    return false;
  }
  if (localState) { localState.busy = kind; localState.notice = ''; localState.error = ''; }
  else { settingsBusy.value = kind; settingsNotice.value = ''; settingsError.value = ''; }
  const payload: Record<string, unknown> = {};
  if (kind === 'tmdb') { payload.tmdb_api_key = settingValue('tmdb_api_key'); payload.tmdb_proxy_url = settingValue('tmdb_proxy_url'); }
  if (kind === 'moviepilot') { payload.moviepilot_url = settingValue('moviepilot_url'); payload.moviepilot_username = settingValue('moviepilot_username'); payload.moviepilot_password = settingValue('moviepilot_password'); }
  if (kind === 'wecom') { for (const name of ['wecom_corp_id', 'wecom_agent_id', 'wecom_secret', 'wecom_api_base_url', 'notify_proxy_url', 'notify_wecom_use_proxy']) payload[name] = settingValue(name); }
  if (kind === 'webhook') { for (const name of ['notify_webhook_url', 'notify_proxy_url', 'notify_webhook_use_proxy']) payload[name] = settingValue(name); }
  if (kind === 'telegram') { for (const name of ['telegram_bot_token', 'telegram_chat_id', 'notify_proxy_url', 'notify_telegram_use_proxy']) payload[name] = settingValue(name); }
  try {
    const response = await postApi<{ message: string }>(`/settings/${kind === 'tmdb' ? 'tmdb' : kind}/test`, payload, csrfToken.value);
    if (localState) localState.notice = response.data?.message || '连接测试成功。';
    else settingsNotice.value = response.data?.message || '连接测试成功。';
    return true;
  } catch (reason) {
    const message = safeOperationError(reason, kind === 'tmdb' ? 'TMDB 连接测试' : `${kind} 连接测试`);
    if (localState) localState.error = message;
    else settingsError.value = message;
    return false;
  } finally {
    if (localState) localState.busy = ''; else settingsBusy.value = '';
  }
}
async function saveNotificationChannel(): Promise<void> {
  const channel = notificationDialogChannel.value;
  if (!channel || notificationDialogBusy.value || !online.value) {
    if (!online.value) notificationDialogError.value = '当前离线，设置保存已禁用。';
    return;
  }
  notificationDialogBusy.value = 'save'; notificationDialogNotice.value = ''; notificationDialogError.value = '';
  const saved = await saveSettings();
  notificationDialogBusy.value = '';
  if (saved) notificationDialogNotice.value = `${channelLabel(channel)}配置已保存。`;
  else notificationDialogError.value = settingsError.value || '配置保存失败，请稍后重试。';
}
async function testNotificationChannel(): Promise<void> {
  const channel = notificationDialogChannel.value;
  if (!channel || notificationDialogBusy.value) return;
  notificationDialogBusy.value = 'test'; notificationDialogNotice.value = ''; notificationDialogError.value = '';
  try {
    const passed = await testSetting(channel);
    if (passed) notificationDialogNotice.value = `${channelLabel(channel)}连接测试已完成。`;
    else notificationDialogError.value = settingsError.value || `${channelLabel(channel)}连接测试失败，请稍后重试。`;
  } catch (reason) {
    notificationDialogError.value = safeOperationError(reason, `${channelLabel(channel)}连接测试`);
  } finally { notificationDialogBusy.value = ''; }
}
async function clearNotificationChannel(): Promise<void> {
  const channel = notificationDialogChannel.value;
  if (!channel || notificationDialogBusy.value || !online.value) {
    if (!online.value) notificationDialogError.value = '当前离线，清空配置已禁用。';
    return;
  }
  if (!window.confirm(`确认清空${channelLabel(channel)}配置并关闭相关通知？`)) return;
  notificationDialogBusy.value = 'clear'; notificationDialogNotice.value = ''; notificationDialogError.value = '';
  const values: Record<string, unknown> = {};
  for (const field of channelFields(channel)) values[field.name] = field.kind === 'bool' ? false : '';
  for (const category of notificationCategories) values[`${category.key}.${channel}`] = false;
  try {
    await patchApi('/settings', values, csrfToken.value);
    await loadResource(true);
    notificationDialogNotice.value = `${channelLabel(channel)}配置已清空，相关通知已关闭。`;
  } catch (reason) { notificationDialogError.value = safeOperationError(reason, `${channelLabel(channel)}配置清空`); }
  finally { notificationDialogBusy.value = ''; }
}
function handleMenuError(reason: unknown, action: string): void {
  menuError.value = safeOperationError(reason, `企业微信菜单${action}`);
}
async function readWecomMenu(): Promise<void> {
  if (menuBusy.value) return;
  if (!online.value) { menuError.value = '当前离线，菜单读取已禁用。'; return; }
  if (!wecomConfigured.value) { menuNotice.value = ''; menuError.value = '企业微信尚未配置完整，请先填写企业 ID、AgentID 和应用密钥。'; return; }
  menuBusy.value = 'read'; menuNotice.value = ''; menuError.value = '';
  try {
    const response = await getApi<{ menu?: Record<string, unknown> }>('/settings/wecom/menu');
    wecomMenu.value = response.data?.menu || null;
    menuNotice.value = '已读取企业微信当前菜单。';
  } catch (reason) { handleMenuError(reason, '读取'); }
  finally { menuBusy.value = ''; }
}
async function syncWecomMenu(): Promise<void> {
  if (menuBusy.value) return;
  if (!online.value) { menuError.value = '当前离线，菜单同步已禁用。'; return; }
  if (!wecomConfigured.value) { menuNotice.value = ''; menuError.value = '企业微信尚未配置完整，请先填写企业 ID、AgentID 和应用密钥。'; return; }
  menuBusy.value = 'sync'; menuNotice.value = ''; menuError.value = '';
  try {
    await postApi<{ message?: string }>('/settings/wecom/menu/sync', {}, csrfToken.value);
    menuNotice.value = '企业微信菜单已同步。';
  } catch (reason) { handleMenuError(reason, '同步'); }
  finally { menuBusy.value = ''; }
}
async function deleteWecomMenu(): Promise<void> {
  if (menuBusy.value) return;
  if (!online.value) { menuError.value = '当前离线，菜单删除已禁用。'; return; }
  if (!wecomConfigured.value) { menuNotice.value = ''; menuError.value = '企业微信尚未配置完整，请先填写企业 ID、AgentID 和应用密钥。'; return; }
  if (!window.confirm('确认删除企业微信当前应用菜单？')) return;
  menuBusy.value = 'delete'; menuNotice.value = ''; menuError.value = '';
  try {
    await postApi<{ message?: string }>('/settings/wecom/menu/delete', {}, csrfToken.value);
    wecomMenu.value = null;
    menuNotice.value = '企业微信菜单已删除。';
  } catch (reason) { handleMenuError(reason, '删除'); }
  finally { menuBusy.value = ''; }
}
async function logout(): Promise<void> { await run('logout', async () => { await postApi('/auth/logout', {}, csrfToken.value); window.location.assign('/login'); }); }
function updateOnline(): void { online.value = navigator.onLine; if (online.value && !data.value && !pageData.value) void load(); }
function goBack(): void {
  navigateBack('admin');
}
function closeMore(): void {
  moreOpen.value = false;
  moreSearchOpen.value = false;
  moreSearchQuery.value = '';
}
function toggleMore(): void { moreOpen.value = !moreOpen.value; }
function toggleMoreSearch(): void {
  moreSearchOpen.value = !moreSearchOpen.value;
  if (!moreSearchOpen.value) moreSearchQuery.value = '';
}
function cleanup(): void { if (pollTimer) clearInterval(pollTimer); if (cooldownTimer) clearTimeout(cooldownTimer); document.body.classList.remove('more-open'); window.removeEventListener('online', updateOnline); window.removeEventListener('offline', updateOnline); }
watch(moreOpen, (open) => { document.body.classList.toggle('more-open', open); });
onMounted(() => { document.title = `${pageTitle.value} · Emby Apex`; cooldownUntil.value = readCooldown(); renderCooldown(); window.addEventListener('online', updateOnline); window.addEventListener('offline', updateOnline); void load(); });
onBeforeUnmount(cleanup);
</script>

<template>
  <div class="admin-layout">
    <aside class="sidebar" aria-label="管理导航">
      <div class="brand-block"><img :src="'/static/brand-logo.png'" alt=""><div class="brand-copy"><strong>Emby Apex</strong><span>媒体服务运营台</span></div></div>
      <AppNav class="sidebar-nav" :items="nav" :active-path="currentPath" aria-label="管理导航" />
      <div class="sidebar-foot"><button class="sm" type="button" :disabled="refreshing" @click="refresh"><RefreshCw :size="15" /><span class="nav-label">{{ cooldownLabel }}</span></button><button class="sm" type="button" :disabled="busy === 'logout'" @click="logout"><LogOut :size="15" /><span class="nav-label">退出登录</span></button></div>
    </aside>
    <div class="admin-main">
      <header class="mobile-topbar"><button class="back-button" type="button" aria-label="返回上一页" title="返回上一页" @click="goBack"><ArrowLeft :size="20" /></button><div class="brand-block"><img :src="'/static/logoicon.png'" alt=""><div class="brand-copy"><strong>Emby Apex</strong><span>运营台</span></div></div><button class="mobile-menu" type="button" aria-haspopup="dialog" :aria-expanded="moreOpen" aria-controls="mobile-more-dialog" aria-label="打开更多菜单" @click="toggleMore"><MoreHorizontal :size="20" /></button></header>
      <main class="page-container">
        <template v-if="currentPath !== '/settings'">
        <div class="page-head"><div class="row"><button class="back-button hide-sm" type="button" aria-label="返回上一页" title="返回上一页" @click="goBack"><ArrowLeft :size="19" /></button><div><h1>{{ pageTitle }}</h1><p class="small muted admin-brand-subtitle">Emby Apex 管理端</p></div></div><button class="secondary" type="button" :disabled="refreshing || cooldownRemaining > 0 || !online" @click="refresh"><LoaderCircle v-if="refreshing" class="spin" :size="16" /><RefreshCw v-else :size="16" />{{ refreshing ? '刷新中' : cooldownLabel }}</button></div>
        <div v-if="error && currentPath !== '/history' && currentPath !== '/settings'" class="msg error" role="alert"><AlertCircle :size="18" /><span>{{ error }}</span><a v-if="error.includes('登录')" href="/login" class="button-link">重新登录</a></div>
        <div v-if="!online" class="msg notice" role="status"><WifiOff :size="18" />当前离线，管理数据刷新已禁用。</div>
        <div v-if="(areaNotice || areaError) && currentPath !== '/settings'" :class="['msg', areaError ? 'error' : 'notice']" role="status"><AlertCircle v-if="areaError" :size="18" /><CheckCircle2 v-else :size="18" />{{ areaError || areaNotice }}</div>
        <div v-if="loading" class="panel vue-state"><LoaderCircle class="spin" :size="22" /><span>正在加载…</span></div>

        <template v-else-if="data">
          <div class="cards"><div class="card"><div class="n">{{ data.summary.playing }}</div><div class="l">正在播放</div></div><div class="card"><div class="n">{{ data.summary.servers }}</div><div class="l">服务器</div></div><div class="card"><div class="n">{{ data.summary.users }}</div><div class="l">用户总数</div></div><div class="card"><div class="n">{{ data.summary.disabled }}</div><div class="l">已停用</div></div><div class="card"><div class="n">{{ data.summary.expiring }}</div><div class="l">即将到期</div></div><div class="card"><div class="n">{{ data.watch_time.hours }}h</div><div class="l">{{ data.watch_time.date }} 观看时长</div></div></div>
          <section class="panel"><div class="row" style="justify-content:space-between"><h2>正在播放</h2><span class="small muted">每 {{ data.poll_interval }} 秒自动刷新</span></div><div v-if="!data.sessions.length" class="empty">当前没有正在播放的会话。</div><table v-else><thead><tr><th>用户</th><th>内容</th><th>客户端</th><th>进度</th><th>操作</th></tr></thead><tbody><tr v-for="session in data.sessions" :key="`${session.server_id}:${session.session_id}`"><td>{{ session.username || '—' }}</td><td>{{ session.item_name || session.title || '—' }}</td><td>{{ session.client || session.device_name || '—' }}</td><td>{{ session.progress_percent ?? '—' }}{{ session.progress_percent != null ? '%' : '' }}</td><td><button class="danger sm" type="button" @click="stopSession(session)">停止</button></td></tr></tbody></table></section>
          <section class="panel"><div class="vue-section-heading watch-time-heading"><div><h2>用户观看时长排行</h2><p class="hint">只统计已结束的播放记录，按观看时长排序。</p></div><div class="watch-date-controls"><div class="watch-date-picker"><span class="watch-date-label">选择日期</span><button class="secondary sm watch-date-trigger" type="button" aria-haspopup="dialog" :aria-expanded="watchDatePickerOpen" @click="openWatchDatePicker">{{ data.watch_time.date }}</button><div v-if="watchDatePickerOpen" class="watch-date-popover" role="dialog" aria-label="选择观看时长日期"><div class="watch-date-popover-head"><button class="icon-button" type="button" aria-label="上个月" title="上个月" :disabled="watchDatePreviousDisabled" @click="shiftWatchDateMonth(-1)"><ChevronLeft :size="17" /></button><strong>{{ watchDateMonthLabel }}</strong><button class="icon-button" type="button" aria-label="下个月" title="下个月" :disabled="watchDateNextDisabled" @click="shiftWatchDateMonth(1)"><ChevronRight :size="17" /></button></div><div class="watch-date-weekdays"><span v-for="weekday in watchWeekdays" :key="weekday">{{ weekday }}</span></div><div class="watch-date-grid"><button v-for="day in watchDateDays" :key="day.iso" class="watch-date-day" :class="{ muted: !day.inMonth, selected: day.iso === data.watch_time.date }" type="button" :disabled="day.disabled" @click="selectWatchDate(day)">{{ day.label }}</button></div><div class="watch-date-actions"><button class="secondary sm" type="button" :disabled="watchTimeLoading || data.watch_time.date === data.watch_time.max_date" @click="returnToToday">今天</button><button class="primary sm" type="button" @click="closeWatchDatePicker">完成</button></div></div></div></div></div><div v-if="watchTimeError" class="msg error" role="alert"><AlertCircle :size="16" />{{ watchTimeError }}</div><div v-else-if="!data.watch_time.users.length" class="empty">{{ data.watch_time.date }} 暂无已完成的播放记录。</div><table v-else><thead><tr><th>用户</th><th>服务器</th><th>播放次数</th><th>观看时长</th></tr></thead><tbody><tr v-for="(user, index) in data.watch_time.users" :key="`${user.server_id ?? user.username}:${user.emby_user_id ?? index}`"><td>{{ user.username || '—' }}</td><td>{{ user.server_name || '—' }}</td><td>{{ user.plays }}</td><td>{{ playbackDuration(user.seconds ?? user.hours * 3600) }}</td></tr></tbody></table></section>
          <section class="panel"><div class="row" style="justify-content:space-between"><h2>播放趋势</h2><span class="small muted">最近 7 天</span></div><div class="chart-bars" aria-label="最近 7 天播放次数"><div v-for="point in data.trend" :key="point.date" class="chart-bar" :title="`${point.date}：${point.plays} 次`"><i :style="{ height: `${(point.plays / maxPlays) * 100}%` }"></i><span>{{ point.date.slice(5) }}</span></div></div></section>
          <section class="panel"><h2>服务器状态</h2><div v-if="!data.servers.length" class="empty">还没有添加服务器。</div><table v-else><thead><tr><th>名称</th><th>地址</th><th>版本</th><th>状态</th><th>最近成功</th></tr></thead><tbody><tr v-for="server in data.servers" :key="server.id"><td>{{ server.name }}</td><td class="small muted">{{ server.base_url || '—' }}</td><td>{{ server.server_version || '—' }}</td><td><span class="badge" :class="server.status === 'ok' ? 'ok' : server.status === 'error' ? 'off' : ''">{{ server.status === 'ok' ? '正常' : server.status === 'error' ? '异常' : '已暂停' }}</span></td><td class="small muted">{{ formatDateTime(server.last_ok_at) }}</td></tr></tbody></table></section>
          <section class="panel"><h2>最近动作</h2><div v-if="!data.logs.length" class="empty">暂无记录</div><table v-else><tbody><tr v-for="log in data.logs" :key="`${log.created_at}:${log.action}`"><td class="small muted">{{ formatDateTime(log.created_at) }}</td><td>{{ ['user_policy_updated', 'emby_policy_updated', 'policy_updated'].includes(log.action) ? '播放策略' : log.action }}</td><td>{{ log.detail || '—' }}</td></tr></tbody></table></section>
        </template>

        <template v-else-if="pageData && currentPath === '/users'">
          <section class="panel"><h2>创建用户</h2><form class="grid" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr))" @submit.prevent="addUser"><label>服务器<select v-model="userForm.server_id" required><option value="" disabled>选择服务器</option><option v-for="server in servers" :key="server.id" :value="server.id">{{ server.name }}</option></select></label><label>用户名<input v-model="userForm.username" required></label><label>密码<input v-model="userForm.password" type="password" required></label><label class="row"><input v-model="userForm.ordinary_registration" type="checkbox">普通注册</label><label class="row"><input v-model="userForm.allow_playback" type="checkbox">允许播放</label><button class="primary" type="submit">创建用户</button></form></section>
          <section class="panel"><div class="row" style="justify-content:space-between"><h2>用户列表</h2><span class="muted small">{{ users.length }} 个用户</span></div><div v-if="!users.length" class="empty">暂无用户。</div><div v-else class="user-card-grid"><article v-for="user in users" :key="user.id" class="user-card" role="button" tabindex="0" @click="openUserDetails(user)" @keydown.enter="openUserDetails(user)"><div class="user-card-header"><div class="user-avatar"><Users :size="20" /></div><div class="user-card-title"><h3>{{ user.username }}</h3><span class="muted small">{{ user.server_name || '未分配服务器' }}</span></div><span class="badge" :class="user.is_disabled ? 'off' : 'ok'">{{ user.is_disabled ? '已停用' : '正常' }}</span></div><div class="user-card-facts"><span><ShieldCheck :size="14" />{{ user.playback_enabled ? '允许播放' : '禁止播放' }}</span><span><CalendarClock :size="14" />{{ user.expires_at ? formatDateTime(user.expires_at) : '永久账户' }}</span><span>{{ user.portal_enabled ? '用户端已开通' : '用户端未开通' }}</span></div><div class="user-card-actions" @click.stop><button class="sm" type="button" :disabled="user.is_admin" :title="user.is_admin ? '管理员账号不可停用' : ''" @click="updateUser(user, { disabled: !user.is_disabled })">{{ user.is_disabled ? '启用' : '停用' }}</button><button class="sm" type="button" @click="updateUser(user, { playback_enabled: !user.playback_enabled })">切换播放</button><button v-if="!user.is_admin" class="danger sm icon-action" type="button" aria-label="删除用户" title="删除用户" @click="deleteUser(user)"><Trash2 :size="14" /></button><span v-else class="muted small">管理员账号</span></div></article></div></section>
        </template>

        <template v-else-if="pageData && currentPath === '/servers'">
          <section class="panel"><h2>添加服务器</h2><form class="grid" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr))" @submit.prevent="addServer"><label>名称<input v-model="serverForm.name" required></label><label>地址<input v-model="serverForm.base_url" type="url" placeholder="https://emby.example" required></label><label>API Key<input v-model="serverForm.api_key" type="password" required></label><label class="row"><input v-model="serverForm.verify_ssl" type="checkbox">验证 TLS</label><button class="primary" type="submit">添加服务器</button></form></section>
          <section class="panel"><h2>服务器列表</h2><div v-if="!servers.length" class="empty">暂无服务器。</div><table v-else><thead><tr><th>名称</th><th>地址</th><th>状态</th><th>版本</th></tr></thead><tbody><tr v-for="server in servers" :key="server.id"><td class="list-name-cell"><div>{{ server.name }}</div><div class="list-item-actions"><button class="sm" type="button" @click="toggleServer(server)">{{ server.enabled ? '暂停' : '启用' }}</button><button class="sm" type="button" @click="syncServer(server)">同步</button><button class="danger sm icon-action" type="button" aria-label="删除服务器" title="删除服务器" @click="deleteServer(server)"><Trash2 :size="14" /></button></div></td><td class="small muted">{{ server.base_url }}</td><td><span class="badge" :class="server.enabled ? 'ok' : 'off'">{{ server.enabled ? '启用' : '暂停' }}</span></td><td>{{ server.server_version || '—' }}</td></tr></tbody></table></section>
        </template>

        <template v-else-if="pageData && currentPath === '/requests'">
          <section class="panel"><div class="row request-filter-row"><button v-for="state in ['pending','in_library','rejected','all']" :key="state" class="sm" :class="{ secondary: requestState === state }" type="button" @click="requestState = state; loadResource()">{{ state === 'pending' ? '待处理' : state === 'in_library' ? '已入库' : state === 'rejected' ? '已拒绝' : '全部' }}</button></div></section>
          <div v-if="!requestGroups.length" class="panel empty"><Film :size="28" /><span>暂无求片记录。</span></div><div v-else class="request-admin-grid"><article v-for="group in requestGroups" :key="groupKey(group)" class="request-admin-card request-admin-card-clickable" role="button" tabindex="0" @click="openRequestDetails(group)" @keydown.enter="openRequestDetails(group)"><div class="request-head"><img class="request-poster" :src="group.poster_local_url || group.poster_url || '/static/logoicon.png'" :alt="group.title" loading="lazy"><div class="request-main"><h2>{{ group.title }}</h2><p class="meta">{{ group.media_type === 'movie' ? '电影' : '电视剧' }} · {{ group.year || '年份未知' }} · TMDB {{ group.tmdb_id }}</p><p class="overview">{{ group.overview || '暂无简介' }}</p><div class="request-card-meta"><span class="badge" :class="group.status === 'pending' ? 'pending' : group.status === 'in_library' ? 'ok' : 'off'">{{ group.status === 'pending' ? '待处理' : group.status === 'in_library' ? '已入库' : '已拒绝' }}</span><span class="muted small">{{ group.items?.length || 0 }} 位用户求片</span></div></div></div><div class="request-users request-card-users"><div v-for="item in (group.items || []).slice(0, 3)" :key="item.id" class="request-user-line"><span>{{ item.username }}</span><span class="muted">{{ item.note || '无备注' }}</span><time>{{ formatDateTime(item.created_at) }}</time></div><span v-if="(group.items || []).length > 3" class="muted small">还有 {{ group.items.length - 3 }} 条记录</span></div><div v-if="group.status === 'pending'" class="request-actions row" @click.stop><button class="primary sm" type="button" @click="confirmRequest(group)">确认入库</button><form class="reject-form" @submit.prevent="rejectRequest(group)"><input v-model="rejectReasons[groupKey(group)]" maxlength="1000" placeholder="拒绝原因（可选）"><button class="danger sm" type="submit">拒绝</button></form></div></article></div>
        </template>

        <template v-else-if="pageData && currentPath === '/codes'">
          <section class="panel"><h2>生成兑换码</h2><form class="grid" style="grid-template-columns:repeat(auto-fit,minmax(130px,1fr))" @submit.prevent="generateCodes"><label>时长<input v-model.number="codeForm.amount" type="number" min="1" required></label><label>单位<select v-model="codeForm.unit"><option value="minute">分钟</option><option value="hour">小时</option><option value="day">天</option><option value="year">年</option></select></label><label>数量<input v-model.number="codeForm.quantity" type="number" min="1" max="100" required></label><label>备注<input v-model="codeForm.note"></label><button class="primary" type="submit">生成</button></form></section>
          <section class="panel"><h2>兑换码列表</h2><div v-if="!codes.length" class="empty">暂无兑换码。</div><table v-else><thead><tr><th>兑换码</th><th>时长</th><th>状态</th><th>使用者</th><th>操作</th></tr></thead><tbody><tr v-for="code in codes" :key="code.id"><td><code>{{ code.code }}</code></td><td>{{ code.amount }} {{ code.unit }}</td><td>{{ code.used ? '已使用' : '未使用' }}</td><td>{{ code.used_by_username || '—' }}</td><td><button v-if="!code.used" class="danger sm" type="button" @click="deleteCode(code)"><Trash2 :size="14" /></button></td></tr></tbody></table></section>
        </template>

        <template v-else-if="currentPath === '/history'"><section class="panel history-panel"><div class="vue-section-heading"><div><h2>播放历史</h2><p class="hint">最近播放记录按时间倒序显示。</p></div><button class="secondary sm" type="button" :disabled="loading" @click="loadResource()"><RefreshCw :class="{ spin: loading }" :size="15" />刷新</button></div><div v-if="loading" class="vue-state compact"><LoaderCircle class="spin" :size="22" />正在加载历史…</div><div v-else-if="error" class="msg error" role="alert"><AlertCircle :size="18" />{{ error }}<a v-if="error.includes('登录')" href="/login" class="button-link">重新登录</a></div><div v-else-if="!history.length" class="empty">暂无播放记录。</div><table v-else><thead><tr><th>用户</th><th>内容</th><th>客户端</th><th>开始时间</th><th>观看秒数</th></tr></thead><tbody><tr v-for="row in history" :key="row.id"><td>{{ row.username || '—' }}</td><td>{{ row.item_name || '—' }}</td><td>{{ row.client || '—' }}</td><td>{{ formatDateTime(row.started_at) }}</td><td>{{ Math.round(row.watched_seconds || 0) }}</td></tr></tbody></table></section></template>
        <template v-else-if="pageData && currentPath === '/logs'"><section class="panel"><div class="vue-section-heading"><div><h2>操作日志</h2><p class="hint">显示全部日志，可按级别筛选。</p></div><label class="small">级别<select :value="logLevel" @change="changeLogLevel(($event.target as HTMLSelectElement).value)"><option value="all">全部</option><option value="info">INFO</option><option value="warning">WARNING</option><option value="error">ERROR</option></select></label></div><div v-if="!logs.length" class="empty">暂无日志。</div><table v-else><thead><tr><th>时间</th><th>级别</th><th>动作</th><th>详情</th></tr></thead><tbody><tr v-for="row in logs" :key="row.id"><td>{{ formatDateTime(row.created_at) }}</td><td>{{ row.level }}</td><td>{{ row.action }}</td><td>{{ row.detail || '—' }}</td></tr></tbody></table><div class="row" style="justify-content:flex-end;gap:8px;margin-top:12px"><button class="secondary sm" type="button" :disabled="logPage <= 1 || loading" @click="changeLogPage(-1)">上一页</button><span class="small muted">第 {{ pageData.page || 1 }} 页</span><button class="secondary sm" type="button" :disabled="!logsHasNext || loading" @click="changeLogPage(1)">下一页</button></div></section></template>
        <template v-else-if="currentPath === '/settings' && false"><section class="panel settings-panel"><div class="vue-section-heading"><div><h2>运行设置</h2><p class="hint">保存后立即应用；敏感字段留空表示保持原值。</p></div><button class="primary" type="submit" form="settings-form" :disabled="busy === 'save-settings'"><LoaderCircle v-if="busy === 'save-settings'" class="spin" :size="16" />保存设置</button></div><div v-if="error" class="msg error" role="alert"><AlertCircle :size="18" />{{ error }}<a v-if="error.includes('登录')" href="/login" class="button-link">重新登录</a></div><div v-if="settingsError" class="msg error" role="alert"><AlertCircle :size="18" />{{ settingsError }}</div><div v-if="settingsNotice" class="msg notice" role="status"><CheckCircle2 :size="18" />{{ settingsNotice }}</div><form id="settings-form" class="grid settings-grid" @submit.prevent="saveSettings"><label v-for="row in settings" :key="row.name" :class="row.kind === 'bool' ? 'row' : ''"><template v-if="row.kind === 'bool'"><input v-model="row.value" type="checkbox">{{ row.label }}</template><template v-else><span>{{ row.label }}</span><input v-model="row.value" :type="row.kind === 'secret' ? 'password' : row.kind === 'int' ? 'number' : 'text'" :min="row.min ?? undefined" :max="row.max ?? undefined" :placeholder="row.kind === 'secret' && row.configured ? '已配置，留空保持' : ''"></template><small v-if="row.hint" class="muted">{{ row.hint }}</small></label></form><div class="settings-actions"><button class="secondary sm" type="button" :disabled="settingsBusy !== ''" @click="testSetting('tmdb')"><LoaderCircle v-if="settingsBusy === 'tmdb'" class="spin" :size="15" />测试 TMDB/代理</button><button class="secondary sm" type="button" :disabled="settingsBusy !== ''" @click="testSetting('webhook')"><LoaderCircle v-if="settingsBusy === 'webhook'" class="spin" :size="15" />测试 Webhook</button><button class="secondary sm" type="button" :disabled="settingsBusy !== ''" @click="testSetting('telegram')"><LoaderCircle v-if="settingsBusy === 'telegram'" class="spin" :size="15" />测试 Telegram</button><button class="secondary sm" type="button" :disabled="settingsBusy !== ''" @click="testSetting('wecom')"><LoaderCircle v-if="settingsBusy === 'wecom'" class="spin" :size="15" />测试企业微信</button></div><section class="wecom-menu-card" aria-labelledby="wecom-menu-title"><div class="vue-section-heading"><div><h3 id="wecom-menu-title">企业微信菜单</h3><p class="hint">同步、读取或删除当前应用菜单；不会显示 Secret、Token 等凭据。</p></div><Settings :size="18" /></div><p v-if="!wecomConfigured" class="msg notice" role="status"><AlertCircle :size="16" />企业微信尚未配置完整，菜单操作暂不可用。</p><div class="settings-actions"><button class="secondary sm" type="button" :disabled="menuBusy !== '' || !online || !wecomConfigured" @click="readWecomMenu"><LoaderCircle v-if="menuBusy === 'read'" class="spin" :size="15" /><RefreshCw v-else :size="15" />读取菜单</button><button class="primary sm" type="button" :disabled="menuBusy !== '' || !online || !wecomConfigured" @click="syncWecomMenu"><LoaderCircle v-if="menuBusy === 'sync'" class="spin" :size="15" />同步菜单</button><button class="danger sm" type="button" :disabled="menuBusy !== '' || !online || !wecomConfigured" @click="deleteWecomMenu"><LoaderCircle v-if="menuBusy === 'delete'" class="spin" :size="15" />删除菜单</button></div><div v-if="menuError" class="msg error" role="alert"><AlertCircle :size="16" />{{ menuError }}<a v-if="menuError.includes('登录')" href="/login" class="button-link">重新登录</a></div><div v-else-if="menuNotice" class="msg notice" role="status"><CheckCircle2 :size="16" />{{ menuNotice }}</div><div v-if="menuBusy === 'read'" class="vue-state compact"><LoaderCircle class="spin" :size="20" />正在读取菜单…</div><div v-else-if="wecomMenu && wecomMenuItems.length" class="wecom-menu-list"><div v-for="item in wecomMenuItems" :key="item.name" class="wecom-menu-item"><strong>{{ item.name }}</strong><span v-if="item.children.length" class="muted">{{ item.children.join('、') }}</span><span v-else class="muted">暂无子菜单</span></div></div><p v-else-if="wecomMenu && !wecomMenuItems.length" class="empty compact">当前未配置菜单。</p></section></section></template>
        </template>
        <template v-if="currentPath === '/settings'">
          <section class="panel settings-panel settings-panel-redesign">
            <div class="vue-section-heading settings-heading">
              <div><h2>运行设置</h2><p class="hint">常规参数和通知渠道分开管理；敏感字段留空表示保持原值。</p></div>
              <button class="primary" type="button" :disabled="busy === 'save-settings' || loading" @click="saveSettings"><LoaderCircle v-if="busy === 'save-settings'" class="spin" :size="16" />保存设置</button>
            </div>
            <div class="settings-tabs" role="tablist" aria-label="设置分类">
              <button type="button" role="tab" :aria-selected="settingsTab === 'general'" :class="{ on: settingsTab === 'general' }" @click="settingsTab = 'general'"><Settings :size="16" />常规设置</button>
              <button type="button" role="tab" :aria-selected="settingsTab === 'notifications'" :class="{ on: settingsTab === 'notifications' }" @click="settingsTab = 'notifications'"><Bell :size="16" />通知</button>
            </div>
            <template v-if="settingsTab === 'general'">
              <div v-if="error" class="msg error" role="alert"><AlertCircle :size="18" />{{ error }}<a v-if="error.includes('登录')" href="/login" class="button-link">重新登录</a></div>
              <div v-if="settingsError" class="msg error" role="alert"><AlertCircle :size="18" />{{ settingsError }}<a v-if="settingsError.includes('登录')" href="/login" class="button-link">重新登录</a></div>
              <div v-if="settingsNotice" class="msg notice" role="status"><CheckCircle2 :size="18" />{{ settingsNotice }}</div>
              <form id="settings-form" class="grid settings-grid" @submit.prevent="saveSettings">
                <label v-for="row in generalSettings" :key="row.name" :class="row.kind === 'bool' ? 'row' : ''">
                  <template v-if="row.kind === 'bool'"><input v-model="row.value" type="checkbox"><span>{{ settingLabel(row) }}</span></template>
                  <template v-else><span>{{ settingLabel(row) }}</span><input v-model="row.value" :type="row.kind === 'secret' ? 'password' : row.kind === 'int' ? 'number' : 'text'" :min="row.min ?? undefined" :max="row.max ?? undefined" :placeholder="settingPlaceholder(row)"></template>
                  <small v-if="settingHint(row)" class="muted">{{ settingHint(row) }}</small>
                </label>
                <label v-if="registrationEnabled" class="registration-target-field">
                  <span>默认注册服务器</span>
                  <select v-model="registrationServerId" :disabled="!registrationServers.length">
                    <option :value="null">请选择服务器</option>
                    <option v-for="server in registrationServers" :key="server.id" :value="server.id">{{ server.name }}</option>
                  </select>
                  <small class="muted">开放注册后，新用户将在此服务器创建；请先选择并保存。</small>
                </label>
                <p v-if="registrationEnabled && !registrationServers.length" class="msg notice">暂无启用的服务器，开放注册暂不可用。</p>
               </form>
               <section class="activation-image-settings" aria-labelledby="activation-image-title">
                 <div class="vue-section-heading"><div><h3 id="activation-image-title">扫码图片</h3><p class="hint">上传后用户端“扫码”标签会显示这张图片；建议尺寸约 828 × 1124，比例约 3:4。</p></div><ImagePlus :size="20" /></div>
                 <input ref="activationFileInput" class="sr-only" type="file" accept="image/png,image/jpeg,image/webp,image/gif" @change="onActivationFileChange">
                 <div v-if="activationImageLoading" class="vue-state compact"><LoaderCircle class="spin" :size="20" />正在读取扫码图片…</div>
                 <div v-else class="activation-image-layout">
                   <div class="activation-image-preview"><div v-if="activationImageSrc" class="activation-image-frame"><img :src="activationImageSrc" alt="扫码图片预览" @load="readActivationImageDimensions" @error="activationImageError = '图片预览加载失败，请重新上传或刷新页面。'"></div><div v-else class="activation-image-empty"><ImagePlus :size="28" /><strong>暂无扫码图片</strong><span>上传后可在此预览</span></div></div>
                   <div class="activation-image-meta"><dl><div><dt>当前状态</dt><dd>{{ activationImage ? '已配置' : '未配置' }}</dd></div><div><dt>实际尺寸</dt><dd>{{ activationImageDimensions ? activationImageDimensions.width + ' × ' + activationImageDimensions.height + 'px' : activationImage ? '读取中…' : '—' }}</dd></div><div><dt>当前比例</dt><dd>{{ activationImageDimensions ? (activationImageDimensions.width / activationImageDimensions.height).toFixed(2) + ':1' : '—' }}</dd></div><div><dt>目标比例</dt><dd>828 × 1124 · 约 3:4</dd></div><div><dt>文件信息</dt><dd>{{ activationImage ? (activationImage.mime || '图片') + ' · ' + Math.round((activationImage.size || 0) / 1024) + ' KB' : 'PNG / JPEG / WEBP / GIF，≤ 5 MiB' }}</dd></div></dl><div class="settings-actions activation-image-actions"><button class="primary sm" type="button" :disabled="activationImageBusy !== '' || !online" @click="chooseActivationImage"><LoaderCircle v-if="activationImageBusy === 'upload'" class="spin" :size="15" /><Upload v-else :size="15" />{{ activationImage ? '替换图片' : '上传图片' }}</button><button v-if="activationImage" class="danger sm" type="button" :disabled="activationImageBusy !== '' || !online" @click="clearActivationImage"><LoaderCircle v-if="activationImageBusy === 'clear'" class="spin" :size="15" /><Trash2 v-else :size="15" />清空图片</button></div></div>
                 </div>
                 <div v-if="activationImageError" class="msg error" role="alert"><AlertCircle :size="16" />{{ activationImageError }}<a v-if="activationImageError.includes('登录')" href="/login" class="button-link">重新登录</a></div><div v-else-if="activationImageNotice" class="msg notice" role="status"><CheckCircle2 :size="16" />{{ activationImageNotice }}</div>
               </section>
               <div class="settings-actions"><button class="secondary sm" type="button" :disabled="settingsBusy !== '' || !online" @click="testSetting('tmdb')"><LoaderCircle v-if="settingsBusy === 'tmdb'" class="spin" :size="15" />测试 TMDB/代理</button><button class="secondary sm" type="button" :disabled="settingsBusy !== '' || !online" @click="testSetting('moviepilot')"><LoaderCircle v-if="settingsBusy === 'moviepilot'" class="spin" :size="15" />测试 MoviePilot</button></div>
            </template>
             <template v-else>
               <div v-if="!online" class="msg notice" role="status"><WifiOff :size="17" />当前离线，通知测试、保存和菜单操作已禁用。</div>
               <section class="notification-channel-grid" aria-label="通知渠道">
                 <button v-for="channel in notificationChannels" :key="channel.key" type="button" class="notification-provider-card" @click="openNotificationDialog(channel.key)">
                   <span class="notification-provider-icon"><component :is="channel.icon" :size="22" /></span>
                   <span class="notification-provider-copy"><strong>{{ channel.label }}</strong><small>{{ channel.description }}</small></span>
                   <span class="notification-provider-status"><span class="notification-light" :class="{ active: channelEnabled(channel.key) }" aria-hidden="true"></span><span>{{ channelEnabled(channel.key) ? '已启用' : '已停用' }}</span><span class="channel-config-status" :class="{ configured: channelConfigured(channel.key) }">{{ channelConfigured(channel.key) ? '已配置' : '未配置' }}</span></span>
                 </button>
               </section>
               <section class="notification-events-card">
                 <div class="vue-section-heading"><div><h3>通知事件</h3><p class="hint">选择要发送的事件类型；渠道凭据在上方卡片中配置。</p></div><Bell :size="19" /></div>
                 <div class="notification-event-grid">
                   <div v-for="category in notificationCategories" :key="category.key" class="notification-event-row">
                     <div><strong>{{ category.label }}</strong><small>{{ category.description }}</small></div>
                     <label v-for="channel in notificationChannels" :key="channel.key" class="notification-switch"><input v-for="toggle in rowsFor([category.key + '.' + channel.key])" :key="toggle.name" v-model="toggle.value" type="checkbox" @change="setNotificationToggle(toggle, channel.key)"><span>{{ channel.label }}</span></label>
                   </div>
                 </div>
                 <div v-if="settingsError" class="msg error notification-feedback" role="alert"><AlertCircle :size="16" />{{ settingsError }}</div>
                 <div v-if="settingsNotice" class="msg notice notification-feedback" role="status"><CheckCircle2 :size="16" />{{ settingsNotice }}</div>
               </section>
               <section v-if="showsSharedChannelConfig('general')" class="notification-shared-card">
                 <label v-for="field in rowsFor(sharedNotificationFields)" :key="field.name"><span>{{ settingLabel(field) }}</span><input v-model="field.value" :type="field.kind === 'secret' ? 'password' : 'text'" :placeholder="settingPlaceholder(field)"><small v-if="settingHint(field)" class="muted">{{ settingHint(field) }}</small></label>
               </section>
             </template>
          </section>
        </template>
     </main>
    </div>
    <Teleport to="body">
      <dialog v-if="notificationDialogChannel" open class="apex-dialog notification-config-dialog" :aria-labelledby="`notification-config-title-${notificationDialogChannel}`" @click.self="closeNotificationDialog">
        <div class="apex-dialog-panel notification-config-panel">
          <header class="apex-dialog-header"><div><span class="eyebrow">通知渠道配置</span><h2 :id="`notification-config-title-${notificationDialogChannel}`">{{ channelLabel(notificationDialogChannel) }}</h2><p class="muted small">敏感字段只用于提交到当前 Emby Apex，不会显示完整值。</p></div><button class="icon-button" type="button" aria-label="关闭通知配置" title="关闭" @click="closeNotificationDialog"><X :size="19" /></button></header>
          <div v-if="notificationDialogError" class="msg error" role="alert"><AlertCircle :size="16" />{{ notificationDialogError }}</div>
          <div v-else-if="notificationDialogNotice" class="msg notice" role="status"><CheckCircle2 :size="16" />{{ notificationDialogNotice }}</div>
          <div class="notification-dialog-type"><span class="notification-dialog-type-label">通知渠道类型</span><span class="notification-dialog-type-value"><component :is="channelIcon(notificationDialogChannel)" :size="18" aria-hidden="true" /><strong>{{ channelLabel(notificationDialogChannel) }}</strong></span></div>
          <div class="notification-dialog-fields">
            <label v-for="field in [...channelFields(notificationDialogChannel), ...rowsFor(sharedNotificationFields)]" :key="field.name" :class="field.kind === 'bool' ? 'row notification-dialog-switch' : ''">
              <template v-if="field.kind === 'bool'"><input v-model="field.value" type="checkbox"><span>{{ settingLabel(field) }}</span></template>
              <template v-else><span>{{ settingLabel(field) }}</span><div class="notification-dialog-field-control" :class="{ 'has-icon': Boolean(settingIcon(field)) }"><component v-if="settingIcon(field)" :is="settingIcon(field)" :size="17" aria-hidden="true" /><input v-model="field.value" :type="notificationDialogChannel === 'wecom' ? 'text' : field.sensitive || field.kind === 'secret' ? 'password' : field.kind === 'int' ? 'number' : 'text'" :min="field.min ?? undefined" :max="field.max ?? undefined" :placeholder="settingPlaceholder(field)" autocomplete="off"></div></template>
              <small v-if="settingHint(field)" class="muted">{{ settingHint(field) }}</small>
            </label>
          </div>
          <section class="notification-event-compact"><h3>启用通知事件</h3><div class="notification-event-toggle-grid"><label v-for="category in notificationCategories" :key="category.key" class="notification-switch"><input v-for="toggle in rowsFor([category.key + '.' + notificationDialogChannel])" :key="toggle.name" v-model="toggle.value" type="checkbox" @change="setNotificationToggle(toggle, notificationDialogChannel!)"><span>{{ category.label }}</span></label></div></section>
          <section v-if="notificationDialogChannel === 'wecom'" class="wecom-menu-card notification-menu-card" aria-labelledby="wecom-menu-title-dialog">
            <div class="vue-section-heading"><div><h3 id="wecom-menu-title-dialog">企业微信菜单</h3><p class="hint">读取、同步或删除当前应用菜单；不会显示任何凭据。</p></div><Building2 :size="18" /></div>
            <p v-if="!wecomConfigured" class="msg notice" role="status"><AlertCircle :size="16" />企业微信尚未配置完整，请先填写企业 ID、AgentID 和应用密钥。</p>
            <div class="settings-actions"><button class="secondary sm" type="button" :disabled="menuBusy !== '' || !online || !wecomConfigured" @click="readWecomMenu"><LoaderCircle v-if="menuBusy === 'read'" class="spin" :size="15" /><RefreshCw v-else :size="15" />读取菜单</button><button class="primary sm" type="button" :disabled="menuBusy !== '' || !online || !wecomConfigured" @click="syncWecomMenu"><LoaderCircle v-if="menuBusy === 'sync'" class="spin" :size="15" />同步菜单</button><button class="danger sm" type="button" :disabled="menuBusy !== '' || !online || !wecomConfigured" @click="deleteWecomMenu"><LoaderCircle v-if="menuBusy === 'delete'" class="spin" :size="15" />删除菜单</button></div>
            <div v-if="menuError" class="msg error" role="alert"><AlertCircle :size="16" />{{ menuError }}</div><div v-else-if="menuNotice" class="msg notice" role="status"><CheckCircle2 :size="16" />{{ menuNotice }}</div>
            <div v-if="menuBusy === 'read'" class="vue-state compact"><LoaderCircle class="spin" :size="20" />正在读取菜单…</div><div v-else-if="wecomMenu && wecomMenuItems.length" class="wecom-menu-list"><div v-for="item in wecomMenuItems" :key="item.name" class="wecom-menu-item"><strong>{{ item.name }}</strong><span v-if="item.children.length" class="muted">{{ item.children.join('、') }}</span><span v-else class="muted">暂无子菜单</span></div></div><p v-else-if="wecomMenu && !wecomMenuItems.length" class="empty compact">当前未配置菜单。</p>
          </section>
          <footer class="apex-dialog-actions"><button class="danger sm" type="button" :disabled="notificationDialogBusy !== ''" @click="clearNotificationChannel">清空配置</button><span class="dialog-spacer"></span><button class="secondary sm" type="button" :disabled="notificationDialogBusy !== ''" @click="testNotificationChannel"><LoaderCircle v-if="notificationDialogBusy === 'test'" class="spin" :size="15" />测试连接</button><button class="primary sm" type="button" :disabled="notificationDialogBusy !== '' || !online" @click="saveNotificationChannel"><LoaderCircle v-if="notificationDialogBusy === 'save'" class="spin" :size="15" /><Save v-else :size="15" />保存配置</button></footer>
        </div>
      </dialog>
      <dialog v-if="userDialogOpen" open class="apex-dialog user-detail-dialog" aria-labelledby="user-detail-title" @click.self="closeUserDetails">
        <div class="apex-dialog-panel">
          <header class="apex-dialog-header"><div><span class="eyebrow">用户详情</span><h2 id="user-detail-title">{{ selectedUser?.username || '用户' }}</h2><p class="muted small">{{ selectedUser?.server_name || '未分配服务器' }} · {{ selectedUser?.is_admin ? '管理员账号' : '普通账号' }}</p></div><button class="icon-button" type="button" aria-label="关闭用户详情" title="关闭" @click="closeUserDetails"><X :size="19" /></button></header>
          <div v-if="userDialogError" class="msg error" role="alert"><AlertCircle :size="16" />{{ userDialogError }}<a v-if="userDialogError.includes('登录')" href="/login" class="button-link">重新登录</a></div>
          <div v-if="userDialogNotice" class="msg notice" role="status"><CheckCircle2 :size="16" />{{ userDialogNotice }}</div>
          <form class="user-detail-form" @submit.prevent="saveUserDetails">
            <div class="user-detail-summary"><span><strong>注册时间</strong>{{ formatDateTime(selectedUser?.registered_at) }}</span><span><strong>最后同步</strong>{{ formatDateTime(selectedUser?.synced_at) }}</span><span><strong>当前播放</strong>{{ selectedUser?.is_playing ? '正在播放' : '未播放' }}</span><span><strong>最近播放</strong>{{ formatDateTime(selectedUser?.last_played_at) }}</span><span><strong>合计播放时长</strong>{{ playbackDuration(selectedUser?.total_playback_seconds) }}</span></div>
            <label class="switch-field"><input v-model="userEdit.permanent" type="checkbox"><span><strong>永久账户</strong><small>永久账户不设置到期时间。</small></span></label>
            <label v-if="!userEdit.permanent"><span>到期时间</span><input v-model="userEdit.expires_at" type="datetime-local" step="1"><small class="muted">精确到秒，按本地时间保存。</small></label>
            <label class="switch-field"><input v-model="userEdit.playback_enabled" type="checkbox"><span><strong>播放权限</strong><small>{{ userEdit.playback_enabled ? '允许播放媒体内容。' : '已禁止播放媒体内容。' }}</small></span></label>
            <label v-if="!selectedUser?.is_admin" class="switch-field"><input v-model="userEdit.portal_enabled" type="checkbox"><span><strong>开通用户端登录</strong><small>{{ userEdit.portal_enabled ? '允许该账号登录用户端。' : '禁止该账号登录用户端。' }}</small></span></label>
            <label v-if="!selectedUser?.is_admin && userEdit.portal_enabled && !selectedUser?.portal_password_configured"><span>用户端初始密码</span><input v-model="userEdit.portal_password" type="password" minlength="8" autocomplete="new-password" placeholder="至少 8 位"><small class="muted">首次开通必须设置密码，之后可在用户端修改。</small></label>
            <label class="switch-field"><input v-model="userEdit.disabled" type="checkbox" :disabled="Boolean(selectedUser?.is_admin)"><span><strong>账号状态</strong><small>{{ selectedUser?.is_admin ? '管理员账号不可停用。' : (userEdit.disabled ? '停用后将无法登录。' : '账号可正常登录。') }}</small></span></label>
            <label><span>备注</span><textarea v-model="userEdit.note" maxlength="1000" placeholder="可选备注"></textarea></label>
            <footer class="apex-dialog-actions"><button class="danger sm" type="button" :disabled="Boolean(selectedUser?.is_admin) || userDialogBusy" @click="deleteSelectedUser"><Trash2 :size="15" />删除用户</button><span class="dialog-spacer"></span><button class="secondary sm" type="button" :disabled="userDialogBusy" @click="closeUserDetails">取消</button><button class="primary sm" type="submit" :disabled="userDialogBusy || !online"><LoaderCircle v-if="userDialogBusy" class="spin" :size="15" /><Save v-else :size="15" />{{ userDialogBusy ? '保存中' : '保存修改' }}</button></footer>
          </form>
        </div>
      </dialog>
      <dialog v-if="selectedRequest" open class="apex-dialog request-detail-dialog" aria-labelledby="request-detail-title" @click.self="closeRequestDetails">
        <div class="apex-dialog-panel request-detail-panel">
          <header class="apex-dialog-header"><div><span class="eyebrow">求片详情</span><h2 id="request-detail-title">{{ selectedRequest.title }}</h2><p class="muted small">{{ selectedRequest.media_type === 'movie' ? '电影' : '电视剧' }} · TMDB {{ selectedRequest.tmdb_id }} · {{ selectedRequest.server_name || '当前服务器' }}</p></div><button class="icon-button" type="button" aria-label="关闭求片详情" title="关闭" @click="closeRequestDetails"><X :size="19" /></button></header>
          <div v-if="requestDetailError" class="msg error" role="alert"><AlertCircle :size="16" />{{ requestDetailError }}<a v-if="requestDetailError.includes('登录')" href="/login" class="button-link">重新登录</a></div>
          <div v-if="requestDetailNotice" class="msg notice" role="status"><CheckCircle2 :size="16" />{{ requestDetailNotice }}</div>
          <div v-if="requestDetailBusy" class="vue-state compact"><LoaderCircle class="spin" :size="23" />正在加载作品详情…</div>
          <template v-else-if="requestDetail">
            <div class="request-detail-hero" :style="requestDetail.backdrop_url ? { backgroundImage: `linear-gradient(90deg, rgba(19,17,28,.97), rgba(19,17,28,.65)), url(${requestDetail.backdrop_url})` } : undefined"><img class="request-detail-poster" :src="requestDetail.poster_url || selectedRequest.poster_local_url || selectedRequest.poster_url || '/static/logoicon.png'" :alt="requestDetail.title || selectedRequest.title"><div class="request-detail-copy"><h3>{{ requestDetail.title || selectedRequest.title }}</h3><p v-if="requestDetail.original_title" class="muted">{{ requestDetail.original_title }}</p><p>{{ requestDetail.overview || '暂无简介' }}</p><div class="detail-chip-row"><span v-for="genre in (requestDetail.genres || [])" :key="genre" class="badge info">{{ genre }}</span><span v-if="requestDetail.rating != null" class="badge warn"><Star :size="12" />{{ Number(requestDetail.rating).toFixed(1) }}</span><span class="badge">{{ detailRuntime(requestDetail) }}</span></div></div></div>
            <div class="request-detail-facts"><div><strong>导演</strong><span>{{ peopleNames(requestDetail.directors) || '—' }}</span></div><div><strong>演员</strong><span>{{ peopleNames(requestDetail.cast, 12) || '—' }}</span></div><div><strong>求片用户</strong><span>{{ requestUserNames(selectedRequest) || '—' }}</span></div><div><strong>提交时间</strong><span>{{ formatDateTime((selectedRequest.items || [])[0]?.created_at) }}</span></div><div><strong>备注</strong><span>{{ requestNotes(selectedRequest) || '—' }}</span></div><div><strong>拒绝原因</strong><span>{{ requestRejectionReasons(selectedRequest) || '—' }}</span></div></div>
            <footer v-if="selectedRequest.status === 'pending'" class="apex-dialog-actions request-detail-actions"><button class="primary sm" type="button" :disabled="Boolean(busy) || !online" @click.stop="confirmRequest(selectedRequest)"><CheckCircle2 :size="15" />确认入库</button><form class="reject-form" @submit.prevent.stop="rejectRequest(selectedRequest)"><input v-model="rejectReasons[groupKey(selectedRequest)]" maxlength="1000" placeholder="拒绝原因（可选）"><button class="danger sm" type="submit" :disabled="Boolean(busy) || !online">拒绝</button></form></footer>
          </template>
        </div>
      </dialog>
    </Teleport>
    <nav class="mobile-bottom-nav apex-mobile-dock" aria-label="常用导航">
      <div class="apex-mobile-dock-card">
        <div class="apex-mobile-dock-toggle" role="list">
          <a v-for="item in nav.slice(0, 4)" :key="item.path" class="apex-mobile-dock-btn" :href="item.path" :class="{ on: currentPath === item.path }" :aria-current="currentPath === item.path ? 'page' : undefined" role="listitem"><span class="apex-mobile-dock-btn-content"><component :is="item.icon" :size="24" :stroke-width="1.8" /><span>{{ item.label }}</span></span></a>
          <button class="apex-mobile-dock-btn" type="button" :class="{ on: nav.slice(4).some((item) => item.path === currentPath) }" aria-haspopup="dialog" :aria-expanded="moreOpen" aria-controls="mobile-more-dialog" @click="toggleMore"><span class="apex-mobile-dock-btn-content"><MoreHorizontal :size="24" :stroke-width="1.8" /><span>更多</span></span></button>
        </div>
      </div>
    </nav>
    <Teleport to="body">
      <dialog v-if="moreOpen" id="mobile-more-dialog" open class="open admin-more-dialog" aria-labelledby="mobile-more-title" @click.self="closeMore">
        <div class="admin-more-shell">
          <header class="admin-more-header">
            <button class="admin-more-icon-button" type="button" aria-label="返回管理端" title="返回管理端" @click="closeMore"><ArrowLeft :size="19" /></button>
            <div class="admin-more-heading"><span>Emby Apex</span><strong id="mobile-more-title">更多功能</strong></div>
            <div class="admin-more-actions">
              <button class="admin-more-icon-button" type="button" :aria-expanded="moreSearchOpen" aria-controls="admin-more-search" aria-label="搜索导航" title="搜索导航" @click="toggleMoreSearch"><Search :size="18" /></button>
              <a class="admin-more-icon-button" href="/settings" aria-label="通知设置" title="通知设置" @click="closeMore"><Bell :size="18" /></a>
              <button class="admin-more-icon-button" type="button" aria-label="关闭更多功能" title="关闭" @click="closeMore"><X :size="19" /></button>
            </div>
          </header>
          <div v-if="moreSearchOpen" id="admin-more-search" class="admin-more-search">
            <Search :size="16" aria-hidden="true" />
            <input v-model="moreSearchQuery" type="search" placeholder="搜索功能" aria-label="搜索功能">
            <button v-if="moreSearchQuery" type="button" aria-label="清除搜索" title="清除搜索" @click="moreSearchQuery = ''"><X :size="15" /></button>
          </div>
          <div class="admin-more-scroll">
            <section v-for="group in filteredNavGroups" :key="group.label" class="admin-more-group" :aria-labelledby="`admin-more-group-${group.label}`">
              <h2 :id="`admin-more-group-${group.label}`">{{ group.label }}</h2>
              <div class="admin-more-items">
                <a v-for="item in group.items" :key="item.path" class="admin-more-item" :style="{ '--admin-more-accent': item.accent }" :href="item.path" @click="closeMore">
                  <span class="admin-more-item-icon"><component :is="item.icon" :size="18" :stroke-width="2" aria-hidden="true" /></span>
                  <span class="admin-more-item-label">{{ item.label }}</span>
                  <ChevronRight class="admin-more-item-chevron" :size="17" aria-hidden="true" />
                </a>
              </div>
            </section>
            <p v-if="!filteredNavGroups.length" class="admin-more-empty">没有匹配的功能</p>
          </div>
          <nav class="admin-more-dock apex-mobile-dock" aria-label="常用导航">
            <div class="apex-mobile-dock-card">
              <div class="apex-mobile-dock-toggle" role="list">
                <a v-for="item in nav.slice(0, 4)" :key="item.path" class="apex-mobile-dock-btn" :href="item.path" role="listitem" @click="closeMore"><span class="apex-mobile-dock-btn-content"><component :is="item.icon" :size="24" :stroke-width="1.8" aria-hidden="true" /><span>{{ item.label }}</span></span></a>
                <button class="apex-mobile-dock-btn on" type="button" aria-label="关闭更多功能" @click="closeMore"><span class="apex-mobile-dock-btn-content"><MoreHorizontal :size="24" :stroke-width="1.8" /><span>更多</span></span></button>
              </div>
            </div>
          </nav>
        </div>
      </dialog>
    </Teleport>
  </div>
</template>
