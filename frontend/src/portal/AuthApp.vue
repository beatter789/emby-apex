<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { getApi, postApi, ApiError } from '../shared/api';
import { AuthCard } from '../shared/components';

// AuthCard keeps the shared UiButton/UiTextField/UiFeedback auth contract;
// its native fields follow the reference login surface for reliable text.

const props = defineProps<{ mode: 'login' | 'register' }>();
const csrfToken = ref('');
const error = ref('');
const loading = ref(false);

onMounted(async () => {
  document.title = `${props.mode === 'register' ? '注册' : '登录'} · Emby Apex`;
  try { csrfToken.value = (await getApi<{ csrf_token: string }>('/auth/csrf')).data?.csrf_token || ''; }
  catch { error.value = '无法建立安全会话，请刷新页面重试。'; }
});
async function submit(credentials: { username: string; password: string; password2: string }): Promise<void> {
  if (loading.value) return;
  error.value = '';
  if (!csrfToken.value) { error.value = '安全凭据缺失，请刷新页面重试。'; return; }
  if (props.mode === 'register' && credentials.password !== credentials.password2) { error.value = '两次输入的密码不一致。'; return; }
  loading.value = true;
  try {
    await postApi(props.mode === 'register' ? '/auth/register' : '/auth/login', { username: credentials.username, password: credentials.password, password2: credentials.password2 }, csrfToken.value);
    window.location.assign('/account');
  } catch (reason) {
    if (reason instanceof ApiError && reason.status === 401) error.value = '用户名或密码错误。';
    else error.value = reason instanceof Error ? reason.message : '操作失败，请稍后重试。';
  } finally { loading.value = false; }
}
</script>

<template>
  <AuthCard
    :title="props.mode === 'register' ? '注册用户中心' : '登录用户中心'"
    subtitle="管理媒体账户与求片"
    brand-caption="用户中心"
    :mode="props.mode"
    :submit-label="props.mode === 'register' ? '注册' : '登录'"
    :loading-label="props.mode === 'register' ? '注册中' : '登录中'"
    :link-href="props.mode === 'register' ? '/login' : '/register'"
    :link-label="props.mode === 'register' ? '已有账户，去登录' : '没有账户，去注册'"
    :error="error"
    :loading="loading"
    @submit="submit"
  />
</template>
