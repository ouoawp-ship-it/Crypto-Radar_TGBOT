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

const newSignal = {
  ...signal,
  id: 8,
  public_ref: "sig_e2e_eth",
  symbol: "ETHUSDT",
  coin: "ETH",
  excerpt: "新增 ETH 资金异动。",
  display: { ...signal.display, title: "ETH 资金异动", summary: "新增 ETH 资金异动。" }
};

const marketOverview = {
  schema_version: "2026-07-17",
  generated_at: "2026-07-17T12:00:00Z",
  window_sec: 3600,
  data_status: "ready",
  warnings: [],
  coverage: { assets: 80, price: 80, oi: 24, spot_flow: 12, futures_flow: 12, funding: 80 },
  overview: {
    bias: "inflow",
    advancing: 48,
    declining: 32,
    flat: 0,
    breadth_pct: 20,
    total_quote_volume: 8_000_000_000,
    spot_net_flow_usd: 12_000_000,
    futures_net_flow_usd: 18_000_000,
    oi_net_change_usd: 800_000
  }
};

const radarBoards = {
  schema_version: "2026-07-17",
  generated_at: "2026-07-17T12:00:00Z",
  window_sec: 3600,
  data_status: "ready",
  warnings: [],
  coverage: marketOverview.coverage,
  methodology: { flow: "Binance K 线主动买卖成交差（CVD）估算" },
  boards: [
    {
      key: "price", title: "价格动量", available: true, coverage: 80,
      positive: { title: "涨幅榜", items: [{ symbol: "BTCUSDT", coin: "BTC", value: 3.2, unit: "percent", strength_percentile: 96 }] },
      negative: { title: "跌幅榜", items: [{ symbol: "ETHUSDT", coin: "ETH", value: -2.1, unit: "percent", strength_percentile: 88 }] }
    },
    {
      key: "oi", title: "持仓变化", available: true, coverage: 24,
      amount_metric: "oi_change_usd", amount_unit: "usd",
      positive: { title: "OI 增长", items: [{ symbol: "BTCUSDT", coin: "BTC", value: 4.5, unit: "percent", strength_percentile: 94 }] },
      negative: { title: "OI 下降", items: [{ symbol: "SOLUSDT", coin: "SOL", value: -3.1, unit: "percent", strength_percentile: 90 }] },
      amount_positive: { title: "OI 增长", items: [
        { symbol: "BTCUSDT", coin: "BTC", value: 4.5, unit: "percent", magnitude_usd: 1_000_000, strength_percentile: 94 },
        { symbol: "ETHUSDT", coin: "ETH", value: 20, unit: "percent", magnitude_usd: 200_000, strength_percentile: 99 }
      ] },
      amount_negative: { title: "OI 下降", items: [
        { symbol: "SOLUSDT", coin: "SOL", value: -3.1, unit: "percent", magnitude_usd: 350_000, strength_percentile: 90 }
      ] },
      strength_positive: { title: "OI 增长", items: [
        { symbol: "ETHUSDT", coin: "ETH", value: 20, unit: "percent", magnitude_usd: 200_000, strength_percentile: 99 },
        { symbol: "BTCUSDT", coin: "BTC", value: 4.5, unit: "percent", magnitude_usd: 1_000_000, strength_percentile: 94 }
      ] },
      strength_negative: { title: "OI 下降", items: [
        { symbol: "SOLUSDT", coin: "SOL", value: -3.1, unit: "percent", magnitude_usd: 350_000, strength_percentile: 90 }
      ] }
    },
    {
      key: "futures_flow", title: "合约主动资金", available: true, coverage: 12,
      positive: { title: "合约流入", items: [{ symbol: "BTCUSDT", coin: "BTC", value: 8_000_000, unit: "usd", strength_percentile: 98 }] },
      negative: { title: "合约流出", items: [{ symbol: "ETHUSDT", coin: "ETH", value: -5_000_000, unit: "usd", strength_percentile: 92 }] }
    },
    {
      key: "spot_flow", title: "现货主动资金", available: true, coverage: 12,
      positive: { title: "现货流入", items: [{ symbol: "BTCUSDT", coin: "BTC", value: 4_000_000, unit: "usd", strength_percentile: 97 }] },
      negative: { title: "现货流出", items: [{ symbol: "ETHUSDT", coin: "ETH", value: -3_000_000, unit: "usd", strength_percentile: 91 }] }
    },
    {
      key: "realtime_surge", title: "Surge 加速", available: true, coverage: 12,
      positive: { title: "多头加速", items: [{ symbol: "BTCUSDT", coin: "BTC", value: 82, unit: "score", strength_percentile: 98 }] },
      negative: { title: "空头加速", items: [{ symbol: "ETHUSDT", coin: "ETH", value: -76, unit: "score", strength_percentile: 94 }] }
    }
  ]
};

