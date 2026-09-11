<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import {
  AlertCircle,
  ArrowLeft,
  CalendarDays,
  CheckCircle2,
  Clock3,
  Film,
  Gift,
  KeyRound,
  LayoutDashboard,
  LoaderCircle,
  LogOut,
  RefreshCw,
  Search,
  ScanLine,
  Send,
  Settings,
  Star,
  Tv,
  UserRound,
  Users,
  WifiOff,
  X,
} from 'lucide-vue-next';
import { ApiError, getApi, postApi } from '../shared/api';
import { AppNav, MobileBottomNav } from '../shared/components';
import { formatDateTime } from '../shared/format';
import { goBack as navigateBack } from '../shared/router';

export type MediaType = 'movie' | 'tv';
export type SearchMode = 'multi' | MediaType | 'movie_id' | 'tv_id';
export type RequestTab = 'search' | 'pending' | 'library' | 'rejected';

export type CreditPerson = {
  id?: number | null;
  name: string;
  character?: string | null;
  profile_url?: string | null;
};

export type MediaItem = {
  id?: number;
  tmdb_id: number;
  media_source?: string;
  media_id?: string;
  media_type: MediaType;
  title: string;
  original_title?: string | null;
  year?: number | null;
  overview?: string | null;
  poster_url?: string | null;
  poster_local_url?: string | null;
  status?: string | null;
  note?: string | null;
  created_at?: string | null;
  rejection_reason?: string | null;
  release_date?: string | null;
  tagline?: string | null;
  rating?: number | null;
  runtime_minutes?: number | null;
  genres?: string[];
  directors?: CreditPerson[];
  cast?: CreditPerson[];
  seasons?: number | null;
  episodes?: number | null;
  backdrop_url?: string | null;
  library_state?: string;
  moviepilot_subscribe_state?: string;
};

export type RequestLists = {
  pending: MediaItem[];
  in_library: MediaItem[];
  rejected: MediaItem[];
};

export type RequestBootstrap = {
  csrfToken?: string;
  tab: RequestTab;
  mode: SearchMode;
  query: string;
  year: string;
  results: MediaItem[];
  selected: MediaItem | null;
  pendingCount: number;
  libraryCount: number;
  rejectedCount: number;
  error: string;
  notice: string;
};

type Props = { bootstrap: RequestBootstrap; standalone?: boolean };

const portalNav = [
  { path: '/account', label: '账户概览', icon: LayoutDashboard },
  { path: '/requests', label: '求片', icon: Search },
  { path: '/account#activation', label: '激活', icon: KeyRound },
  { path: '/account#scan', label: '扫码', icon: ScanLine },
  { path: '/account#settings', label: '账户设置', icon: Settings },
];

const detailFields = [
  'release_date',
  'tagline',
  'status',
  'rating',
  'runtime_minutes',
  'genres',
  'directors',
  'cast',
  'seasons',
  'episodes',
  'backdrop_url',
] as const;

const props = defineProps<Props>();
const standalone = props.standalone !== false;
const csrfToken = ref(props.bootstrap.csrfToken || '');
const tab = ref<RequestTab>(props.bootstrap.tab || 'search');
const mode = ref<SearchMode>(props.bootstrap.mode || 'multi');
const query = ref(props.bootstrap.query || '');
const year = ref(props.bootstrap.year || '');
const results = ref<MediaItem[]>(props.bootstrap.results || []);
const selected = ref<MediaItem | null>(props.bootstrap.selected || null);
const lists = ref<RequestLists>({ pending: [], in_library: [], rejected: [] });
const note = ref('');
const loading = ref(false);
const listsLoading = ref(false);
const detailLoading = ref(false);
const submitting = ref(false);
const searched = ref(Boolean(props.bootstrap.query));
const errorMessage = ref(props.bootstrap.error || '');
const successMessage = ref(props.bootstrap.notice || '');
const loginExpired = ref(false);
const detailError = ref('');
const detailNotice = ref('');
const detailLoginExpired = ref(false);
const requestKey = ref('');
const online = ref(typeof navigator === 'undefined' ? true : navigator.onLine);
const listsRequestSucceeded = ref(false);
const detailVisible = ref(false);
const detailDialog = ref<HTMLDialogElement | null>(null);
const logoutBusy = ref(false);
const selectedSeasons = ref<number[]>([]);
const expandedSeasons = ref<number[]>([]);

