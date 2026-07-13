import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        surface: {
          canvas: "#F3F4F6",
          bright: "#F8F9FB",
          panel: "#FFFFFF",
          low: "#EDEEF0"
        },
        text: {
          primary: "#0B0F14",
          secondary: "#354042",
          muted: "#A1A3A8"
        },
        primary: {
          50: "#EEF0FF",
          100: "#DEE0FF",
          500: "#4755AE",
          700: "#2E3C95",
          800: "#26337F"
        },
        "border-subtle": "#E5E7EB",
        good: "#10B981",
        warn: "#F59E0B",
        risk: "#EF4444"
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
