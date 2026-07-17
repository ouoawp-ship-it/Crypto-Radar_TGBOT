import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "var(--font-geist-sans)",
          '"PingFang SC"',
          '"Hiragino Sans GB"',
          '"Microsoft YaHei UI"',
          '"Noto Sans SC"',
          "ui-sans-serif",
          "system-ui",
          "sans-serif"
        ]
      },
      fontWeight: {
        medium: "500",
        semibold: "560",
        bold: "650"
      },
      colors: {
        surface: {
          canvas: "rgb(var(--surface-canvas) / <alpha-value>)",
          bright: "rgb(var(--surface-bright) / <alpha-value>)",
          panel: "rgb(var(--surface-panel) / <alpha-value>)",
          low: "rgb(var(--surface-low) / <alpha-value>)",
          container: "rgb(var(--surface-container) / <alpha-value>)",
          "container-low": "rgb(var(--surface-container-low) / <alpha-value>)"
        },
        text: {
          primary: "rgb(var(--text-primary) / <alpha-value>)",
          secondary: "rgb(var(--text-secondary) / <alpha-value>)",
          muted: "rgb(var(--text-muted) / <alpha-value>)"
        },
        primary: {
          50: "rgb(var(--primary-50) / <alpha-value>)",
          100: "rgb(var(--primary-100) / <alpha-value>)",
          500: "rgb(var(--primary-500) / <alpha-value>)",
          600: "rgb(var(--primary-600) / <alpha-value>)",
          700: "rgb(var(--primary-700) / <alpha-value>)",
          800: "rgb(var(--primary-800) / <alpha-value>)"
        },
        "border-subtle": "rgb(var(--border-subtle) / <alpha-value>)",
        good: "rgb(var(--good) / <alpha-value>)",
        warn: "rgb(var(--warn) / <alpha-value>)",
        risk: "rgb(var(--risk) / <alpha-value>)"
      },
      borderRadius: {
        lg: "0.75rem",
        xl: "1rem"
      },
      boxShadow: {
        soft: "0 1px 2px rgba(15, 23, 42, 0.04)",
        floating: "0 8px 24px rgba(15, 23, 42, 0.08)"
      }
    }
  },
  plugins: []
};

export default config;