const fundsSectors = {
  schema_version: "2026-07-17",
  catalog_version: "2026.07.1",
  generated_at: "2026-07-17T12:00:00Z",
  window_sec: 3600,
  market_type: "spot",
  data_status: "ready",
  coverage: { assets: 2, flow: 2, gross_flow: 2, oi: 2, market_cap: 2 },
  warnings: [],
  summary: { net_flow_usd: 3_000_000, inflow_usd: 12_000_000, outflow_usd: 9_000_000, asset_count: 2, covered_assets: 2, leading_inflow_sector: "layer1", leading_outflow_sector: "layer2" },
  catalog: [
    { id: "layer1", label: "L1", description: "一层公链" },
    { id: "layer2", label: "L2", description: "二层扩容" }
  ],
  sectors: [
    { sector_id: "layer1", label: "L1", net_flow_usd: 8_000_000, magnitude_usd: 8_000_000, inflow_usd: 10_000_000, outflow_usd: 2_000_000, asset_count: 1, covered_assets: 1, coverage_ratio: 1, data_status: "ready", leaders: [{ symbol: "BTCUSDT", net_flow_usd: 8_000_000 }] },
    { sector_id: "layer2", label: "L2", net_flow_usd: -5_000_000, magnitude_usd: 5_000_000, inflow_usd: 2_000_000, outflow_usd: 7_000_000, asset_count: 1, covered_assets: 1, coverage_ratio: 1, data_status: "ready", leaders: [{ symbol: "ARBUSDT", net_flow_usd: -5_000_000 }] }
  ]
};

const fundsAssets = {
  schema_version: "2026-07-17",
  catalog_version: "2026.07.1",
  generated_at: "2026-07-17T12:00:00Z",
  window_sec: 3600,
  market_type: "spot",
  data_status: "ready",
  coverage: { assets: 2, flow: 2 },
  warnings: [],
  pagination: { page: 1, page_size: 50, page_count: 1, total: 2 },
  items: [
    { symbol: "BTCUSDT", coin: "BTC", price: 65000, price_change_pct: 2.4, net_flow_usd: 8_000_000, inflow_usd: 10_000_000, outflow_usd: 2_000_000, volume_usd: 1_200_000_000, oi_usd: 820_000_000, oi_change_pct: 1.8, funding_pct: -0.02, market_cap: 1_200_000_000_000, updated_at: "2026-07-17T12:00:00Z", data_status: "ready", sector: { primary_sector_id: "layer1", primary_sector_label: "L1", sector_ids: ["layer1"] } },
    { symbol: "ARBUSDT", coin: "ARB", price: 1.1, price_change_pct: -1.2, net_flow_usd: -5_000_000, inflow_usd: 2_000_000, outflow_usd: 7_000_000, volume_usd: 120_000_000, oi_usd: 80_000_000, oi_change_pct: -2.4, funding_pct: 0.01, market_cap: 4_000_000_000, updated_at: "2026-07-17T12:00:00Z", data_status: "ready", sector: { primary_sector_id: "layer2", primary_sector_label: "L2", sector_ids: ["layer2"] } }
  ]
};

