<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { getApi, postApi } from '../shared/api';
import { AuthCard } from '../shared/components';

// AuthCard keeps the shared UiButton/UiTextField/UiFeedback auth contract;
// its native fields follow the reference login surface for reliable text.

const csrfToken = ref('');
const error = ref('');
const loading = ref(false);
onMounted(async () => { document.title = '登录 · Emby Apex'; try { csrfToken.value = (await getApi<{ csrf_token: string }>('/auth/csrf')).data?.csrf_token || ''; } catch { error.value = '无法建立安全会话，请刷新页面重试。'; } });
async function submit(credentials: { username: string; password: string; password2: string }): Promise<void> {
  if (loading.value || !csrfToken.value) return;
  loading.value = true; error.value = '';
  try { await postApi('/auth/login', { username: credentials.username, password: credentials.password }, csrfToken.value); window.location.assign('/dashboard'); }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '登录失败，请稍后重试。'; }
  finally { loading.value = false; }
}
</script>

<template>
  <AuthCard title="管理员登录" subtitle="使用 APEX 管理账号继续" brand-caption="媒体服务运营台" username-label="管理员账号" submit-label="登录" loading-label="登录中" :error="error" :loading="loading" @submit="submit" />
</template>
