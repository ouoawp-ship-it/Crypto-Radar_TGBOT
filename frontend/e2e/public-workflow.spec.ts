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

const radarFixtureCoins = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "SUI", "LINK", "AVAX", "ARB", "OP", "AAVE", "UNI", "LTC", "HYPE", "BNB"];

function radarFixtureItems(direction: "positive" | "negative", offset: number, unit: "percent" | "usd", magnitudeBase = 0) {
  const sign = direction === "positive" ? 1 : -1;
  return Array.from({ length: 8 }, (_, index) => {
    const coin = radarFixtureCoins[(index + offset) % radarFixtureCoins.length];
    const absoluteValue = unit === "usd" ? 14_000_000 - index * 1_150_000 : 5.8 - index * 0.47;
    return {
      symbol: `${coin}USDT`,
      coin,
      value: sign * absoluteValue,
      unit,
      magnitude_usd: magnitudeBase ? Math.max(250_000, magnitudeBase - index * magnitudeBase * 0.11) : undefined,
      strength_percentile: 100 - index - offset % 3,
    };
  });
}

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
      positive: { title: "涨幅榜", items: radarFixtureItems("positive", 0, "percent") },
      negative: { title: "跌幅榜", items: radarFixtureItems("negative", 8, "percent") }
    },
    {
      key: "oi", title: "持仓变化", available: true, coverage: 24,
      amount_metric: "oi_change_usd", amount_unit: "usd",
      positive: { title: "OI 增长", items: radarFixtureItems("positive", 1, "percent", 20_000_000) },
      negative: { title: "OI 下降", items: radarFixtureItems("negative", 9, "percent", 5_000_000) }
    },
    {
      key: "futures_flow", title: "合约主动资金", available: true, coverage: 12,
      positive: { title: "合约流入", items: radarFixtureItems("positive", 2, "usd") },
      negative: { title: "合约流出", items: radarFixtureItems("negative", 10, "usd") }
    },
    {
      key: "spot_flow", title: "现货主动资金", available: true, coverage: 12,
      positive: { title: "现货流入", items: radarFixtureItems("positive", 3, "usd") },
      negative: { title: "现货流出", items: radarFixtureItems("negative", 11, "usd") }
    },
    {
      key: "realtime_surge", title: "Surge 加速", available: true, coverage: 12,
      positive: { title: "多头加速", items: [{ symbol: "BTCUSDT", coin: "BTC", value: 82, unit: "score", strength_percentile: 98 }] },
      negative: { title: "空头加速", items: [{ symbol: "ETHUSDT", coin: "ETH", value: -76, unit: "score", strength_percentile: 94 }] }
    }
  ]
};

const realtimeItems = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "SUI", "LINK", "AVAX", "ARB", "OP", "AAVE", "UNI", "LTC"].map((coin, index) => {
  const direction = index % 2 === 0 ? "long" : "short";
  const signed = direction === "long" ? 1 : -1;
  return {
    symbol: `${coin}USDT`, coin, observed_at: "2026-07-18T08:30:00Z", data_status: "ready",
    windows: {
      "5m": { available: true, coverage_ratio: 1, gross_trade_usd: 90_000_000 - index * 2_000_000, cvd_usd: signed * (12_000_000 - index * 300_000), cvd_ratio_pct: signed * (13 - index * 0.3), price_change_pct: signed * (1.2 - index * 0.03), long_liquidation_usd: direction === "short" ? 2_000_000 + index * 10_000 : 150_000, short_liquidation_usd: direction === "long" ? 2_500_000 + index * 10_000 : 120_000 },
      "15m": { available: true, coverage_ratio: 1, cvd_ratio_pct: signed * 8, price_change_pct: signed * 1.4 },
      "30m": { available: true, coverage_ratio: 1, cvd_ratio_pct: signed * 7, price_change_pct: signed * 1.1 },
      "1h": { available: true, coverage_ratio: 1, cvd_ratio_pct: signed * 6, price_change_pct: signed * 1.8 },
      "4h": { available: true, coverage_ratio: 1, cvd_ratio_pct: signed * 5, price_change_pct: signed * 2.1 },
      "1d": { available: true, coverage_ratio: 1, cvd_ratio_pct: signed * 4, price_change_pct: signed * 3.2 }
    },
    surge: { available: true, triggered: index < 5, direction, score: 92 - index * 3, flow_acceleration_pp: signed * (18 - index), volume_acceleration_pct: 42 - index },
    ambush: { available: true, triggered: index >= 5 && index < 9, direction, score: 78 - index, price_compression_pct: 0.6 + index * 0.04 },
    anomaly_24h: { count: 24 - index, long_count: direction === "long" ? 16 - Math.floor(index / 2) : 6, short_count: direction === "short" ? 15 - Math.floor(index / 2) : 5, latest_at: "2026-07-18T08:30:00Z" },
    resonance: { available: true, direction, active_count: 4, window_count: 5, windows: ["15m", "30m", "1h", "4h", "1d"].map((key, windowIndex) => ({ key, active: windowIndex < 4, direction, coverage_ratio: 1 })) },
    lifecycle: { state: index < 2 ? "enhancing" : "continuing", label: index < 2 ? "增强" : "持续", basis: "封闭窗口规则状态" }
  };
});