const isIdMode = computed(() => mode.value === 'movie_id' || mode.value === 'tv_id');
const pending = computed(() => lists.value.pending);
const inLibrary = computed(() => lists.value.in_library);
const rejected = computed(() => lists.value.rejected);
const pendingCount = computed(() => listsRequestSucceeded.value ? pending.value.length : props.bootstrap.pendingCount || 0);
const libraryCount = computed(() => listsRequestSucceeded.value ? inLibrary.value.length : props.bootstrap.libraryCount || 0);
const rejectedCount = computed(() => listsRequestSucceeded.value ? rejected.value.length : props.bootstrap.rejectedCount || 0);
const activeList = computed(() => ({ pending: pending.value, library: inLibrary.value, rejected: rejected.value } as Record<string, MediaItem[]>)[tab.value] || []);
const listTitle = computed(() => ({ pending: '进行中的求片', library: '已入库', rejected: '已拒绝' } as Record<string, string>)[tab.value] || '');
const listEmpty = computed(() => ({ pending: '还没有进行中的求片。', library: '当前还没有已入库的求片。', rejected: '还没有被拒绝的求片。' } as Record<string, string>)[tab.value] || '');
const requestStateByKey = computed(() => {
  const state: Record<string, 'pending' | 'library' | 'rejected'> = {};
  for (const item of pending.value) state[recordKey(item)] = 'pending';
  for (const item of inLibrary.value) state[recordKey(item)] = 'library';
  for (const item of rejected.value) if (!state[recordKey(item)]) state[recordKey(item)] = 'rejected';
  return state;
});
const selectedState = computed(() => selected.value ? itemRequestState(selected.value) : '');
// Keep the subscribe action available whenever a media item is selected.  A
// transient detail request failure/loading state should not make the button
// permanently inert: the backend resolves the canonical MoviePilot media
// identity again when creating the subscription and returns a useful error.
const selectedCanSubmit = computed(() => Boolean(selected.value && !submitting.value && online.value && selectedState.value !== 'pending' && selectedState.value !== 'library'));

function recordKey(item: Pick<MediaItem, 'media_type' | 'tmdb_id'> & Partial<MediaItem>): string {
  return `${item.media_type}:${item.media_source || 'themoviedb'}:${item.media_id || item.tmdb_id}`;
}

function itemRequestState(item: MediaItem): 'pending' | 'library' | 'rejected' | '' {
  const fromList = requestStateByKey.value[recordKey(item)];
  if (fromList) return fromList;
  if (item.status === 'pending') return 'pending';
  if (item.status === 'in_library') return 'library';
  if (item.status === 'rejected') return 'rejected';
  if (item.library_state === 'in_library') return 'library';
  return '';
}

function statusLabel(state: string): string {
  return ({ pending: '已求片', library: '已入库', rejected: '已拒绝' } as Record<string, string>)[state] || '';
}

function statusClass(state: string): string {
  return state === 'library' ? 'ok' : state === 'rejected' ? 'bad' : 'pending';
}

function mediaLabel(item: MediaItem): string {
  return item.media_type === 'movie' ? '电影' : '电视剧';
}

function libraryStateLabel(item: MediaItem): string {
  if (itemRequestState(item) === 'library') return '已入库';
  if (item.library_state === 'unknown') return '无法确认';
  if (item.library_state === 'not_in_library') return '未入库';
  return '';
}

function poster(item: MediaItem): string {
  return item.poster_local_url || item.poster_url || '/static/logoicon.png';
}

function profileImage(person: CreditPerson): string {
  return person.profile_url || '/static/logoicon.png';
}

function hasCompleteDetails(item: MediaItem): boolean {
  return Boolean(item.title && item.media_type && (item.media_id || item.tmdb_id) && detailFields.every((field) => Object.prototype.hasOwnProperty.call(item, field)));
}

function clearMessages(): void {
  errorMessage.value = '';
  successMessage.value = '';
  loginExpired.value = false;
}

