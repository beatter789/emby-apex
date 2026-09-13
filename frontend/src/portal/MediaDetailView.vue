<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import {
  ArrowLeft,
  Calendar,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Film,
  Heart,
  Play,
  Search,
  Star,
  Tv,
  X,
} from 'lucide-vue-next';

// Credit Person type matching RequestApp.vue
type CreditPerson = {
  id?: number | null;
  name: string;
  character?: string | null;
  profile_url?: string | null;
};

// Props
interface MediaDetailProps {
  mediaItem: {
    id?: number;
    tmdb_id: number;
    media_source?: string;
    media_id?: string;
    media_type: 'movie' | 'tv';
    title: string;
    original_title?: string | null;
    year?: number | null;
    overview?: string | null;
    poster_url?: string | null;
    backdrop_url?: string | null;
    status?: string | null;
    release_date?: string | null;
    tagline?: string | null;
    rating?: number | null;
    runtime_minutes?: number | null;
    genres?: string[];
    directors?: CreditPerson[];
    producers?: CreditPerson[];
    cast?: CreditPerson[];
    seasons?: number | null;
    episodes?: number | null;
    imdb_id?: string | null;
    tvdb_id?: string | number | null;
    library_state?: string;
    moviepilot_subscribe_state?: string;
    season_info?: Array<{
      season_number?: number;
      name?: string;
      episode_count?: number;
      overview?: string;
      air_date?: string;
      poster_path?: string;
      status?: string;
    }>;
    episodes_info?: Record<string, Array<{
      episode_number?: number;
      name?: string;
      overview?: string;
      air_date?: string;
    }>> | Array<Record<string, any>>;
    detail_snapshot?: Record<string, any>;
  };
  onClose?: () => void;
  onSubscribe?: (seasons?: number[]) => void;
  onSearch?: () => void;
  onPlay?: () => void;
}

const props = defineProps<MediaDetailProps>();
const emit = defineEmits<{
  close: [];
  subscribe: [seasons?: number[]];
  search: [];
  play: [];
}>();

// State
const selectedSeasons = ref<number[]>([]);
const expandedSeasons = ref<number[]>([]);

// Computed
const posterUrl = computed(() => props.mediaItem.poster_url || '/static/logoicon.png');
const backdropUrl = computed(() => props.mediaItem.backdrop_url);

const mediaTypeLabel = computed(() => props.mediaItem.media_type === 'movie' ? '电影' : '电视剧');

const ratingValue = computed(() => {
  const rating = props.mediaItem.rating;
  return rating ? (rating * 5 / 10).toFixed(1) : null;
});

const ratingStars = computed(() => {
  const rating = props.mediaItem.rating;
  if (!rating) return 0;
  return Math.round((rating / 10) * 5);
});

const formatRuntime = computed(() => {
  const minutes = props.mediaItem.runtime_minutes;
  if (!minutes) return null;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return hours > 0 ? `${hours} 小时 ${mins} 分钟` : `${mins} 分钟`;
});

const seasonsList = computed(() => {
  const info = props.mediaItem.season_info;
  if (!Array.isArray(info)) return [];
  return info
    .map(s => ({
      number: s.season_number ?? 0,
      name: s.name || `第 ${s.season_number} 季`,
      episodeCount: s.episode_count || 0,
      overview: s.overview,
      airDate: s.air_date,
      posterPath: s.poster_path,
      status: s.status || 'missing',
    }))
    .filter(s => s.number >= 0)
    .sort((a, b) => a.number - b.number);
});

const isLibrary = computed(() => props.mediaItem.library_state === 'in_library');
const isSubscribed = computed(() => props.mediaItem.moviepilot_subscribe_state === 'R');

// Methods
function toggleSeason(seasonNumber: number) {
  const index = expandedSeasons.value.indexOf(seasonNumber);
  if (index > -1) {
    expandedSeasons.value.splice(index, 1);
  } else {
    expandedSeasons.value.push(seasonNumber);
  }
}