const anomalyEvents = realtimeItems.flatMap((item, index) => {
  const positive = item.surge.direction === "long";
  const rankings = {
    self: { available: true, rank: (index % 9) + 1, sample_size: 288, percentile: 99 - index, method: "近 24h 同币历史窗口" },
    market_strength: { available: true, rank: index + 1, sample_size: realtimeItems.length, percentile: 98 - index, method: "全场历史极端分位" },
    market_absolute: { available: true, rank: realtimeItems.length - index, sample_size: realtimeItems.length, percentile: 80 + index, method: "全场绝对金额" }
  };
  return [
    { id: `${item.symbol}:price`, symbol: item.symbol, coin: item.coin, observed_at: item.observed_at, window: "5m", event_type: positive ? "price_up" : "price_down", label: positive ? "价格暴涨" : "价格暴跌", metric: "price", direction: item.surge.direction, value: item.windows["5m"].price_change_pct, change_pct: item.windows["5m"].price_change_pct, rankings },
    { id: `${item.symbol}:flow`, symbol: item.symbol, coin: item.coin, observed_at: item.observed_at, window: "15m", event_type: positive ? "perp_inflow" : "perp_outflow", label: positive ? "合约净流入" : "合约净流出", metric: "perp_flow", direction: item.surge.direction, value: item.windows["5m"].cvd_usd, value_usd: item.windows["5m"].cvd_usd, change_pct: item.windows["5m"].cvd_ratio_pct, rankings }
  ];
});

const realtimeIntelligence = {
  schema_version: "2026-07-18.1", generated_at: "2026-07-18T08:30:00Z", observed_at: "2026-07-18T08:30:00Z", data_status: "ready",
  coverage: { symbols: realtimeItems.length, surge: 5, ambush: 4, total: realtimeItems.length, anomaly_events: anomalyEvents.length }, items: realtimeItems, anomaly_events: anomalyEvents,
  boards: []
};

const crossExchangeOi = {
  schema_version: "workstation.funds.open-interest.v1", symbol: "BTCUSDT", data_status: "ready",
  coverage: { exchanges: 3, target: 3 }, mark_price: 65000, total_oi_usd: 1_450_000_000, top_exchange_share_pct: 48.2759,
  exchanges: [
    { exchange: "binance", oi_usd: 700_000_000, share_pct: 48.2759, status: "ready" },
    { exchange: "bybit", oi_usd: 450_000_000, share_pct: 31.0345, status: "ready" },
    { exchange: "okx", oi_usd: 300_000_000, share_pct: 20.6897, status: "ready" }
  ]
};

const fundsSectorFixtures = [
  ["layer1", "L1", 1_474_700], ["privacy", "隐私", 328_500], ["exchange", "平台币", 302_800],
  ["layer2", "L2", 294_700], ["gaming", "GameFi", 214_500], ["depin", "DePIN", 122_000],
  ["ai", "AI", 110_400], ["metals", "贵金属", 99_600], ["modular", "模块化", 88_200],
  ["rwa", "RWA", -498_500], ["meme", "Meme", -193_300], ["data", "数据", -147_000],
  ["desci", "DeSci", -128_500], ["btc", "BTC生态", -116_200], ["social", "社交", -98_600],
  ["nft", "NFT", -86_100], ["defi", "DeFi", -72_300], ["payments", "支付", -48_600]
] as const;

