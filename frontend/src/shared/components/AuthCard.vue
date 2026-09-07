<script setup lang="ts">
import { ref } from 'vue';
import { CircleUserRound, Eye, EyeOff, LockKeyhole, LogIn, UserPlus } from 'lucide-vue-next';
import UiFeedback from './UiFeedback.vue';
import UiButton from './UiButton.vue';

const props = withDefaults(defineProps<{
  title: string;
  subtitle: string;
  brandCaption: string;
  mode?: 'login' | 'register';
  usernameLabel?: string;
  passwordLabel?: string;
  confirmPasswordLabel?: string;
  error?: string;
  loading?: boolean;
  submitLabel?: string;
  loadingLabel?: string;
  linkHref?: string;
  linkLabel?: string;
}>(), {
  mode: 'login',
  usernameLabel: '用户名',
  passwordLabel: '密码',
  confirmPasswordLabel: '确认密码',
  error: '',
  loading: false,
  submitLabel: '登录',
  loadingLabel: '处理中',
  linkHref: '',
  linkLabel: '',
});

const emit = defineEmits<{
  submit: [payload: { username: string; password: string; password2: string }];
}>();

const username = ref('');
const password = ref('');
const password2 = ref('');
const showPassword = ref(false);
const showPassword2 = ref(false);

function submit(): void {
  if (props.loading) return;
  emit('submit', { username: username.value, password: password.value, password2: password2.value });
}
</script>

<template>
  <main class="login-root" data-login-visual-profile="glass">
    <div class="login-ambient-light" aria-hidden="true"><div class="login-ambient-light__wash" /></div>
    <div class="auth-wrapper">
      <section class="login-card" aria-labelledby="login-title">
        <div class="login-card__surface" aria-hidden="true" />
        <div class="login-card__content">
          <header class="login-head">
            <img class="login-logo-mark" :src="'/static/logoicon.png'" alt="Emby Apex" width="72" height="72">
            <div class="login-logo"><h1 id="login-title" class="login-title">Emby Apex</h1><p class="login-subtitle">{{ brandCaption }}</p></div>
          </header>
          <div class="login-body">
            <div class="login-heading"><h2>{{ title }}</h2><p>{{ subtitle }}</p></div>
            <UiFeedback v-if="error" class="login-alert" :message="error" />
            <form class="login-form" autocomplete="on" @submit.prevent="submit">
              <label class="native-login-field login-input">
                <CircleUserRound class="native-login-field__icon" :size="19" aria-hidden="true" />
                <span class="sr-only">{{ usernameLabel }}</span>
                <input v-model="username" name="username" type="text" autocomplete="username" autocapitalize="none" spellcheck="false" :placeholder="usernameLabel" :aria-label="usernameLabel" required>
              </label>
              <label class="native-login-field native-login-field--password login-input">
                <LockKeyhole class="native-login-field__icon" :size="19" aria-hidden="true" />
                <span class="sr-only">{{ passwordLabel }}</span>
                <input v-model="password" name="password" :type="showPassword ? 'text' : 'password'" autocomplete="current-password" :placeholder="passwordLabel" :aria-label="passwordLabel" required>
                <button class="native-login-field__toggle" type="button" :aria-label="showPassword ? '隐藏密码' : '显示密码'" :title="showPassword ? '隐藏密码' : '显示密码'" @click="showPassword = !showPassword">
                  <EyeOff v-if="showPassword" :size="18" aria-hidden="true" /><Eye v-else :size="18" aria-hidden="true" />
                </button>
              </label>
              <label v-if="mode === 'register'" class="native-login-field native-login-field--password login-input">
                <LockKeyhole class="native-login-field__icon" :size="19" aria-hidden="true" />
                <span class="sr-only">{{ confirmPasswordLabel }}</span>
                <input v-model="password2" name="password2" :type="showPassword2 ? 'text' : 'password'" autocomplete="new-password" :placeholder="confirmPasswordLabel" :aria-label="confirmPasswordLabel" required>
                <button class="native-login-field__toggle" type="button" :aria-label="showPassword2 ? '隐藏确认密码' : '显示确认密码'" :title="showPassword2 ? '隐藏确认密码' : '显示确认密码'" @click="showPassword2 = !showPassword2">
                  <EyeOff v-if="showPassword2" :size="18" aria-hidden="true" /><Eye v-else :size="18" aria-hidden="true" />
                </button>
              </label>
              <UiButton class="login-submit" type="submit" :loading="loading" :disabled="loading" block>
                <UserPlus v-if="!loading && mode === 'register'" :size="17" aria-hidden="true" /><LogIn v-else-if="!loading" :size="17" aria-hidden="true" />
                {{ loading ? loadingLabel : submitLabel }}
              </UiButton>
            </form>
            <p v-if="linkHref && linkLabel" class="login-link"><a :href="linkHref">{{ linkLabel }}</a></p>
          </div>
          <footer class="login-foot"><span>Emby Apex</span><span class="login-version">安全会话</span></footer>
        </div>
      </section>
    </div>
  </main>
</template>
