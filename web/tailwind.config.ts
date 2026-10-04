import type { Config } from 'tailwindcss';

// Theme tokens live as CSS variables in src/index.css (light default,
// html[data-theme="dark"] variant), extracted from the designs' <helmet><style>.
// Step-state colours are named after the step states (plans/01 §3).
const v = (name: string) => `var(--${name})`;

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: v('bg'),
        bg2: v('bg2'),
        panel: v('panel'),
        panel2: v('panel2'),
        line: v('line'),
        line2: v('line2'),
        dot: v('dot'),
        fg: v('fg'),
        fg2: v('fg2'),
        fg3: v('fg3'),
        accent: v('accent'),
        state: {
          planned: v('s-planned'),
          ready: v('s-ready'),
          leased: v('s-leased'),
          claimed: v('s-claimed'),
          committed: v('s-committed'),
          rejected: v('s-rejected'),
          input: v('s-input'),
          dead: v('s-dead'),
        },
      },
      fontFamily: {
        sans: ['"IBM Plex Sans"', 'system-ui', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'monospace'],
      },
      boxShadow: {
        card: v('shadow'),
      },
      fontSize: {
        '2xs': ['10.5px', '1.4'],
        xs: ['11px', '1.45'],
        'xs+': ['11.5px', '1.45'],
        sm: ['12px', '1.45'],
        'sm+': ['12.5px', '1.45'],
        base: ['13px', '1.5'],
        'base+': ['13.5px', '1.5'],
        md: ['14px', '1.5'],
        lg: ['15px', '1.45'],
        xl: ['16px', '1.4'],
        '2xl': ['18px', '1.35'],
        '3xl': ['22px', '1.35'],
      },
    },
  },
  plugins: [],
} satisfies Config;