const infoFeed = {
  schema_version: "2026-07-17",
  generated_at: "2026-07-17T12:00:00Z",
  data_status: "ready",
  coverage: { events: 1, clusters: 1, high_importance: 1, linked_symbols: 1, rights_verified: 1, sources: 1 },
  warnings: [],
  pagination: { page: 1, page_size: 30, page_count: 1, total: 1 },
  summary: { high_importance: 1, risk: 0, opportunity: 1, official: 1 },
  channels: [
    { key: "official", label: "官方公告", status: "ready", count: 1, rights_status: "official_link_only" },
    { key: "authorized_zh", label: "授权中文资讯", status: "unavailable", count: 0, reason: "尚未配置可验证授权源" },
    { key: "authorized_en", label: "授权英文资讯", status: "unavailable", count: 0, reason: "尚未配置可验证授权源" },
    { key: "sentiment", label: "市场情绪", status: "unavailable", count: 0, reason: "未使用未授权社交数据" }
  ],
  items: [{
    event_id: "binance_abc",
    published_at: "2026-07-17T11:30:00Z",
    collected_at: "2026-07-17T11:31:00Z",
    source: "Binance",
    source_type: "official_announcement",
    title: "Binance Will List Example Token (ABC)",
    url: "https://www.binance.com/en/support/announcement/example",
    symbols: ["ABCUSDT"],
    importance: "high",
    language: "en",
    cluster_id: "cluster_abc",
    cluster_size: 1,
    event_kind: "opportunity",
    rights_status: "official_link_only",
    timestamp_quality: "source",
    data_status: "ready",
    source_links: [{ source: "Binance", url: "https://www.binance.com/en/support/announcement/example", rights_status: "official_link_only" }],
    ai_analysis: {
      status: "ready",
      fact_summary: "Binance Will List Example Token (ABC)",
      possible_impact: "可能提升短期关注度与成交活跃度，不代表价格必然上涨。",
      verification_needed: ["核对官方原文和生效时间", "验证市场是否已经反应"],
      fact_inference_boundary: "fact_summary 来自官方标题；possible_impact 为规则推断。"
    }
  }]
};

const agentEvidence = [
  { ref: "ev_breadth", kind: "market_metric", scope: "global", key: "breadth_pct", label: "上涨广度", value: 25, unit: "percent", source: "market_cockpit", observed_at: "2026-07-17T12:00:00Z", data_status: "ready" },
  { ref: "ev_spot", kind: "market_metric", scope: "global", key: "spot_net_flow_usd", label: "现货主动资金差", value: 20_000_000, unit: "usd", source: "market_cockpit", observed_at: "2026-07-17T12:00:00Z", data_status: "ready" },
  { ref: "ev_signal", kind: "signal_event", scope: "BTCUSDT", key: "sig_e2e_btc", label: "启动雷达", value: "BTC 启动信号", source: "signal_store", observed_at: "2026-07-17T11:58:00Z", data_status: "ready", url: "/radar?symbol=BTCUSDT" },
  { ref: "ev_news", kind: "news_event", scope: "binance_abc", key: "binance_abc", label: "高重要度官方公告", value: "Binance Will List Example Token (ABC)", source: "Binance", observed_at: "2026-07-17T11:30:00Z", data_status: "ready", url: "https://www.binance.com/en/support/announcement/example" }
];

const globalAgent = {
  insight_id: "agent_global", agent_type: "global", scope: "market", label: "全局 Agent",
  generated_at: "2026-07-17T12:00:00Z", expires_at: "2026-07-17T12:03:00Z",
  state: "strengthening", state_label: "同步增强", confidence: 0.78, data_status: "ready",
  summary: "4h 市场广度为 `+25.00%`，现货主动资金差 `+$20.00M`；规则状态为同步增强。",
  evidence_refs: ["ev_breadth", "ev_spot"], counter_evidence_refs: []
};