const fundsSectors = {
  schema_version: "2026-07-18",
  catalog_version: "2026.07.1",
  generated_at: "2026-07-17T12:00:00Z",
  window_sec: 3600,
  market_type: "spot",
  data_status: "ready",
  coverage: { assets: 698, flow: 698, gross_flow: 698, oi: 412, market_cap: 634 },
  warnings: [],
  summary: { net_flow_usd: 2_755_900, inflow_usd: 3_429_800, outflow_usd: 673_900, asset_count: 698, covered_assets: 698, leading_inflow_sector: "L1", leading_outflow_sector: "RWA" },
  catalog: fundsSectorFixtures.map(([id, label]) => ({ id, label, description: `${label} 板块` })),
  sectors: fundsSectorFixtures.map(([sector_id, label, net_flow_usd], index) => ({ sector_id, label, net_flow_usd, magnitude_usd: Math.abs(net_flow_usd), inflow_usd: net_flow_usd > 0 ? Math.abs(net_flow_usd) + 150_000 : 80_000 + index * 5_000, outflow_usd: net_flow_usd < 0 ? Math.abs(net_flow_usd) + 80_000 : 150_000, asset_count: 8 + index, covered_assets: 8 + index, coverage_ratio: 1, data_status: "ready", leaders: [{ symbol: "BTCUSDT", net_flow_usd }] }))
};

const fundsCoins = ["BTC", "SOL", "DATA", "XRP", "ETH", "ZEC", "ADA", "AAVE", "XAUT", "LINK", "MANTRA", "INJ", "HYPE", "ALLO", "ENA", "UAI", "BONK", "POL", "EVAA", "XPL"];
const fundsNetFixtures = [4_720_900, 791_500, 562_900, 426_300, 424_100, 134_200, 112_300, 105_200, 85_100, 67_500, 60_300, 46_900, 45_100, 38_900, 34_700, 31_500, 28_600, 24_200, 21_800, 19_500];
const fundsAssetFixtures = Array.from({ length: 698 }, (_, index) => {
  const coin = fundsCoins[index] || `T${String(index + 1).padStart(3, "0")}`;
  const net = fundsNetFixtures[index] ?? Math.max(100, (22 - Math.min(index, 21)) * 10_000 + (index % 3) * 700);
  const inflow = net * (2.1 + (index % 20) * 0.03);
  const outflow = inflow - net;
  return { symbol: `${coin}USDT`, coin, price: index === 0 ? 0.4356 : 0.08 + index * 0.127, price_change_pct: index % 3 === 0 ? 8.17 - index * 0.01 : -0.99 - index * 0.02, net_flow_usd: net, net_flow_change_pct: index % 4 === 0 ? null : -72 + (index % 20) * 3.7, inflow_usd: inflow, outflow_usd: outflow, volume_usd: Math.max(10_000, 745_000 - index * 800), volume_change_pct: index % 2 ? -14.03 - index * 0.01 : 38.19 - index * 0.01, oi_usd: Math.max(100_000, 820_000_000 - index * 1_000_000), oi_change_pct: 1.8 - index * 0.01, funding_pct: -0.02 + index * 0.0001, market_cap: Math.max(500_000, 102_000_000 - index * 100_000), updated_at: "2026-07-18T17:44:00Z", data_status: "ready", sector: { primary_sector_id: fundsSectorFixtures[index % fundsSectorFixtures.length][0], primary_sector_label: fundsSectorFixtures[index % fundsSectorFixtures.length][1], sector_ids: [fundsSectorFixtures[index % fundsSectorFixtures.length][0]] } };
});
const fundsAssets = {
  schema_version: "2026-07-18",
  catalog_version: "2026.07.1",
  generated_at: "2026-07-17T12:00:00Z",
  window_sec: 3600,
  market_type: "spot",
  data_status: "ready",
  coverage: { assets: 698, flow: 698 },
  warnings: [],
  distribution: { oi_total_usd: 900_000_000, oi_covered_assets: 62, top_10_oi_share_pct: 74.5, top_50_oi_share_pct: 98.2 },
  pagination: { page: 1, page_size: 20, page_count: 35, total: 698 },
  items: fundsAssetFixtures.slice(0, 20)
};