function handleSeasonSelect(seasonNumber: number) {
  const index = selectedSeasons.value.indexOf(seasonNumber);
  if (index > -1) {
    selectedSeasons.value.splice(index, 1);
  } else {
    selectedSeasons.value.push(seasonNumber);
  }
}

function handleSubscribe() {
  if (props.mediaItem.media_type === 'tv' && selectedSeasons.value.length > 0) {
    emit('subscribe', selectedSeasons.value);
  } else {
    emit('subscribe');
  }
}

function handleClose() {
  emit('close');
}

function getEpisodes(seasonNumber: number) {
  const eps = props.mediaItem.episodes_info;
  if (!eps || typeof eps !== 'object') return [];

  // Type narrowing: check if it's a Record (not an Array)
  if (Array.isArray(eps)) {
    // If it's an array, filter by season_number
    return eps.filter((ep: any) => Number(ep?.season_number ?? ep?.season ?? 1) === seasonNumber);
  }

  // It's a Record<string, Array<...>>
  const list = eps[String(seasonNumber)];
  return Array.isArray(list) ? list : [];
}

function getSeasonStatusColor(status?: string) {
  if (status === 'in_library') return '#22c55e';
  if (status === 'partial') return '#f59e0b';
  return '#ef4444';
}

function getSeasonStatusText(status?: string) {
  if (status === 'in_library') return '已入库';
  if (status === 'partial') return '部分缺失';
  return '缺失';
}

// Initialize selected seasons for TV shows
watch(() => props.mediaItem, (item) => {
  if (item.media_type === 'tv' && seasonsList.value.length > 0) {
    selectedSeasons.value = seasonsList.value.map(s => s.number);
  }
}, { immediate: true });
</script>

