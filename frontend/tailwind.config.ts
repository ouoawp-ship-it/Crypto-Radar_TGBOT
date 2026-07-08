import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        surface: {
          950: "#071016",
          900: "#0b141d",
          850: "#101b26",
          800: "#152434"
        },
        cyanline: "#28d6c7",
        violetline: "#7c5cff",
        risk: "#ff4d5e",
        warn: "#f59e0b",
        good: "#22c55e"
      },
      boxShadow: {
        glow: "0 18px 60px rgba(40, 214, 199, 0.12)",
        panel: "0 20px 80px rgba(0,0,0,0.32)"
      }
    }
  },
  plugins: []
};

export default config;