const extraInfoFixtures = [
  ...Array.from({ length: 9 }, (_, index) => ({
    event_id: `zh_news_${index}`,
    published_at: `2030-07-18T${String(19 - Math.floor(index / 3)).padStart(2, "0")}:${String(58 - index * 4).padStart(2, "0")}:00Z`,
    collected_at: "2030-07-18T20:31:00Z",
    source: index % 2 ? "深潮 TechFlow" : "PANews",
    source_type: "news",
    title: ["市场消息：机构资金持续关注主流资产", "链上数据出现新的大额资金迁移", "交易平台公布最新市场风险提示"][index % 3],
    summary: undefined,
    url: `https://example.com/zh/${index}`,
    symbols: [["BTCUSDT"], ["ETHUSDT"], ["SOLUSDT"]][index % 3],
    importance: index % 4 === 0 ? "high" : "medium",
    language: "zh",
    cluster_id: `cluster_zh_${index}`,
    cluster_size: 1,
    event_kind: index % 5 === 0 ? "risk" : "opportunity",
    rights_status: "public_rss_link",
    timestamp_quality: "source",
    data_status: "ready",
    source_links: [],
  })),
  ...Array.from({ length: 9 }, (_, index) => ({
    event_id: `en_news_${index}`,
    published_at: `2030-07-18T19:${String(57 - index * 4).padStart(2, "0")}:00Z`,
    collected_at: "2030-07-18T20:31:00Z",
    source: index % 2 ? "marketfeed" : "Decrypt",
    source_type: "news",
    title: ["Market liquidity improves across major crypto assets", "Institutional flows return as volatility expands", "Traders monitor a fresh derivatives positioning shift"][index % 3],
    summary: undefined,
    url: `https://example.com/en/${index}`,
    symbols: [["BTCUSDT"], ["ETHUSDT"], ["XRPUSDT"]][index % 3],
    importance: index % 4 === 0 ? "high" : "medium",
    language: "en",
    cluster_id: `cluster_en_${index}`,
    cluster_size: 1,
    event_kind: index % 5 === 0 ? "risk" : "opportunity",
    rights_status: "public_rss_link",
    timestamp_quality: "source",
    data_status: "ready",
    source_links: [],
  })),
  ...Array.from({ length: 9 }, (_, index) => ({
    event_id: `kol_${index}`,
    published_at: `2030-07-18T19:${String(56 - index * 5).padStart(2, "0")}:00Z`,
    collected_at: "2030-07-18T20:31:00Z",
    source: index % 2 ? "@marketobserver.bsky.social" : "@analyst.bsky.social",
    source_type: "kol",
    title: ["主流币资金结构正在重新平衡，等待现货确认。", "短线波动扩大，但趋势仍需要成交量验证。", "衍生品仓位变化值得继续跟踪。"][index % 3],
    summary: undefined,
    url: `https://bsky.app/profile/example/post/${index}`,
    symbols: [["SOLUSDT"], ["BTCUSDT"], ["ONDOUSDT"]][index % 3],
    importance: index % 3 === 0 ? "high" : "medium",
    language: "zh",
    cluster_id: `cluster_kol_${index}`,
    cluster_size: 1,
    event_kind: index % 4 === 0 ? "risk" : "opportunity",
    rights_status: "public_social_link",
    timestamp_quality: "source",
    data_status: "ready",
    source_links: [],
    ai_analysis: { status: "not_generated", engagement: { likes: 160 - index * 7, reposts: 32 - index, replies: 18 - index, score: 260 - index * 12 } },
  })),
  ...Array.from({ length: 14 }, (_, index) => {
    const coins = ["BANK", "SPX", "SAKE", "BTC", "SNDK", "ETH", "CL", "XAU"];
    const coin = coins[index % coins.length];
    return {
      event_id: `plaza_${index}`,
      published_at: `2030-07-18T20:${String(29 - index).padStart(2, "0")}:00Z`,
      collected_at: "2030-07-18T20:31:00Z",
      source: "@market.bsky.social",
      source_type: "plaza",
      title: `$${coin} 公开讨论热度上升，资金与价格结构出现新的共振信号。`,
      summary: `$${coin} 公开讨论热度、方向和互动强度的聚合摘要。`,
      url: `https://bsky.app/profile/market.bsky.social/post/${index}`,
      symbols: [`${coin}USDT`],
      importance: index < 3 ? "high" : "medium",
      language: "zh",
      cluster_id: `cluster_plaza_${index}`,
      cluster_size: 1,
      event_kind: index % 4 === 1 ? "risk" : "opportunity",
      rights_status: "public_social_link",
      timestamp_quality: "source",
      data_status: "ready",
      source_links: [],
      ai_analysis: { status: "not_generated", engagement: { likes: 180 - index * 5, reposts: 30 - index, replies: 16 - Math.floor(index / 2), score: 300 - index * 13 } },
    };
  }),
];

