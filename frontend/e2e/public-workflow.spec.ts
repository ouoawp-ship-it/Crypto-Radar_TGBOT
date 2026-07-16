import { expect, Page, test } from "@playwright/test";

const signal = {
  id: 7,
  public_ref: "sig_e2e_btc",
  time: "2026-07-16T12:00:00+00:00",
  symbol: "BTCUSDT",
  coin: "BTC",
  module: "launch",
  status: "sent",
  signal_type: "启动雷达",
  score: 88,
  stage: "active",
  excerpt: "价格与 OI 同步增强，进入启动观察。",
  display: { title: "BTC 启动信号", module_label: "启动雷达", status_label: "已发送", summary: "价格与 OI 同步增强，进入启动观察。", card_tone: "good" }
};

const intelligence = {
  self_rank: { available: true, percentile: 96, rank: 2, sample_size: 40, method: "同币历史" },
  market_strength_rank: { available: true, percentile: 92, rank: 3, sample_size: 80, method: "同模块横截面" },
  market_absolute_rank: { available: false, sample_size: 1, reason: "样本积累中" },
  lifecycle: { state: "enhancing", label: "增强", basis: "规则分数较上次提高 8.0" },
  resonance: {
    active_count: 3,
    method: "跨模块同时出现，不推断多空方向",
    windows: ["15m", "30m", "1h", "4h", "1d"].map((key, index) => ({ key, active: index < 3, module_count: index < 3 ? 2 : 1, signal_count: 2 }))
  }
};

const market = {
  symbol: "BTCUSDT",
  coin: "BTC",
  status: "fresh",
  updated_at: "2026-07-16T12:00:00+00:00",
  age_sec: 8,
  metrics: {
    price: { value: 65000, unit: "usd", source: "binance_futures", status: "fresh", age_sec: 8 },
    price_24h_pct: { value: 2.4, unit: "percent", source: "binance_futures", status: "fresh", age_sec: 8 },
    quote_volume: { value: 1_200_000_000, unit: "usd", source: "binance_futures", status: "fresh", age_sec: 8 },
    oi_value: { value: 820_000_000, unit: "usd", source: "binance_futures", status: "fresh", age_sec: 8 },
    price_15m_pct: { value: 0.6, unit: "percent", source: "binance_futures", status: "fresh", age_sec: 8 },
    price_1h_pct: { value: 1.1, unit: "percent", source: "binance_futures", status: "fresh", age_sec: 8 },
    oi_15m_pct: { value: 1.8, unit: "percent", source: "binance_futures", status: "fresh", age_sec: 8 },
    funding_pct: { value: -0.02, unit: "percent_per_cycle", source: "binance_futures", status: "fresh", age_sec: 8 }
  },
  funding_exchanges: [{ exchange: "Binance", funding_pct: -0.02, interval_hours: 8 }],
  tiers: { liquidity: "高流动性" }
};