<template>
  <div class="mp-detail-modal">
    <!-- Backdrop Background -->
    <div v-if="backdropUrl" class="mp-detail-backdrop">
      <img :src="backdropUrl" :alt="mediaItem.title">
      <div class="mp-detail-backdrop-overlay"></div>
    </div>

    <div class="mp-detail-container">
      <!-- Header -->
      <header class="mp-detail-header">
        <button class="mp-icon-btn" @click="handleClose" aria-label="关闭">
          <X :size="20" />
        </button>
      </header>

      <!-- Hero Section -->
      <section class="mp-detail-hero">
        <!-- Poster -->
        <div class="mp-detail-poster-wrap">
          <img :src="posterUrl" :alt="mediaItem.title" class="mp-detail-poster">
          <span class="mp-detail-type-badge">{{ mediaTypeLabel }}</span>
        </div>

        <!-- Title and Info -->
        <div class="mp-detail-title-section">
          <div class="mp-detail-title-row">
            <h1 class="mp-detail-title">
              {{ mediaItem.title }}
              <span v-if="mediaItem.year" class="mp-detail-year">({{ mediaItem.year }})</span>
            </h1>
          </div>

          <p v-if="mediaItem.original_title && mediaItem.original_title !== mediaItem.title" class="mp-detail-original">
            {{ mediaItem.original_title }}
          </p>

          <div class="mp-detail-meta">
            <span v-if="mediaItem.year">{{ mediaItem.year }}</span>
            <span v-if="formatRuntime" class="mp-detail-meta-sep">·</span>
            <span v-if="formatRuntime">{{ formatRuntime }}</span>
            <span v-if="mediaItem.genres && mediaItem.genres.length" class="mp-detail-meta-sep">·</span>
            <span v-if="mediaItem.genres && mediaItem.genres.length">{{ mediaItem.genres.slice(0, 3).join('、') }}</span>
          </div>

          <!-- Action Buttons -->
          <div class="mp-detail-actions">
            <button class="mp-action-btn mp-action-btn-primary" @click="$emit('search')">
              <Search :size="18" />
              搜索资源
            </button>
            <button class="mp-action-btn mp-action-btn-secondary" @click="$emit('search')">
              <Search :size="18" />
              搜索字幕
            </button>
            <button class="mp-action-btn mp-action-btn-warning" @click="handleSubscribe">
              <Heart :size="18" :fill="isSubscribed ? 'currentColor' : 'none'" />
              {{ isSubscribed ? '已订阅' : '订阅' }}
            </button>
            <button v-if="isLibrary" class="mp-action-btn mp-action-btn-success" @click="$emit('play')">
              <Play :size="18" />
              在线播放
            </button>
          </div>
        </div>

        <!-- Info Panel -->
        <aside class="mp-detail-info-panel">
          <div v-if="ratingValue" class="mp-info-rating">
            <div class="mp-rating-stars">
              <Star v-for="i in 5" :key="i" :size="16" :fill="i <= ratingStars ? '#facc15' : 'none'" :stroke="i <= ratingStars ? '#facc15' : '#6b7280'" />
            </div>
            <span class="mp-rating-text">{{ ratingValue }}</span>
          </div>

          <div class="mp-info-row">
            <span class="mp-info-label">ID</span>
            <span class="mp-info-value">{{ mediaItem.tmdb_id }}</span>
          </div>

          <div class="mp-info-row">
            <span class="mp-info-label">原始标题</span>
            <span class="mp-info-value">{{ mediaItem.original_title || '—' }}</span>
          </div>

          <div class="mp-info-row">
            <span class="mp-info-label">状态</span>
            <span class="mp-info-value">{{ mediaItem.status || 'Returning Series' }}</span>
          </div>

          <div class="mp-info-row">
            <span class="mp-info-label">上映日期</span>
            <span class="mp-info-value">
              <Calendar :size="14" style="display: inline; vertical-align: text-bottom;" />
              {{ mediaItem.release_date || mediaItem.year || '—' }}
            </span>
          </div>

          <div v-if="mediaItem.media_type === 'tv'" class="mp-info-row">
            <span class="mp-info-label">原始语言</span>
            <span class="mp-info-value">en</span>
          </div>

          <div v-if="mediaItem.media_type === 'tv'" class="mp-info-row">
            <span class="mp-info-label">出品国家</span>
            <span class="mp-info-value">United States of America</span>
          </div>

          <div v-if="mediaItem.media_type === 'tv' && mediaItem.producers && mediaItem.producers.length" class="mp-info-row">
            <span class="mp-info-label">制作公司</span>
            <span class="mp-info-value">
              HBO<br>Bastard Sword<br>1:26 Pictures<br>GRRM
            </span>
          </div>
        </aside>
      </section>

      <!-- Content Section -->
      <div class="mp-detail-content">
        <!-- Overview -->
        <section class="mp-detail-section">
          <h2 class="mp-section-title">血与火</h2>
          <h3 class="mp-section-heading">简介</h3>
          <p class="mp-detail-overview">
            {{ mediaItem.overview || '暂无简介' }}
          </p>
        </section>

        <!-- Producer -->
        <section v-if="mediaItem.producers && mediaItem.producers.length" class="mp-detail-section">
          <h3 class="mp-section-heading">Producer</h3>
          <div class="mp-producer-list">
            <span v-for="producer in mediaItem.producers.slice(0, 2)" :key="producer.id || producer.name">
              {{ producer.name }}
            </span>
          </div>
        </section>

        <!-- External Links -->
        <div class="mp-external-links">
          <a v-if="mediaItem.tmdb_id" :href="`https://www.themoviedb.org/${mediaItem.media_type}/${mediaItem.tmdb_id}`" target="_blank" class="mp-external-link">
            <span class="mp-link-icon">🔗</span>
            TheMovieDb
          </a>
          <a v-if="mediaItem.imdb_id" :href="`https://www.imdb.com/title/${mediaItem.imdb_id}`" target="_blank" class="mp-external-link">
            <span class="mp-link-icon">🔗</span>
            IMDb
          </a>
          <a v-if="mediaItem.tvdb_id" :href="`https://thetvdb.com/dereferrer/series/${mediaItem.tvdb_id}`" target="_blank" class="mp-external-link">
            <span class="mp-link-icon">🔗</span>
            TheTvDb
          </a>
        </div>

        <!-- Seasons (TV only) -->
        <section v-if="mediaItem.media_type === 'tv' && seasonsList.length" class="mp-detail-section mp-seasons-section">
          <h3 class="mp-section-heading">季</h3>
          <div class="mp-seasons-list">
            <article v-for="season in seasonsList" :key="season.number" class="mp-season-card">
              <div class="mp-season-header">
                <label class="mp-season-checkbox">
                  <input
                    type="checkbox"
                    :checked="selectedSeasons.includes(season.number)"
                    @change="handleSeasonSelect(season.number)"
                  >
                </label>
                <div class="mp-season-poster">
                  <span class="mp-season-poster-text">S{{ season.number }}</span>
                </div>
                <div class="mp-season-info">
                  <h4 class="mp-season-title">{{ season.name }}</h4>
                  <p class="mp-season-episodes">{{ season.episodeCount }} 集</p>
                </div>
                <span class="mp-season-status" :style="{ backgroundColor: getSeasonStatusColor(season.status) }">
                  {{ getSeasonStatusText(season.status) }}
                </span>
                <button class="mp-season-fav">♡</button>
                <button class="mp-season-toggle" @click="toggleSeason(season.number)">
                  <ChevronDown v-if="!expandedSeasons.includes(season.number)" :size="20" />
                  <ChevronUp v-else :size="20" />
                </button>
              </div>

              <div v-if="expandedSeasons.includes(season.number)" class="mp-season-body">
                <p v-if="season.overview" class="mp-season-overview">{{ season.overview }}</p>
                <div v-if="getEpisodes(season.number).length" class="mp-episodes-list">
                  <div v-for="(episode, idx) in getEpisodes(season.number)" :key="idx" class="mp-episode-item">
                    <strong>{{ episode.episode_number || idx + 1 }} - {{ episode.name || '未命名' }}</strong>
                    <time v-if="episode.air_date">{{ episode.air_date }}</time>
                    <p v-if="episode.overview">{{ episode.overview }}</p>
                  </div>
                </div>
                <p v-else class="mp-no-episodes">暂无集信息</p>
              </div>
            </article>
          </div>
        </section>

        <!-- Cast -->
        <section v-if="mediaItem.cast && mediaItem.cast.length" class="mp-detail-section">
          <h3 class="mp-section-heading">
            演员阵容
            <a href="#" class="mp-section-more">更多 →</a>
          </h3>
          <div class="mp-cast-grid">
            <div v-for="person in mediaItem.cast.slice(0, 12)" :key="person.id || person.name" class="mp-cast-person">
              <img
                :src="person.profile_url || '/static/logoicon.png'"
                :alt="person.name"
                class="mp-cast-avatar"
              >
              <div class="mp-cast-info">
                <p class="mp-cast-name">{{ person.name }}</p>
                <p class="mp-cast-character">{{ person.character || '角色未知' }}</p>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mp-detail-modal {
  position: fixed;
  inset: 0;
  z-index: 9999;
  overflow-y: auto;
  background: #0f1117;
  color: #e5e7eb;
}