const infoFeed = {
  schema_version: "2026-07-18",
  generated_at: "2030-07-18T20:35:00Z",
  data_status: "ready",
  coverage: { events: 45, clusters: 45, high_importance: 12, linked_symbols: 45, rights_verified: 45, sources: 8 },
  warnings: [],
  pagination: { page: 1, page_size: 80, page_count: 1, total: 45 },
  summary: { high_importance: 2, risk: 1, opportunity: 2, official: 0 },
  channels: [
    { key: "news_zh", label: "聚合资讯", status: "ready", count: 1, rights_status: "public_rss_link" },
    { key: "news_en", label: "英文流资讯", status: "ready", count: 1, rights_status: "public_rss_link" },
    { key: "kol", label: "KOL聚合资讯", status: "ready", count: 1, rights_status: "public_social_link" },
    { key: "plaza", label: "市场广场情绪", status: "ready", count: 1, rights_status: "public_social_link" }
  ],
  items: [
    { event_id: "panews_btc", published_at: "2030-07-18T20:30:00Z", collected_at: "2030-07-18T20:31:00Z", source: "PANews", source_type: "news", title: "比特币现货资金持续流入，市场关注度快速升温", summary: "BTC 交易量与主动买盘同步增强。", url: "https://www.panewslab.com/zh/articles/example", symbols: ["BTCUSDT"], importance: "high", language: "zh", cluster_id: "cluster_btc_zh", cluster_size: 1, event_kind: "opportunity", rights_status: "public_rss_link", timestamp_quality: "source", data_status: "ready", source_links: [] },
    { event_id: "decrypt_eth", published_at: "2030-07-18T20:25:00Z", collected_at: "2030-07-18T20:31:00Z", source: "Decrypt", source_type: "news", title: "Ethereum trading activity accelerates as ETF demand returns", summary: "Spot volume and institutional demand rose together.", url: "https://decrypt.co/example", symbols: ["ETHUSDT"], importance: "medium", language: "en", cluster_id: "cluster_eth_en", cluster_size: 1, event_kind: "opportunity", rights_status: "public_rss_link", timestamp_quality: "source", data_status: "ready", source_links: [] },
    { event_id: "bsky_kol_sol", published_at: "2030-07-18T20:20:00Z", collected_at: "2030-07-18T20:31:00Z", source: "@analyst.bsky.social", source_type: "kol", title: "$SOL liquidity is improving, but confirmation still needs spot follow-through.", url: "https://bsky.app/profile/analyst.bsky.social/post/sol", symbols: ["SOLUSDT"], importance: "high", language: "en", cluster_id: "cluster_sol_kol", cluster_size: 1, event_kind: "neutral", rights_status: "public_social_link", timestamp_quality: "source", data_status: "ready", source_links: [], ai_analysis: { status: "not_generated", engagement: { likes: 188, reposts: 28, replies: 14, score: 258 } } },
    { event_id: "bsky_plaza_doge", published_at: "2030-07-18T20:15:00Z", collected_at: "2030-07-18T20:31:00Z", source: "@market.bsky.social", source_type: "plaza", title: "$DOGE breakout discussion is surging across the public feed.", url: "https://bsky.app/profile/market.bsky.social/post/doge", symbols: ["DOGEUSDT"], importance: "medium", language: "en", cluster_id: "cluster_doge_plaza", cluster_size: 1, event_kind: "opportunity", rights_status: "public_social_link", timestamp_quality: "source", data_status: "ready", source_links: [], ai_analysis: { status: "not_generated", engagement: { likes: 96, reposts: 12, replies: 8, score: 128 } } },
    ...extraInfoFixtures,
  ]
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
  await page.route("https://cdn.jsdelivr.net/**", (route) => route.abort("failed"));
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
    if (url.pathname === "/public-api/workstation/radar/momentum-windows") {
      return route.fulfill({
        json: {
          ok: true,
          data: {
            windows: Object.fromEntries(["15m", "30m", "1h", "4h", "1d"].map((window) => [window, { ...radarBoards, window }]))
          }
        }
      });
    }
    if (url.pathname === "/public-api/workstation/radar/momentum") return route.fulfill({ json: { ok: true, data: radarBoards } });
    if (url.pathname === "/public-api/radar/realtime-intelligence") return route.fulfill({ json: { ok: true, data: realtimeIntelligence } });
    if (url.pathname === "/public-api/workstation/funds/open-interest") return route.fulfill({ json: { ok: true, data: { ...crossExchangeOi, symbol: url.searchParams.get("symbol") || "BTCUSDT" } } });
    if (url.pathname === "/public-api/funds/sectors") return route.fulfill({ json: { ok: true, data: fundsSectors } });
    if (url.pathname === "/public-api/funds/assets") {
      const page = Math.max(1, Number(url.searchParams.get("page") || 1));
      const pageSize = Math.max(1, Number(url.searchParams.get("page_size") || 20));
      const search = String(url.searchParams.get("search") || "").toUpperCase();
      const sort = String(url.searchParams.get("sort") || "net_flow_usd") as keyof (typeof fundsAssetFixtures)[number];
      const direction = url.searchParams.get("direction") === "asc" ? 1 : -1;
      const filtered = fundsAssetFixtures.filter((item) => !search || `${item.symbol} ${item.coin}`.includes(search));
      filtered.sort((a, b) => (Number(a[sort] ?? Number.NEGATIVE_INFINITY) - Number(b[sort] ?? Number.NEGATIVE_INFINITY)) * direction);
      const total = filtered.length;
      const items = filtered.slice((page - 1) * pageSize, page * pageSize);
      return route.fulfill({ json: { ok: true, data: { ...fundsAssets, market_type: url.searchParams.get("market_type") || "spot", window_sec: Number(url.searchParams.get("window_sec") || 900), warnings: options.assetWarnings || fundsAssets.warnings, sort: { key: sort, direction: direction === 1 ? "asc" : "desc" }, pagination: { page, page_size: pageSize, page_count: Math.max(1, Math.ceil(total / pageSize)), total }, items } } });
    }
    if (url.pathname === "/public-api/info/feed") {
      infoRequests += 1;
      lastInfoSearch = url.search;
      const sourceType = String(url.searchParams.get("source_type") || "");
      const language = String(url.searchParams.get("language") || "");
      const items = infoFeed.items.filter((item) => (!sourceType || item.source_type === sourceType) && (!language || item.language === language));
      return route.fulfill({ json: { ok: true, data: { ...infoFeed, coverage: { ...infoFeed.coverage, events: items.length }, pagination: { ...infoFeed.pagination, total: items.length }, items } } });
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

test("desktop radar exposes the independent workstation modules", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/radar");

  for (const heading of ["异动监控", "热钱观察榜单", "全场态势", "Surge 飙升榜", "24h 异动总榜", "埋伏池"]) {
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
  }
  await expect(page.getByLabel(/五窗口共振/).first()).toBeVisible();
  await expect(page.getByText("强度榜").first()).toBeVisible();
  await expect(page.getByText(/96%/).first()).toBeVisible();
});

test("desktop radar mirrors the target three-column scan hierarchy", async ({ page }) => {
  await page.setViewportSize({ width: 1152, height: 720 });
  await mockPublicApi(page);
  await page.goto("/radar");

  const eventBox = await page.getByTestId("radar-event-feed").boundingBox();
  const matrixBox = await page.getByTestId("radar-hot-money").boundingBox();
  const sideBox = await page.getByTestId("radar-side-intelligence").boundingBox();
  expect(eventBox?.x).toBeCloseTo(10, 0);
  expect(eventBox?.y).toBeCloseTo(55, 0);
  expect(eventBox?.width).toBeCloseTo(231, 0);
  expect(matrixBox?.width).toBeCloseTo(650, 0);
  expect(sideBox?.width).toBeCloseTo(231, 0);
  expect(eventBox?.height).toBeCloseTo(655, 0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1152);
});

test("925x732 logged-in Mercu reference geometry remains aligned", async ({ page }) => {
  await page.setViewportSize({ width: 925, height: 732 });
  await mockPublicApi(page);
  await page.goto("/radar");
  const event = await page.getByTestId("radar-event-feed").boundingBox();
  const center = await page.getByTestId("radar-hot-money").boundingBox();
  const side = await page.getByTestId("radar-side-intelligence").boundingBox();
  expect(event?.x).toBeCloseTo(6, 0);
  expect(event?.y).toBeCloseTo(51, 0);
  expect(event?.width).toBeCloseTo(200.5, 1);
  expect(center?.width).toBeCloseTo(500, 0);
  expect(side?.width).toBeCloseTo(200.5, 1);
  expect(event?.height).toBeCloseTo(675, 0);
  const momentumBoards = await page.getByTestId("radar-momentum-matrix").locator(":scope > section").evaluateAll((elements) => elements.map((element) => {
    const rect = element.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width };
  }));
  expect(momentumBoards).toHaveLength(4);
  expect(momentumBoards[0].x).toBeCloseTo(219.5, 1);
  expect(momentumBoards[0].y).toBeCloseTo(98, 0);
  expect(momentumBoards[0].width).toBeCloseTo(240, 0);
  await expect(page.getByText("+5.80%", { exact: true }).first()).toBeVisible();
  const strengthColumns = await page.getByTestId("radar-strength-grid").first().evaluate((element) => getComputedStyle(element).gridTemplateColumns.split(" ").length);
  expect(strengthColumns).toBe(2);

  await page.goto("/info");
  const infoColumns = await page.locator('[data-testid="info-four-columns"] > section').evaluateAll((elements) => elements.map((element) => {
    const rect = element.getBoundingClientRect();
    return { width: Math.round(rect.width), top: Math.round(rect.top), bottom: Math.round(rect.bottom) };
  }));
  expect(infoColumns).toHaveLength(4);
  expect(infoColumns.reduce((sum, column) => sum + column.width, 0)).toBeGreaterThanOrEqual(890);
  expect(infoColumns.every((column) => column.top === 101 && column.bottom === 726)).toBe(true);

  await page.goto("/funds");
  const sector = await page.getByRole("heading", { name: "板块资金流" }).locator("xpath=ancestor::section").boundingBox();
  expect(sector?.x).toBeCloseTo(10, 0);
  expect(sector?.y).toBeCloseTo(95, 0);
  expect(sector?.width).toBeCloseTo(225, 0);
  expect(sector?.height).toBeCloseTo(631, 0);
  const assets = await page.getByTestId("funds-assets-overview").boundingBox();
  const assetSearch = await page.getByTestId("funds-assets-overview").locator("input").boundingBox();
  const assetFooter = await page.getByTestId("funds-assets-overview").locator("footer").boundingBox();
  expect(assets?.x).toBeCloseTo(247, 0);
  expect(assets?.width).toBeCloseTo(668, 0);
  expect(assetSearch?.x).toBeGreaterThanOrEqual(531);
  expect(assetSearch?.x).toBeLessThanOrEqual(534);
  expect(assetSearch?.width).toBeCloseTo(255, 0);
  expect(assetFooter?.y).toBeCloseTo(697, 0);
  expect(assetFooter?.height).toBeCloseTo(28, 0);
});