async function mockPublicApi(page: Page) {
  await page.route("**/public-api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/public-api/telemetry") return route.fulfill({ status: 202, json: { ok: true } });
    if (url.pathname === "/public-api/signals") return route.fulfill({ json: { ok: true, data: { items: [signal], count: 1 } } });
    if (url.pathname === "/public-api/signals/stats") return route.fulfill({ json: { ok: true, data: { total: 1, sent: 1, blocked: 0, failed: 0, skipped: 0 } } });
    if (url.pathname === "/public-api/radar/intelligence") return route.fulfill({ json: { ok: true, data: {
      data_status: "ready", summary: { signals: 1, symbols: 1, resonance_symbols: 1, enhancing_symbols: 1 },
      items: [{ signal, intelligence }],
      boards: [
        { key: "launch", title: "启动候选", description: "启动模块最新高分信号。", count: 1, items: [{ signal, intelligence }] },
        { key: "resonance", title: "跨模块共振", description: "至少两个雷达模块。", count: 1, items: [{ signal, intelligence }] },
        { key: "funding", title: "极端费率", description: "资金费率异常。", count: 0, items: [] },
        { key: "risk", title: "结构与公告风险", description: "结构或公告风险。", count: 0, items: [] }
      ]
    } } });
    if (url.pathname === "/public-api/signals/context") return route.fulfill({ json: { ok: true, data: {
      signal, market, evidence: [
        { key: "price", label: "当前价格", metric: market.metrics.price },
        { key: "oi", label: "合约 OI", metric: market.metrics.oi_value }
      ], lifecycle: intelligence.lifecycle,
      rankings: { self: intelligence.self_rank, market_strength: intelligence.market_strength_rank, market_absolute: intelligence.market_absolute_rank },
      resonance: intelligence.resonance, related: { same_symbol: [] },
      actions: { symbol_url: "/radar?symbol=BTCUSDT", ai_url: "https://t.me/example_bot?start=analyze_BTC", alert_url: "https://t.me/example_bot?start=alert_BTC" }
    } } });
    if (url.pathname === "/public-api/coin/context") return route.fulfill({ json: { ok: true, data: { symbol: "BTCUSDT", coin: "BTC", market, summary: { signal_count: 1, sent_count: 1, module_counts: { launch: 1 } }, timeline: [{ ...signal, intelligence }], actions: { radar_url: "/radar?symbol=BTCUSDT" } } } });
    if (url.pathname === "/public-api/market/watchlist") return route.fulfill({ json: { ok: true, data: { items: [{ symbol: "BTCUSDT", ok: true, market, coin_url: "/coin/BTCUSDT" }], count: 1, invalid: [] } } });
    return route.fulfill({ status: 404, json: { ok: false, message: "not mocked" } });
  });
}

test("desktop radar supports opportunity-to-evidence workflow", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/radar");

  await expect(page.getByRole("heading", { name: "机会看板" })).toBeVisible();
  await expect(page.getByText("启动候选", { exact: true })).toBeVisible();
  await expect(page.getByText("P96 · #2/40")).toBeVisible();
  await page.getByRole("button", { name: /查看证据与上下文/ }).click();
  await expect(page.getByRole("dialog", { name: "信号上下文详情" })).toBeVisible();
  await expect(page.getByText("相对排名与生命周期")).toBeVisible();
  await expect(page.getByText("状态依据：规则分数较上次提高 8.0")).toBeVisible();
});

test("360px radar keeps filters, cards and full-width detail usable", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 780 });
  await mockPublicApi(page);
  await page.goto("/radar");

  await expect(page.getByPlaceholder("BTC 或 BTCUSDT")).toBeVisible();
  await page.getByRole("button", { name: /查看证据与上下文/ }).click();
  const dialog = page.getByRole("dialog", { name: "信号上下文详情" });
  await expect(dialog).toBeVisible();
  const box = await dialog.boundingBox();
  expect(box?.width).toBeGreaterThanOrEqual(350);
  await expect(page.getByRole("button", { name: "关闭信号详情" })).toBeVisible();
});

test("768px radar keeps the compact drawer and primary actions usable", async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 900 });
  await mockPublicApi(page);
  await page.goto("/radar");

  await expect(page.getByPlaceholder("BTC 或 BTCUSDT")).toBeVisible();
  await page.getByRole("button", { name: /查看证据与上下文/ }).click();
  const dialog = page.getByRole("dialog", { name: "信号上下文详情" });
  await expect(dialog).toBeVisible();
  const box = await dialog.boundingBox();
  expect(box?.width).toBeGreaterThanOrEqual(520);
  expect(box?.width).toBeLessThanOrEqual(540);
  await expect(page.getByRole("link", { name: "单币上下文" })).toBeVisible();
  await expect(page.getByRole("button", { name: "自选", exact: true })).toBeVisible();
});

test("coin context and browser-local watchlist form a reusable loop", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/coin/BTCUSDT");
  await expect(page.getByRole("heading", { name: "BTC 单币上下文" })).toBeVisible();
  await page.getByRole("button", { name: /加入自选/ }).click();
  await page.goto("/watchlist");
  await expect(page.getByText("BTCUSDT", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "查看上下文" })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("paoxx.public.watchlist.v1"))).toContain("BTCUSDT");
});