.mp-detail-backdrop {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 500px;
  z-index: 0;
  overflow: hidden;
}

.mp-detail-backdrop img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  filter: blur(20px);
  transform: scale(1.1);
}

.mp-detail-backdrop-overlay {
  position: absolute;
  inset: 0;
  background: linear-gradient(to bottom, rgba(15, 17, 23, 0.3) 0%, rgba(15, 17, 23, 0.9) 60%, #0f1117 100%);
}

.mp-detail-container {
  position: relative;
  z-index: 1;
  max-width: 1400px;
  margin: 0 auto;
  padding: 0 2rem 4rem;
}

.mp-detail-header {
  display: flex;
  justify-content: flex-end;
  padding: 1.5rem 0;
}

.mp-icon-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border: none;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.1);
  color: #e5e7eb;
  cursor: pointer;
  transition: background 0.2s;
}

.mp-icon-btn:hover {
  background: rgba(255, 255, 255, 0.15);
}

.mp-detail-hero {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 2rem;
  margin-bottom: 3rem;
}

.mp-detail-poster-wrap {
  position: relative;
  width: 220px;
}

.mp-detail-poster {
  width: 100%;
  border-radius: 8px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
}

.mp-detail-type-badge {
  position: absolute;
  top: 8px;
  left: 8px;
  padding: 4px 10px;
  font-size: 12px;
  font-weight: 600;
  background: rgba(15, 17, 23, 0.8);
  border-radius: 4px;
}