test("funds table sorting and browser-local favorites are functional", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/funds");

  await page.getByLabel("添加ALLO自选").click();
  await expect(page.getByLabel("取消ALLO自选")).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("取消ALLO自选")).toBeVisible();

  const volumeSort = page.getByLabel("按交易量($)排序");
  await volumeSort.click();
  await expect(volumeSort).toHaveText("交易量($)↓");
  await volumeSort.click();
  await expect(volumeSort).toHaveText("交易量($)↑");
  await expect(page.getByTestId("funds-assets-overview").locator('[role="button"]').filter({ hasText: "T698" })).toBeVisible();
});

for (const viewport of [
  { css: { width: 1152, height: 720 }, pixels: { width: 1440, height: 900 } },
  { css: { width: 1536, height: 864 }, pixels: { width: 1920, height: 1080 } },
]) {
  test(`workstation visual fixtures remain stable at ${viewport.pixels.width}x${viewport.pixels.height}`, async ({ page }) => {
    await page.setViewportSize(viewport.css);
    await mockPublicApi(page);
    for (const route of ["radar", "info", "funds"] as const) {
      await page.goto(`/${route}`);
      await expect(page.getByTestId(`${route}-workstation`)).toBeVisible();
      await page.addStyleTag({ content: "nextjs-portal { display: none !important; }" });
      await expect(page).toHaveScreenshot(`${route}-${viewport.pixels.width}x${viewport.pixels.height}.png`, { animations: "disabled", maxDiffPixelRatio: 0.015, scale: "device" });
    }
  });
}

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