const agentsOverview = {
  schema_version: "2026-07-17", engine_version: "2026.07.1",
  generated_at: "2026-07-17T12:00:00Z", expires_at: "2026-07-17T12:03:00Z", window_sec: 14400, data_status: "ready",
  coverage: { insights: 5, ready: 5, evidence: 4, signals: 1, news_events: 1 }, warnings: [],
  agents: {
    global: globalAgent,
    majors: [
      { ...globalAgent, insight_id: "agent_btc", agent_type: "major", scope: "BTCUSDT", label: "BTC 解盘 Agent", state_label: "偏强观察", summary: "BTC 4h 价格 `+2.20%`、OI `+3.10%`；规则状态为偏强观察。", actions: { coin_url: "/coin/BTCUSDT", radar_url: "/radar?symbol=BTCUSDT", ai_url: "https://t.me/example_bot?start=analyze_BTC" } },
      { ...globalAgent, insight_id: "agent_eth", agent_type: "major", scope: "ETHUSDT", label: "ETH 解盘 Agent", state: "divergent", state_label: "分歧观察", summary: "ETH 4h 价格与资金出现分歧。", actions: { coin_url: "/coin/ETHUSDT", radar_url: "/radar?symbol=ETHUSDT" } }
    ],
    anomalies: [{ ...globalAgent, insight_id: "agent_anomaly", agent_type: "anomaly", scope: "BTCUSDT", label: "BTC 异常候选", state: "observe", state_label: "偏强观察", summary: "BTC 近 4h 出现 `1` 条已发送信号，需验证资金与 OI。", evidence_refs: ["ev_signal"], actions: { coin_url: "/coin/BTCUSDT", radar_url: "/radar?symbol=BTCUSDT" } }],
    messages: [{ ...globalAgent, insight_id: "agent_message", agent_type: "message", scope: "ABCUSDT", label: "消息 Agent", state: "new_event", state_label: "新增重要事件", summary: "官方公告：Binance Will List Example Token (ABC)。", evidence_refs: ["ev_news"], actions: { info_url: "/info?event=binance_abc", source_url: "https://www.binance.com/en/support/announcement/example" } }]
  },
  evidence: agentEvidence,
  model_info: { provider: "local", model: "rule-engine", version: "2026.07.1", llm_generated: false },
  safety: { rule_first: true, ready_only_for_direction: true, numbers_formatted_by_code: true, evidence_required: true, disclaimer: "市场观察，不构成投资建议。" }
};

const coinChartPoints = Array.from({ length: 48 }, (_, index) => ({
  open_time: new Date(Date.UTC(2026, 6, 16, 0, index * 15)).toISOString(),
  open_time_ms: Date.UTC(2026, 6, 16, 0, index * 15),
  open: 64000 + index * 20,
  high: 64120 + index * 20,
  low: 63920 + index * 20,
  close: 64060 + index * 20,
  quote_volume: 2_000_000 + index * 10_000
}));

const coinSeriesPoints = Array.from({ length: 8 }, (_, index) => ({
  observed_at: 1_000 + index * 300,
  updated_at: new Date(Date.UTC(2026, 6, 16, 0, index * 5)).toISOString(),
  price: 64000 + index * 100,
  oi_usd: 800_000_000 + index * 2_000_000,
  spot_flow_usd: -1_000_000 + index * 400_000,
  futures_flow_usd: -500_000 + index * 300_000,
  funding_pct: -0.02
}));