function handleError(error: unknown, fallback: string): void {
  if (error instanceof ApiError && error.status === 401) {
    loginExpired.value = true;
    errorMessage.value = '登录已失效，请重新登录后继续。';
    return;
  }
  if (!online.value) {
    errorMessage.value = '网络不可用，请恢复联网后重试。';
    return;
  }
  errorMessage.value = error instanceof Error ? error.message : fallback;
}

function detailErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      detailLoginExpired.value = true;
      return '登录已失效，请重新登录。';
    }
    if (error.status === 400) return '作品详情请求无效。';
    if (error.status >= 500) return '服务暂时不可用，请稍后重试。';
  }
  if (!online.value) return '当前离线，无法加载作品详情。';
  return error instanceof Error ? error.message : fallback;
}

async function loadLists(): Promise<void> {
  listsLoading.value = true;
  try {
    const response = await getApi<RequestLists>('/requests');
    lists.value = {
      pending: response.data?.pending || [],
      in_library: response.data?.in_library || [],
      rejected: response.data?.rejected || [],
    };
    listsRequestSucceeded.value = true;
  } catch (error) {
    handleError(error, '求片列表加载失败，请稍后重试。');
  } finally {
    listsLoading.value = false;
  }
}

async function search(): Promise<void> {
  clearMessages();
  selected.value = null;
  closeDetailDialog();
  note.value = '';
  searched.value = true;
  if (!query.value.trim()) {
    results.value = [];
    errorMessage.value = '请输入名称或 TMDB ID。';
    return;
  }
  loading.value = true;
  try {
    const params = new URLSearchParams({ mode: mode.value, query: query.value.trim(), year: year.value.trim() });
    const response = await getApi<{ results: MediaItem[] }>(`/requests/search?${params.toString()}`);
    results.value = response.data?.results || [];
  } catch (error) {
    results.value = [];
    handleError(error, '搜索失败，请检查网络或代理设置。');
  } finally {
    loading.value = false;
  }
}