test("320px radar keeps its primary workstation controls usable", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 780 });
  await mockPublicApi(page);
  await page.goto("/radar");

  await expect(page.getByPlaceholder("搜索币种...")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
  for (const control of [
    page.getByPlaceholder("搜索币种..."),
    page.getByRole("button", { name: "1h" }),
    page.getByRole("button", { name: "暂停" }),
    page.getByRole("button", { name: "立即更新" }),
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
  await expect(page.getByRole("heading", { name: "24h 异动总榜" })).toBeVisible();
});

test("public cockpit defaults to the Mercu-style light system and persists theme choice", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/radar");

  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expect(page.locator("body")).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await page.getByRole("button", { name: "切换到深色主题" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});

test("header distinguishes degraded data from an offline API", async ({ page }) => {
  await mockPublicApi(page, { healthStatus: "degraded" });
  await page.goto("/radar");

  await expect(page.getByText("DEGRADED", { exact: true })).toBeVisible();
});

test("767px radar keeps stacked workstation modules usable", async ({ page }) => {
  await page.setViewportSize({ width: 767, height: 900 });
  await mockPublicApi(page);
  await page.goto("/radar");

  await expect(page.getByPlaceholder("搜索币种...")).toBeVisible();
  await expect(page.getByRole("heading", { name: "热钱观察榜单" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "全场态势" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(767);
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

test("radar signal deep link opens and closes the exact context drawer", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/radar?signal=sig_e2e_btc");

  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("BTCUSDT", { exact: true })).toBeVisible();
  await dialog.locator("button").first().click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => new URL(page.url()).searchParams.has("signal")).toBe(false);
});

test("funds workstation links overview, time series and cross-exchange OI", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/funds");

  await expect(page.getByRole("heading", { name: "板块资金流" })).toBeVisible();
  await expect(page.getByLabel("搜索全体代币")).toBeVisible();
  await page.getByRole("button", { name: /BTC/ }).first().click();
  await expect(page.getByRole("heading", { name: "BTCUSDT 现货" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "现货资金流" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "跨所持仓对比" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "OI & 资金费率" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "持仓分布 / 集中度" })).toBeVisible();
  await expect(page.getByText("$1.45B", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "合约" }).click();
  await expect(page.getByRole("heading", { name: "BTCUSDT 合约（永续）" })).toBeVisible();
});

test("funds overview uses server-backed pagination and search semantics", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/funds");

  await expect(page.getByText("共 698 个代币 · 每页 20 条 · 第 1/35 页")).toBeVisible();
  const secondPageRequest = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/public-api/funds/assets" && url.searchParams.get("market_type") === "spot" && url.searchParams.get("page") === "2";
  });
  await page.getByRole("button", { name: "下一页" }).click();
  await secondPageRequest;
  await expect(page.getByText("共 698 个代币 · 每页 20 条 · 第 2/35 页")).toBeVisible();
  await expect(page.getByText("21", { exact: true }).first()).toBeVisible();

  const searchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/public-api/funds/assets" && url.searchParams.get("market_type") === "spot" && url.searchParams.get("search") === "BTC";
  });
  await page.getByLabel("搜索全体代币").fill("BTC");
  await searchRequest;
  await expect(page.getByText("共 1 个代币 · 每页 20 条 · 第 1/1 页")).toBeVisible();
});