async function mockPublicApi(page: Page, options: { streamSignal?: boolean; agents?: unknown; assetWarnings?: string[]; healthStatus?: "ok" | "degraded" } = {}) {
  let signalRequests = 0;
  let infoRequests = 0;
  let lastInfoSearch = "";
  let streamRequests = 0;
  let streamDelivered = false;
  let signalsFail = false;
  let agentsFail = false;
  let agentRequests = 0;
  await page.route("**/public-api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/public-api/health") return route.fulfill({ json: { ok: true, data: { status: options.healthStatus || "ok" } } });
    if (url.pathname === "/public-api/telemetry") return route.fulfill({ status: 202, json: { ok: true } });
    if (url.pathname === "/public-api/stream") {
      streamRequests += 1;
      if (options.streamSignal) await new Promise((resolve) => setTimeout(resolve, 2000));
      const event = options.streamSignal ? "event: status\ndata: {\"state\":\"connected\"}\n\nid: 8\nevent: signal\ndata: {\"ref\":\"sig_e2e_eth\",\"symbol\":\"ETHUSDT\"}\n\n" : "event: status\ndata: {\"state\":\"connected\"}\n\n";
      streamDelivered = options.streamSignal || streamDelivered;
      return route.fulfill({ status: 200, contentType: "text/event-stream", headers: { "Cache-Control": "no-cache" }, body: event });
    }
    if (url.pathname === "/public-api/signals") {
      signalRequests += 1;
      if (signalsFail) return route.fulfill({ status: 503, json: { ok: false, message: "信号接口暂时不可用" } });
      const items = options.streamSignal && streamDelivered ? [newSignal, signal] : [signal];
      return route.fulfill({ json: { ok: true, data: { items, count: items.length } } });
    }
    if (url.pathname === "/public-api/signals/stats") return route.fulfill({ json: { ok: true, data: { total: 1, sent: 1, blocked: 0, failed: 0, skipped: 0 } } });
    if (url.pathname === "/public-api/market/overview") return route.fulfill({ json: { ok: true, data: marketOverview } });
    if (url.pathname === "/public-api/radar/boards") return route.fulfill({ json: { ok: true, data: radarBoards } });
    if (url.pathname === "/public-api/funds/sectors") return route.fulfill({ json: { ok: true, data: fundsSectors } });
    if (url.pathname === "/public-api/funds/assets") return route.fulfill({ json: { ok: true, data: { ...fundsAssets, warnings: options.assetWarnings || fundsAssets.warnings } } });
    if (url.pathname === "/public-api/info/feed") {
      infoRequests += 1;
      lastInfoSearch = url.search;
      return route.fulfill({ json: { ok: true, data: infoFeed } });
    }
    if (url.pathname === "/public-api/agents/overview") {
      agentRequests += 1;
      if (agentsFail) return route.fulfill({ status: 503, json: { ok: false, message: "AI 决策暂时不可用" } });
      return route.fulfill({ json: { ok: true, data: options.agents || agentsOverview } });
    }
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
    if (url.pathname === "/public-api/coin/context") return route.fulfill({ json: { ok: true, data: {
      symbol: "BTCUSDT", coin: "BTC", market, data_status: "ready", warnings: [],
      summary: { signal_count: 1, sent_count: 1, module_counts: { launch: 1 } },
      chart: { market_type: "futures", interval: "15m", source: "binance_futures_klines", data_status: "ready", coverage: { requested: 48, returned: 48 }, points: coinChartPoints },
      series: { data_status: "ready", coverage: { points: 8, price: 8, oi: 8, spot_flow: 8, futures_flow: 8, funding: 8 }, points: coinSeriesPoints },
      related_info: { data_status: "empty", items: [] }, evidence_coverage: { market: 1, chart_points: 48, snapshot_points: 8, signals: 1, announcements: 0 },
      timeline: [{ ...signal, intelligence }], actions: { radar_url: "/radar?symbol=BTCUSDT", share_url: "/coin/BTCUSDT" }
    } } });
    if (url.pathname === "/public-api/market/watchlist") return route.fulfill({ json: { ok: true, data: { items: [{ symbol: "BTCUSDT", ok: true, market, coin_url: "/coin/BTCUSDT" }], count: 1, invalid: [] } } });
    return route.fulfill({ status: 404, json: { ok: false, message: "not mocked" } });
  });
  return {
    signalRequests: () => signalRequests,
    infoRequests: () => infoRequests,
    lastInfoSearch: () => lastInfoSearch,
    streamRequests: () => streamRequests,
    agentRequests: () => agentRequests,
    releaseSignal: () => { streamDelivered = true; },
    failSignals: () => { signalsFail = true; },
    failAgents: () => { agentsFail = true; },
  };
}