function openDetailDialog(): void {
  detailVisible.value = true;
  void nextTick(() => {
    const dialog = detailDialog.value;
    if (dialog && typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal();
    else if (dialog) dialog.setAttribute('open', '');
  });
}

function closeDetailDialog(): void {
  const dialog = detailDialog.value;
  if (dialog?.open && typeof dialog.close === 'function') dialog.close();
  detailVisible.value = false;
}

async function select(item: MediaItem): Promise<void> {
  clearMessages();
  detailError.value = '';
  detailNotice.value = '';
  detailLoginExpired.value = false;
  selected.value = { ...item };
  selectedSeasons.value = item.media_type === 'tv' && item.seasons ? Array.from({ length: item.seasons }, (_, i) => i + 1) : [];
  expandedSeasons.value = [];
  note.value = item.note || '';
  openDetailDialog();
  if (hasCompleteDetails(item)) return;
  detailLoading.value = true;
  try {
    const response = await getApi<MediaItem>(`/requests/tmdb/${encodeURIComponent(item.media_type)}/${encodeURIComponent(String(item.media_id || item.tmdb_id))}?media_source=${encodeURIComponent(item.media_source || 'themoviedb')}`);
    const merged = { ...item, ...(response.data || {}) };
    selected.value = merged;
    if (merged.media_type === 'tv' && merged.seasons && !selectedSeasons.value.length) {
      selectedSeasons.value = Array.from({ length: merged.seasons }, (_, i) => i + 1);
    }
  } catch (error) {
    detailError.value = detailErrorMessage(error, '作品详情加载失败，请稍后重试。');
  } finally {
    detailLoading.value = false;
  }
}

async function changeTab(nextTab: RequestTab): Promise<void> {
  clearMessages();
  tab.value = nextTab;
  if (nextTab !== 'search') await loadLists();
}

function formatReleaseDate(item: MediaItem): string {
  return item.release_date || (item.year ? String(item.year) : '—');
}

function formatRating(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(1)} / 10` : '—';
}

function formatRuntime(value: number | null | undefined): string {
  const minutes = Number(value || 0);
  if (!minutes) return '—';
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return hours ? `${hours} 小时 ${rest} 分钟` : `${rest} 分钟`;
}

function toggleSeason(season: number): void {
  expandedSeasons.value = expandedSeasons.value.includes(season)
    ? expandedSeasons.value.filter((value) => value !== season)
    : [...expandedSeasons.value, season];
}

async function submitRequest(): Promise<void> {
  const item = selected.value;
  if (!item || submitting.value) return;
  if (!online.value) {
    detailError.value = '当前处于离线状态，提交操作已禁用。';
    return;
  }
  if (!csrfToken.value) {
    detailError.value = '缺少 CSRF 凭据，请刷新页面重试。';
    return;
  }
  if (selectedState.value === 'pending' || selectedState.value === 'library') {
    detailError.value = `该作品${selectedState.value === 'pending' ? '已提交求片' : '已入库'}，无需重复提交。`;
    return;
  }
  clearMessages();
  detailError.value = '';
  detailLoginExpired.value = false;
  const key = recordKey(item);
  if (requestKey.value === key) return;
  requestKey.value = key;
  submitting.value = true;
  try {
    await postApi<MediaItem>('/requests', { tmdb_id: item.tmdb_id, media_id: item.media_id || String(item.tmdb_id), media_source: item.media_source || 'themoviedb', media_type: item.media_type, seasons: item.media_type === 'tv' ? selectedSeasons.value : [], note: note.value }, csrfToken.value);
    detailNotice.value = '求片已提交，状态已更新为进行中。';
    await loadLists();
  } catch (error) {
    requestKey.value = '';
    if (error instanceof ApiError && error.status === 401) {
      detailLoginExpired.value = true;
      detailError.value = '登录已失效，请重新登录。';
    } else if (error instanceof ApiError && error.status === 400) {
      detailError.value = error.message || '请求无效，请检查备注后重试。';
    } else if (error instanceof ApiError && error.status >= 500) {
      detailError.value = '服务暂时不可用，请稍后重试。';
    } else if (!online.value) {
      detailError.value = '当前离线，提交操作已取消。';
    } else {
      detailError.value = error instanceof Error ? error.message : '提交失败，请稍后重试。';
    }
  } finally {
    submitting.value = false;
  }
}

function updateOnlineState(): void {
  online.value = navigator.onLine;
}

function goBack(): void {
  navigateBack('portal');
}

async function logout(): Promise<void> {
  if (logoutBusy.value || !online.value) return;
  logoutBusy.value = true;
  try {
    await postApi('/auth/logout', {}, csrfToken.value);
    window.location.assign('/login');
  } catch (error) {
    handleError(error, '退出登录失败，请稍后重试。');
  } finally {
    logoutBusy.value = false;
  }
}

onMounted(async () => {
  document.title = '求片 · Emby Apex';
  window.addEventListener('online', updateOnlineState);
  window.addEventListener('offline', updateOnlineState);
  try {
    const csrf = await getApi<{ csrf_token: string }>('/auth/csrf');
    csrfToken.value = csrf.data?.csrf_token || '';
  } catch (error) {
    handleError(error, '无法建立安全会话，请刷新页面重试。');
  }
  void loadLists();
});

onBeforeUnmount(() => {
  window.removeEventListener('online', updateOnlineState);
  window.removeEventListener('offline', updateOnlineState);
  closeDetailDialog();
});
</script>

<template>
  <div class="portal-workspace portal-request-workspace">
    <aside class="portal-sidebar" aria-label="用户中心导航">
      <div class="brand-block"><img :src="'/static/brand-logo.png'" alt=""><div class="brand-copy"><strong>Emby Apex</strong><span>用户中心</span></div></div>
      <AppNav class="portal-sidebar-nav" :items="portalNav" active-path="/requests" aria-label="用户中心导航" />
    </aside>

    <section class="vue-request-app" aria-label="Vue 求片中心">
      <header v-if="standalone" class="portal-header bar">
        <button class="back-button" type="button" aria-label="返回上一页" title="返回上一页" @click="goBack"><ArrowLeft :size="20" /></button>
        <h1 class="portal-brand"><img :src="'/static/brand-logo.png'" alt=""><span>求片中心</span></h1>
        <div class="actions"><button class="link" type="button" :disabled="listsLoading || !online" @click="loadLists"><LoaderCircle v-if="listsLoading" class="spin" :size="15" /><RefreshCw v-else :size="15" />{{ listsLoading ? '刷新中' : '刷新' }}</button><button class="link" type="button" :disabled="logoutBusy || !online" @click="logout"><LogOut :size="15" />退出登录</button></div>
      </header>

      <div v-if="errorMessage" class="msg error vue-feedback" role="alert"><AlertCircle :size="18" aria-hidden="true" /><span>{{ errorMessage }}</span><a v-if="loginExpired" href="/login" class="button-link">重新登录</a></div>
      <div v-if="successMessage" class="msg notice vue-feedback" role="status"><CheckCircle2 :size="18" aria-hidden="true" /><span>{{ successMessage }}</span></div>
      <p v-if="!online" class="msg notice vue-feedback" role="status"><WifiOff :size="18" aria-hidden="true" /><span>当前离线，仅显示已加载数据；写入操作已禁用。</span></p>

      <nav class="tabs vue-request-tabs" aria-label="求片分类">
        <button type="button" :class="['tab', { on: tab === 'search' }]" @click="changeTab('search')"><Search :size="15" />求片</button>
        <button type="button" :class="['tab', { on: tab === 'pending' }]" @click="changeTab('pending')">已求片 <span>{{ pendingCount }}</span></button>
        <button type="button" :class="['tab', { on: tab === 'library' }]" @click="changeTab('library')">已入库 <span>{{ libraryCount }}</span></button>
        <button type="button" :class="['tab', { on: tab === 'rejected' }]" @click="changeTab('rejected')">已拒绝 <span>{{ rejectedCount }}</span></button>
      </nav>

      <template v-if="tab === 'search'">
        <section class="card vue-request-search">
          <div class="vue-section-heading"><div><h2>搜索电影或电视剧</h2><p class="hint">选择作品后填写备注并提交求片。</p></div><Search :size="22" aria-hidden="true" /></div>
          <form class="search-form" @submit.prevent="search">
            <label><span>搜索模式</span><select v-model="mode"><option value="multi">聚合（电影 + 电视剧）</option><option value="movie">电影</option><option value="tv">电视剧</option><option value="movie_id">电影 TMDB ID</option><option value="tv_id">剧集 TMDB ID</option></select></label>
            <label><span>{{ isIdMode ? 'TMDB ID' : '名称' }}</span><input v-model="query" type="search" required autocomplete="off" :placeholder="isIdMode ? '输入 TMDB ID' : '输入名称'"></label>
            <label v-if="!isIdMode"><span>年份（可选）</span><input v-model="year" type="text" inputmode="numeric" maxlength="4" placeholder="例如 2024"></label>
            <button class="primary" type="submit" :disabled="loading"><LoaderCircle v-if="loading" class="spin" :size="17" /><Search v-else :size="17" />{{ loading ? '搜索中' : '搜索' }}</button>
          </form>
        </section>

        <section v-if="loading" class="card vue-state" role="status"><LoaderCircle class="spin" :size="22" /><span>正在搜索…</span></section>
        <section v-else-if="searched && !results.length" class="card vue-state"><Film :size="24" /><strong>没有找到匹配作品</strong><span class="hint">请尝试更短的名称、其他年份或 TMDB ID。</span></section>
        <section v-else-if="results.length" class="vue-request-results">
          <div class="vue-section-heading"><h2>搜索结果</h2><span class="muted small">{{ results.length }} 个结果</span></div>
          <div class="result-grid portal-poster-grid">
            <article v-for="item in results" :key="recordKey(item)" class="result-card vue-result-card portal-poster-card" role="button" tabindex="0" @click="select(item)" @keydown.enter.prevent="select(item)">
              <img class="poster" :src="poster(item)" :alt="item.title" loading="lazy"><span class="mp-type-chip">{{ mediaLabel(item) }}</span><span v-if="item.rating" class="mp-rating-chip">{{ Number(item.rating).toFixed(1) }}</span><div class="media-hover-overview">{{ item.overview || '暂无简介' }}</div>
              <div class="result-body"><div class="result-card-title"><h3>{{ item.title }}</h3><span v-if="itemRequestState(item)" :class="['badge', statusClass(itemRequestState(item))]">{{ statusLabel(itemRequestState(item)) }}</span><span v-else-if="libraryStateLabel(item)" class="badge off">{{ libraryStateLabel(item) }}</span></div><p class="meta"><component :is="item.media_type === 'movie' ? Film : Tv" :size="13" />{{ mediaLabel(item) }} · {{ item.year || '年份未知' }}<template v-if="item.media_type === 'tv' && item.seasons"> · {{ item.seasons }} 季<span v-if="item.episodes"> · {{ item.episodes }} 集</span></template></p><p class="overview">{{ item.overview || '暂无简介' }}</p><button type="button" class="secondary" @click.stop="select(item)"><Search :size="15" />查看详情</button></div>
            </article>
          </div>
        </section>
      </template>

      <template v-else>
        <section class="card vue-request-list-panel">
          <div class="vue-section-heading"><div><h2>{{ listTitle }}</h2><p class="hint">仅显示当前账户可见的求片记录。</p></div><button class="secondary sm" type="button" :disabled="listsLoading" @click="loadLists"><LoaderCircle v-if="listsLoading" class="spin" :size="15" /><RefreshCw v-else :size="15" />刷新</button></div>
          <div v-if="listsLoading && !activeList.length" class="vue-state compact" role="status"><LoaderCircle class="spin" :size="21" /><span>正在加载列表…</span></div>
          <div v-else-if="!activeList.length" class="vue-state compact"><Film :size="24" /><span>{{ listEmpty }}</span></div>
          <div v-else class="request-list">
            <article v-for="item in activeList" :key="item.id || recordKey(item)" class="request-item portal-request-item" role="button" tabindex="0" @click="select(item)" @keydown.enter.prevent="select(item)">
              <img class="poster" :src="poster(item)" :alt="item.title" loading="lazy"><div><div class="request-item-title"><h3>{{ item.title }}</h3><span :class="['badge', statusClass(itemRequestState(item))]">{{ statusLabel(itemRequestState(item)) }}</span></div><p class="meta">{{ mediaLabel(item) }} · {{ item.year || '年份未知' }} · TMDB {{ item.tmdb_id }} · {{ formatDateTime(item.created_at) }}</p><p>{{ item.overview || '暂无简介' }}</p><p v-if="item.note" class="hint">备注：{{ item.note }}</p><p v-if="tab === 'rejected' && item.rejection_reason" class="hint">拒绝原因：{{ item.rejection_reason }}</p><a v-if="tab === 'rejected'" class="button-link" :href="`/requests?tab=search&mode=${item.media_type}&query=${encodeURIComponent(item.title)}`" @click.stop>重新搜索</a></div>
            </article>
          </div>
        </section>
      </template>

      <dialog v-if="detailVisible" ref="detailDialog" class="portal-media-dialog" aria-labelledby="portal-detail-title" @cancel.prevent="closeDetailDialog" @click.self="closeDetailDialog">
        <div class="portal-media-dialog-panel">
          <header class="portal-dialog-header"><div><span class="eyebrow">作品详情</span><h2 id="portal-detail-title">{{ selected?.title || '加载作品详情' }}</h2></div><button class="icon-button" type="button" aria-label="关闭详情" title="关闭详情" @click="closeDetailDialog"><X :size="20" /></button></header>
          <div v-if="detailError" class="msg error vue-feedback" role="alert"><AlertCircle :size="17" /><span>{{ detailError }}</span><a v-if="detailLoginExpired" href="/login" class="button-link">重新登录</a></div>
          <div v-if="detailNotice" class="msg notice vue-feedback" role="status"><CheckCircle2 :size="17" /><span>{{ detailNotice }}</span></div>
          <div v-if="detailLoading" class="vue-state compact" role="status"><LoaderCircle class="spin" :size="24" /><span>正在加载详情…</span></div>
          <template v-if="selected">
            <section class="portal-detail-hero" :style="selected.backdrop_url ? { backgroundImage: `url(${selected.backdrop_url})` } : undefined">
              <img class="portal-detail-poster" :src="poster(selected)" :alt="selected.title">
              <div class="portal-detail-copy"><div class="vue-detail-title"><h3>{{ selected.title }}</h3><span class="badge info">{{ mediaLabel(selected) }}</span><span v-if="itemRequestState(selected)" :class="['badge', statusClass(itemRequestState(selected))]">{{ statusLabel(itemRequestState(selected)) }}</span></div><p v-if="selected.original_title && selected.original_title !== selected.title" class="detail-original">{{ selected.original_title }}</p><p v-if="selected.tagline" class="detail-tagline">{{ selected.tagline }}</p><div class="detail-chip-row"><span v-for="genre in (selected.genres || [])" :key="genre" class="badge">{{ genre }}</span></div></div>
            </section>
            <p class="portal-detail-overview">{{ selected.overview || '暂无简介' }}</p>
            <dl class="portal-detail-facts"><div><dt><CalendarDays :size="14" />发行日期</dt><dd>{{ formatReleaseDate(selected) }}</dd></div><div><dt><Star :size="14" />评分</dt><dd>{{ formatRating(selected.rating) }}</dd></div><div><dt><Clock3 :size="14" />时长</dt><dd>{{ formatRuntime(selected.runtime_minutes) }}</dd></div><div><dt><Film :size="14" />状态</dt><dd>{{ selected.status || '—' }}</dd></div><div v-if="selected.media_type === 'tv'"><dt><Tv :size="14" />季 / 集</dt><dd>{{ selected.seasons ?? '—' }} 季 · {{ selected.episodes ?? '—' }} 集</dd></div><div><dt><UserRound :size="14" />TMDB ID</dt><dd>{{ selected.tmdb_id }}</dd></div></dl>
            <section v-if="selected.directors?.length" class="portal-credit-section"><h3><UserRound :size="16" />导演</h3><div class="portal-director-list"><span v-for="person in (selected.directors || []).slice(0, 5)" :key="person.id || person.name">{{ person.name }}</span></div></section>
            <section v-if="selected.cast?.length" class="portal-credit-section"><h3><Users :size="16" />演员</h3><div class="portal-cast-grid"><div v-for="person in (selected.cast || []).slice(0, 12)" :key="person.id || person.name" class="portal-cast-person"><img :src="profileImage(person)" :alt="person.name" loading="lazy"><div><strong>{{ person.name }}</strong><span>{{ person.character || '角色未知' }}</span></div></div></div></section>
            <form class="portal-request-form" @submit.prevent="submitRequest"><div v-if="selected.media_type === 'tv' && selected.seasons" class="season-picker"><strong>季</strong><div class="season-grid"><div v-for="season in selected.seasons" :key="season" class="season-row"><label><input v-model="selectedSeasons" type="checkbox" :value="season" :disabled="!selectedCanSubmit"> 第 {{ season }} 季</label><button type="button" class="icon-button" @click="toggleSeason(season)">{{ expandedSeasons.includes(season) ? '⌃' : '⌄' }}</button><div v-if="expandedSeasons.includes(season)" class="episode-list"><span v-for="episode in Math.min(selected.episodes || 0, 30)" :key="episode">E{{ String(episode).padStart(2, '0') }}</span><small v-if="!selected.episodes">暂无集信息</small></div></div></div></div><label><span>求片备注（可选）</span><textarea v-model="note" maxlength="1000" placeholder="例如：希望优先添加国语版" :disabled="!selectedCanSubmit"></textarea></label><p v-if="selectedState === 'pending'" class="hint">该作品已提交求片，等待管理员处理。</p><p v-else-if="selectedState === 'library'" class="hint">该作品已入库，无需重复提交。</p><button class="primary portal-request-submit" type="submit" :disabled="!selectedCanSubmit"><LoaderCircle v-if="submitting" class="spin" :size="17" /><Send v-else :size="17" />{{ submitting ? '提交中' : '订阅' }}</button></form>
          </template>
        </div>
      </dialog>
    </section>
    <MobileBottomNav :items="portalNav" active-path="/requests" aria-label="用户中心导航" />
  </div>
</template>