test("funds workstation preserves explicit cross-venue coverage", async ({ page }) => {
  await mockPublicApi(page, { assetWarnings: ["资产资金数据已降级"] });
  await page.goto("/funds");
  await page.getByRole("button", { name: /BTC/ }).first().click();

  await expect(page.getByText("3/3 场所")).toBeVisible();
  await expect(page.getByText("缺失交易所不按 0 计入分母", { exact: false })).toBeVisible();
});

test("390px funds workstation stacks without page-level horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockPublicApi(page);
  await page.goto("/funds");

  await expect(page.getByLabel("搜索全体代币")).toBeVisible();
  await page.getByRole("button", { name: /BTC/ }).first().click();
  await expect(page.getByRole("heading", { name: "跨所持仓对比" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test("information workstation keeps four fixed authorized streams traceable", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/info");

  await expect(page.getByRole("heading", { name: "AI 信息蒸馏" })).toBeVisible();
  for (const heading of ["聚合资讯", "英文流资讯", "KOL聚合资讯", "市场广场情绪"]) await expect(page.getByRole("heading", { name: heading }).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "比特币现货资金持续流入，市场关注度快速升温" }).first()).toBeVisible();
  await expect(page.getByRole("link").filter({ hasText: "比特币现货资金持续流入" }).first()).toHaveAttribute("rel", "noreferrer");
});

test("information workstation loads each source column independently", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.goto("/info");

  await expect(page.getByRole("heading", { name: "比特币现货资金持续流入，市场关注度快速升温" }).first()).toBeVisible();
  await expect.poll(state.infoRequests).toBe(4);
  await page.getByRole("button", { name: /4h AI 综合分析/ }).click();
  await expect.poll(state.infoRequests).toBe(8);
});

test("390px information workstation stacks its four columns", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockPublicApi(page);
  await page.goto("/info");

  await expect(page.getByRole("heading", { name: "聚合资讯" }).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "比特币现货资金持续流入，市场关注度快速升温" }).first()).toBeVisible();
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

test("radar polling can be paused and manually refreshed", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/radar");

  await expect(page.getByText("15s 增量", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "暂停" }).click();
  await expect(page.getByText("已暂停", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "立即更新" }).click();
  await expect(page.getByRole("heading", { name: "异动总榜" })).toBeVisible();
  await page.getByRole("button", { name: "继续" }).click();
  await expect(page.getByText("15s 增量", { exact: false })).toBeVisible();
});

test("reserved AI surface never exposes copied directional conclusions", async ({ page }) => {
  const state = await mockPublicApi(page, { agents: agentsOverview });
  await page.goto("/agents");

  await expect(page.getByText("当前页面不请求 AI 决策接口", { exact: false })).toBeVisible();
  await expect(page.getByText("同步增强", { exact: true })).toHaveCount(0);
  expect(state.agentRequests()).toBe(0);
});