test("desktop radar supports opportunity-to-evidence workflow", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/radar");

  await expect(page.getByRole("heading", { name: "机会看板" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "热钱观察榜单" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "全场态势" })).toBeVisible();
  await expect(page.getByText("资金偏流入")).toBeVisible();
  await expect(page.getByLabel("涨幅榜量级榜")).toBeVisible();
  await expect(page.getByLabel("涨幅榜强度榜")).toBeVisible();
  await expect(page.getByLabel("主力合约流入榜量级榜")).toBeVisible();
  const oiAmount = page.getByLabel("持仓榜量级榜").first();
  const oiStrength = page.getByLabel("持仓榜强度榜").first();
  await expect(oiAmount.getByRole("button").first()).toContainText("BTC");
  await expect(oiAmount.getByRole("button").first()).toContainText("+$1.0M");
  await expect(page.getByLabel("持仓榜量级榜").last().getByRole("button").first()).toContainText("−$350.0K");
  await expect(oiStrength.getByRole("button").first()).toContainText("ETH");
  await expect(page.getByText("+$800.0K", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "资金合流" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "资金力度" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Surge 加速" })).toBeVisible();
  await expect(page.getByText("+82.0", { exact: true })).toBeVisible();
  await expect(page.getByText("启动候选", { exact: true })).toBeVisible();
  await expect(page.getByText("P96 · #2/40")).toBeVisible();
  await page.getByRole("button", { name: /查看证据与上下文/ }).click();
  await expect(page.getByRole("dialog", { name: "信号上下文详情" })).toBeVisible();
  await expect(page.getByText("相对排名与生命周期")).toBeVisible();
  await expect(page.getByText("状态依据：规则分数较上次提高 8.0")).toBeVisible();
});

test("desktop radar mirrors the target three-column scan hierarchy", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockPublicApi(page);
  await page.goto("/radar");

  const eventBox = await page.getByTestId("radar-event-feed").boundingBox();
  const matrixBox = await page.getByTestId("radar-hot-money").boundingBox();
  const sideBox = await page.getByTestId("radar-side-intelligence").boundingBox();
  expect(eventBox).toMatchObject({ x: 10, y: 66, width: 268, height: 824 });
  expect(matrixBox).toMatchObject({ x: 288, y: 66, width: 864, height: 824 });
  expect(sideBox).toMatchObject({ x: 1162, y: 66, width: 268, height: 824 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1440);
});

test("home dashboard refreshes its own signal data", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.goto("/");

  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.getByText("BTCUSDT", { exact: true }).first()).toBeVisible();
  expect(state.signalRequests()).toBe(1);
  state.failSignals();
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.getByText(/刷新失败，正在继续显示上次成功数据/)).toBeVisible();
  await expect(page.getByText("BTCUSDT", { exact: true }).first()).toBeVisible();
});

test("Paoxx AI reservation page does not call the former agent endpoint", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.goto("/agents");

  await expect(page.getByTestId("paoxx-ai-reserved")).toBeVisible();
  await expect(page.getByRole("heading", { name: "泡泡智选" })).toBeVisible();
  await expect(page.getByText("当前不提供第三方 AI 智选、荐币或自动交易功能。", { exact: false })).toBeVisible();
  expect(state.agentRequests()).toBe(0);
});

test("320px radar keeps filters, cards and full-width detail usable", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 780 });
  await mockPublicApi(page);
  await page.goto("/radar");

  await expect(page.getByPlaceholder("BTC 或 BTCUSDT")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
  for (const control of [
    page.getByPlaceholder("BTC 或 BTCUSDT"),
    page.getByRole("button", { name: "1h" }),
    page.getByRole("button", { name: "暂停" }),
    page.getByRole("button", { name: "刷新雷达" }),
  ]) {
    const controlBox = await control.boundingBox();
    expect(controlBox?.height).toBeGreaterThanOrEqual(44);
  }
  const undersizedControls = await page.locator("a[href], button, input, select, summary").evaluateAll((elements) => elements
    .map((element) => {
      const rect = element.getBoundingClientRect();
      return { label: element.getAttribute("aria-label") || element.textContent?.trim() || element.tagName, width: rect.width, height: rect.height };
    })
    .filter((item) => item.label !== "Open Next.js Dev Tools" && item.width > 0 && item.height > 0 && (item.width < 44 || item.height < 44)));
  expect(undersizedControls).toEqual([]);
  await page.getByRole("button", { name: /查看证据与上下文/ }).click();
  const dialog = page.getByRole("dialog", { name: "信号上下文详情" });
  await expect(dialog).toBeVisible();
  const box = await dialog.boundingBox();
  expect(box?.width).toBeGreaterThanOrEqual(310);
  await expect(page.getByRole("button", { name: "关闭信号详情" })).toBeVisible();
});