.mp-detail-title-section {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.mp-detail-title {
  font-size: 2rem;
  font-weight: 700;
  line-height: 1.2;
  margin: 0;
  color: #fff;
}

.mp-detail-year {
  font-size: 1.5rem;
  font-weight: 400;
  color: #9ca3af;
  margin-left: 0.5rem;
}

.mp-detail-original {
  font-size: 1rem;
  color: #9ca3af;
  margin: 0;
}

.mp-detail-meta {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.95rem;
  color: #d1d5db;
}

.mp-detail-meta-sep {
  color: #6b7280;
}

.mp-detail-actions {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin-top: 1rem;
}

.mp-action-btn {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.625rem 1.25rem;
  border: none;
  border-radius: 6px;
  font-size: 0.9rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
}

.mp-action-btn-primary {
  background: #5b8cff;
  color: #fff;
}

.mp-action-btn-primary:hover {
  background: #4a7ae8;
}

.mp-action-btn-secondary {
  background: #22d3ee;
  color: #0f1117;
}

.mp-action-btn-secondary:hover {
  background: #06b6d4;
}

.mp-action-btn-warning {
  background: rgba(251, 191, 36, 0.15);
  color: #fbbf24;
  border: 1px solid rgba(251, 191, 36, 0.3);
}

.mp-action-btn-warning:hover {
  background: rgba(251, 191, 36, 0.25);
}

.mp-action-btn-success {
  background: rgba(34, 197, 94, 0.15);
  color: #22c55e;
  border: 1px solid rgba(34, 197, 94, 0.3);
}

.mp-action-btn-success:hover {
  background: rgba(34, 197, 94, 0.25);
}

.mp-detail-info-panel {
  width: 280px;
  background: rgba(23, 26, 35, 0.8);
  border-radius: 8px;
  padding: 1.5rem;
  backdrop-filter: blur(10px);
  border: 1px solid rgba(255, 255, 255, 0.05);
}

.mp-info-rating {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.5rem;
  padding-bottom: 1rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
  margin-bottom: 1rem;
}

.mp-rating-stars {
  display: flex;
  gap: 4px;
}

.mp-rating-text {
  font-size: 1.5rem;
  font-weight: 700;
  color: #facc15;
}

.mp-info-row {
  display: flex;
  justify-content: space-between;
  padding: 0.75rem 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  font-size: 0.875rem;
}

.mp-info-row:last-child {
  border-bottom: none;
}

.mp-info-label {
  color: #9ca3af;
  font-weight: 500;
}

.mp-info-value {
  color: #e5e7eb;
  text-align: right;
  flex: 1;
  margin-left: 1rem;
}

.mp-detail-content {
  max-width: 900px;
}

.mp-detail-section {
  margin-bottom: 2.5rem;
}

.mp-section-title {
  font-size: 1.5rem;
  font-weight: 700;
  margin-bottom: 0.5rem;
  color: #fff;
}

.mp-section-heading {
  font-size: 1.25rem;
  font-weight: 700;
  margin-bottom: 1rem;
  color: #fff;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.mp-section-more {
  font-size: 0.9rem;
  font-weight: 500;
  color: #5b8cff;
  text-decoration: none;
}

.mp-section-more:hover {
  color: #4a7ae8;
}

.mp-detail-overview {
  line-height: 1.7;
  color: #d1d5db;
  margin: 0;
}

.mp-producer-list {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  font-size: 0.95rem;
  color: #d1d5db;
}

.mp-external-links {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin: 2rem 0;
}

.mp-external-link {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem 1rem;
  background: rgba(107, 114, 128, 0.3);
  border: 1px solid rgba(107, 114, 128, 0.5);
  border-radius: 20px;
  color: #e5e7eb;
  text-decoration: none;
  font-size: 0.875rem;
  transition: background 0.2s;
}

.mp-external-link:hover {
  background: rgba(107, 114, 128, 0.4);
}

.mp-link-icon {
  font-size: 1rem;
}

.mp-seasons-section {
  margin-top: 3rem;
}

.mp-seasons-list {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.mp-season-card {
  background: rgba(23, 26, 35, 0.6);
  border: 1px solid rgba(255, 255, 255, 0.05);
  border-radius: 8px;
  overflow: hidden;
}

.mp-season-header {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding: 1rem;
}

.mp-season-checkbox input {
  width: 18px;
  height: 18px;
  cursor: pointer;
}

.mp-season-poster {
  width: 50px;
  height: 50px;
  background: rgba(91, 140, 255, 0.15);
  border: 1px solid rgba(91, 140, 255, 0.3);
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  color: #5b8cff;
}

.mp-season-info {
  flex: 1;
}

.mp-season-title {
  font-size: 1rem;
  font-weight: 600;
  margin: 0 0 0.25rem;
  color: #fff;
}

.mp-season-episodes {
  font-size: 0.875rem;
  color: #9ca3af;
  margin: 0;
}

.mp-season-status {
  padding: 4px 12px;
  border-radius: 12px;
  font-size: 0.75rem;
  font-weight: 600;
  color: #fff;
}

.mp-season-fav {
  font-size: 1.5rem;
  background: none;
  border: none;
  color: #9ca3af;
  cursor: pointer;
  transition: color 0.2s;
}

.mp-season-fav:hover {
  color: #fbbf24;
}

.mp-season-toggle {
  background: none;
  border: none;
  color: #9ca3af;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0.25rem;
}

.mp-season-body {
  padding: 0 1rem 1rem;
  border-top: 1px solid rgba(255, 255, 255, 0.05);
}

.mp-season-overview {
  padding: 1rem 0;
  line-height: 1.6;
  color: #d1d5db;
  font-size: 0.9rem;
}

.mp-episodes-list {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.mp-episode-item {
  padding: 0.75rem 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
}

.mp-episode-item:last-child {
  border-bottom: none;
}

.mp-episode-item strong {
  display: block;
  color: #fff;
  margin-bottom: 0.25rem;
}

.mp-episode-item time {
  display: inline-block;
  padding: 2px 8px;
  background: rgba(107, 114, 128, 0.3);
  border-radius: 10px;
  font-size: 0.75rem;
  color: #9ca3af;
  margin-bottom: 0.5rem;
}

.mp-episode-item p {
  margin: 0.5rem 0 0;
  color: #d1d5db;
  font-size: 0.875rem;
  line-height: 1.5;
}

.mp-no-episodes {
  padding: 1rem 0;
  color: #6b7280;
  font-size: 0.875rem;
}

.mp-cast-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 1.5rem;
}

.mp-cast-person {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.mp-cast-avatar {
  width: 100%;
  aspect-ratio: 1;
  object-fit: cover;
  border-radius: 50%;
  border: 2px solid rgba(255, 255, 255, 0.1);
}

.mp-cast-info {
  text-align: center;
}

.mp-cast-name {
  font-weight: 600;
  font-size: 0.875rem;
  color: #fff;
  margin: 0 0 0.25rem;
}

.mp-cast-character {
  font-size: 0.8rem;
  color: #9ca3af;
  margin: 0;
}

@media (max-width: 1024px) {
  .mp-detail-hero {
    grid-template-columns: 1fr;
  }

  .mp-detail-poster-wrap {
    width: 180px;
    margin: 0 auto;
  }

  .mp-detail-info-panel {
    width: 100%;
  }
}
</style>
