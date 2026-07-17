"use client";

import { useEffect, useState } from "react";

const STORAGE_KEY = "paoxx.theme.v1";

type Theme = "light" | "dark";

function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("light");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    const next: Theme = saved === "dark" || saved === "light"
      ? saved
      : window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    setTheme(next);
    applyTheme(next);
    setMounted(true);
  }, []);

  function toggle() {
    const next: Theme = theme === "light" ? "dark" : "light";
    setTheme(next);
    applyTheme(next);
    window.localStorage.setItem(STORAGE_KEY, next);
  }

  return (
    <button
      aria-label={theme === "light" ? "切换到深色主题" : "切换到浅色主题"}
      className="grid h-9 w-9 place-items-center rounded-md border border-border-subtle bg-surface-panel text-sm text-text-secondary transition hover:border-primary-100 hover:text-primary-700"
      onClick={toggle}
      title={theme === "light" ? "深色主题" : "浅色主题"}
      type="button"
    >
      <span aria-hidden="true">{mounted && theme === "dark" ? "☀" : "◐"}</span>
    </button>
  );
}