test("public cockpit uses the fixed Mercu-style dark visual system", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/radar");

  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("body")).toHaveCSS("background-color", "rgb(7, 9, 13)");
  await page.locator("summary").filter({ hasText: "高级筛选" }).click();
  await expect(page.getByRole("button", { name: "应用" })).toHaveCSS("color", "rgb(7, 9, 13)");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});

test("header distinguishes degraded data from an offline API", async ({ page }) => {
  await mockPublicApi(page, { healthStatus: "degraded" });
  await page.goto("/radar");

  await expect(page.getByText("DEGRADED", { exact: true })).toBeVisible();
  await expect(page.getByLabel("公开 API 可用，部分数据正在积累或降级")).toBeVisible();
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
  await expect(page.getByRole("heading", { name: "K 线与成交量" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "快照证据曲线" })).toBeVisible();
  await page.getByRole("button", { name: /加入自选/ }).click();
  await page.goto("/watchlist");
  await expect(page.getByText("BTCUSDT", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "查看上下文" })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("paoxx.public.watchlist.v1"))).toContain("BTCUSDT");
});

test("funds center links sector rotation to asset evidence", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/funds");

  await expect(page.getByRole("heading", { name: "资金中心" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "板块资金" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "资产资金表" })).toBeVisible();
  await expect(page.getByText("BTCUSDT", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("L1", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: "证据" }).first()).toHaveAttribute("href", "/coin/BTCUSDT");
});

test("funds center surfaces asset-only degradation warnings", async ({ page }) => {
  await mockPublicApi(page, { assetWarnings: ["资产资金数据已降级"] });
  await page.goto("/funds");

  await expect(page.getByText("资产资金数据已降级")).toBeVisible();
});

