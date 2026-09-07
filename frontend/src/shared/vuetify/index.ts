import 'vuetify/styles';

import { createVuetify, type ThemeDefinition } from 'vuetify';
import {
  VAlert,
  VApp,
  VBtn,
  VProgressCircular,
  VTextField,
} from 'vuetify/components';

const apexDarkTheme: ThemeDefinition = {
  dark: true,
  colors: {
    background: '#0B0912',
    surface: '#13111C',
    'surface-variant': '#1B1728',
    primary: '#7C5CFF',
    secondary: '#9278FF',
    info: '#60A5FA',
    success: '#34D399',
    warning: '#FBBF24',
    error: '#FB7185',
    'on-background': '#F7F5FF',
    'on-surface': '#F7F5FF',
  },
  variables: {
    'border-color': '#302A43',
    'high-emphasis-opacity': 0.94,
    'medium-emphasis-opacity': 0.72,
    'low-emphasis-opacity': 0.48,
  },
};

/** Shared Vuetify configuration for both the admin and portal entry points. */
export const apexVuetify = createVuetify({
  components: { VAlert, VApp, VBtn, VProgressCircular, VTextField },
  theme: {
    defaultTheme: 'apexDark',
    themes: { apexDark: apexDarkTheme },
  },
  defaults: {
    VBtn: {
      color: 'primary',
      rounded: 'sm',
      minHeight: 44,
      elevation: 0,
    },
    VTextField: {
      color: 'primary',
      variant: 'outlined',
      density: 'comfortable',
      hideDetails: 'auto',
    },
    VAlert: {
      rounded: 'sm',
      density: 'comfortable',
    },
  },
});

export default apexVuetify;