test("390px funds center uses cards without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockPublicApi(page);
  await page.goto("/funds");

  await expect(page.getByLabel("搜索资产")).toBeVisible();
  await expect(page.getByRole("link", { name: "证据" }).first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test("information center keeps official facts, inferences and source rights traceable", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/info");

  await expect(page.getByRole("heading", { name: "信息中心" })).toBeVisible();
  await expect(page.getByText("授权中文资讯", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Binance Will List Example Token (ABC)" })).toBeVisible();
  await page.getByText("展开规则化解读与验证项").click();
  await expect(page.getByText("可能影响 · 规则推断")).toBeVisible();
  await expect(page.getByRole("link", { name: "Binance 原文 ↗" })).toHaveAttribute("rel", "noopener noreferrer");
});

test("information center applies text filters once instead of requesting on every keystroke", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.goto("/info");

  await expect(page.getByRole("heading", { name: "Binance Will List Example Token (ABC)" })).toBeVisible();
  const requestsBeforeTyping = state.infoRequests();
  await page.getByLabel("币种筛选").fill("BTC");
  await page.getByLabel("搜索资讯").fill("listing");
  await page.waitForTimeout(200);
  expect(state.infoRequests()).toBe(requestsBeforeTyping);

  await page.getByRole("button", { name: "应用" }).click();
  await expect.poll(state.infoRequests).toBe(requestsBeforeTyping + 1);
  const search = new URLSearchParams(state.lastInfoSearch());
  expect(search.get("symbol")).toBe("BTC");
  expect(search.get("q")).toBe("listing");
});

test("390px information center has no horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockPublicApi(page);
  await page.goto("/info");

  await expect(page.getByLabel("搜索资讯")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Binance Will List Example Token (ABC)" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test("Paoxx AI page remains an explicit self-owned reservation", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.goto("/agents");

  await expect(page.getByRole("heading", { name: "泡泡智选" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "先把证据做对，再让模型开口。" })).toBeVisible();
  await expect(page.getByText("PAOXX NATIVE")).toBeVisible();
  await expect(page.getByText("公开版本")).toBeVisible();
  await expect(page.getByText("未开放")).toBeVisible();
  await expect(page.getByText("全局 Agent")).toHaveCount(0);
  expect(state.agentRequests()).toBe(0);
});

test("390px Paoxx AI reservation stays usable without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockPublicApi(page);
  await page.goto("/agents");

  await expect(page.getByRole("heading", { name: "先把证据做对，再让模型开口。" })).toBeVisible();
  await expect(page.getByRole("link", { name: "先看实时雷达" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test("radar SSE surfaces a new event, reconnects and can be paused", async ({ page }) => {
  await page.addInitScript(() => {
    type Listener = (event: MessageEvent) => void;
    class FakeEventSource {
      static current: FakeEventSource | null = null;
      onopen: (() => void) | null = null;
      onerror: (() => void) | null = null;
      listeners = new Map<string, Listener[]>();
      constructor() {
        FakeEventSource.current = this;
        setTimeout(() => this.onopen?.(), 10);
      }
      addEventListener(type: string, listener: Listener) {
        this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
      }
      close() { if (FakeEventSource.current === this) FakeEventSource.current = null; }
      emit(type: string, data: string) { for (const listener of this.listeners.get(type) || []) listener(new MessageEvent(type, { data })); }
    }
    Object.defineProperty(window, "EventSource", { configurable: true, value: FakeEventSource });
    (window as unknown as { __emitSseSignal: () => number }).__emitSseSignal = () => {
      const current = FakeEventSource.current;
      if (!current) return -1;
      const count = current.listeners.get("signal")?.length || 0;
      current.emit("signal", "{\"ref\":\"sig_e2e_eth\"}");
      return count;
    };
    (window as unknown as { __dropSse: () => void }).__dropSse = () => FakeEventSource.current?.onerror?.();
    (window as unknown as { __restoreSse: () => void }).__restoreSse = () => FakeEventSource.current?.onopen?.();
  });
  const state = await mockPublicApi(page, { streamSignal: true });
  await page.goto("/radar");

  await expect(page.getByText("BTCUSDT", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("LIVE", { exact: true }).last()).toBeVisible();
  expect(await page.evaluate(() => document.visibilityState)).toBe("visible");
  state.releaseSignal();
  const requestsBeforeBurst = state.signalRequests();
  const listenerCount = await page.evaluate(() => {
    const emit = (window as unknown as { __emitSseSignal: () => number }).__emitSseSignal;
    const count = emit();
    emit();
    emit();
    return count;
  });
  expect(listenerCount).toBeGreaterThan(0);
  await expect.poll(state.signalRequests).toBe(requestsBeforeBurst + 1);
  await page.waitForTimeout(1000);
  expect(state.signalRequests()).toBe(requestsBeforeBurst + 1);
  const incoming = page.getByRole("button", { name: "新增 1 条异动，点击更新" });
  await expect(incoming).toBeVisible({ timeout: 12_000 });
  await incoming.click();
  await expect(page.getByRole("button", { name: /ETHUSDT 查看证据与上下文/ })).toBeVisible();
  await page.evaluate(() => (window as unknown as { __dropSse: () => void }).__dropSse());
  await expect(page.getByText("RECONNECTING", { exact: true })).toBeVisible();
  await page.evaluate(() => (window as unknown as { __restoreSse: () => void }).__restoreSse());
  await expect(page.getByText("LIVE", { exact: true }).last()).toBeVisible();
  await page.getByRole("button", { name: "暂停" }).click();
  await expect(page.getByText("PAUSED", { exact: true })).toBeVisible();
});

test("reserved AI surface never exposes copied directional conclusions", async ({ page }) => {
  const state = await mockPublicApi(page, { agents: agentsOverview });
  await page.goto("/agents");

  await expect(page.getByText("当前页面不请求 AI 决策接口", { exact: false })).toBeVisible();
  await expect(page.getByText("同步增强", { exact: true })).toHaveCount(0);
  expect(state.agentRequests()).toBe(0);
});
