import { expect, Page, test } from "@playwright/test";
import { mercuRadarFixture } from "./fixtures/mercu-radar";

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
    oi_net_change_usd: 800_000,
    comparison: {
      previous: {
        advancing: 42,
        declining: 38,
        breadth_pct: 5,
        spot_net_flow_usd: -800_000,
        futures_net_flow_usd: 23_000_000,
        oi_net_change_usd: 1_600_000
      },
      delta: {
        breadth_pct: 15,
        spot_net_flow_usd: 12_800_000,
        futures_net_flow_usd: -5_000_000,
        oi_net_change_usd: -800_000
      }
    }
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
  ["layer1", "L1", 1_474_700], ["privacy", "隐私", 462_400], ["staking", "质押", 325_700], ["exchange", "平台币", 302_800],
  ["gaming", "GameFi", 214_500], ["identity", "身份", 122_400], ["depin", "DePIN", 122_000], ["ai", "AI", 110_400],
  ["metals", "贵金属", 99_600], ["modular", "模块化", 88_200], ["payments", "支付", 48_800], ["layer2", "L2", 44_900], ["cross_chain", "跨链", 13_200],
  ["rwa", "RWA", -221_700], ["oracle", "预言机", -187_300], ["defi", "DeFi", -92_700], ["meme", "Meme", -72_300],
  ["social", "社交", -62_800], ["nft", "NFT", -15_200], ["stocks", "股票", -14_700], ["btc", "BTC生态", -3_826.2],
  ["data", "数据", -1_300], ["desci", "DeSci", -1_273.8]
] as const;

const fundsSectors = {
  schema_version: "2026-07-18",
  catalog_version: "2026.07.1",
  generated_at: "2026-07-18T23:05:00Z",
  window_sec: 3600,
  market_type: "spot",
  data_status: "ready",
  coverage: { assets: 688, flow: 688, gross_flow: 688, oi: 412, market_cap: 634 },
  warnings: [],
  summary: { net_flow_usd: 2_755_900, inflow_usd: 3_429_800, outflow_usd: 673_100, asset_count: 688, covered_assets: 688, leading_inflow_sector: "L1", leading_outflow_sector: "RWA" },
  catalog: fundsSectorFixtures.map(([id, label]) => ({ id, label, description: `${label} 板块` })),
  sectors: fundsSectorFixtures.map(([sector_id, label, net_flow_usd], index) => ({ sector_id, label, net_flow_usd, magnitude_usd: Math.abs(net_flow_usd), inflow_usd: net_flow_usd > 0 ? Math.abs(net_flow_usd) + 150_000 : 80_000 + index * 5_000, outflow_usd: net_flow_usd < 0 ? Math.abs(net_flow_usd) + 80_000 : 150_000, asset_count: 8 + index, covered_assets: 8 + index, coverage_ratio: 1, data_status: "ready", leaders: [{ symbol: "BTCUSDT", net_flow_usd }] }))
};

const fundsCoins = ["BTC", "SOL", "DATA", "XRP", "ETH", "ZEC", "ADA", "AAVE", "XAUT", "LINK", "MANTRA", "INJ", "HYPE", "ALLO", "ENA", "UAI", "BONK", "POL", "EVAA", "XPL"];
const fundsNetFixtures = [4_720_900, 791_500, 562_900, 426_300, 424_100, 134_200, 112_300, 105_200, 85_100, 67_500, 60_300, 46_900, 45_100, 38_900, 34_700, 31_500, 28_600, 24_200, 21_800, 19_500];
const mercuFundRows = [
  { volume_usd: 31_643_900, volume_change_pct: -53.11, inflow_usd: 18_182_400, outflow_usd: 13_461_500, market_cap: 1_300_000_000_000, price: 64_804, price_change_pct: 1.34 },
  { volume_usd: 2_640_300, volume_change_pct: -42.26, inflow_usd: 1_715_900, outflow_usd: 924_400, market_cap: 43_916_000_000, price: 75.57, price_change_pct: 0.83 },
  { volume_usd: 614_400, volume_change_pct: -50.58, inflow_usd: 588_700, outflow_usd: 25_800, market_cap: 97_164_400, price: 0.2736, price_change_pct: 0.22 },
  { volume_usd: 2_105_600, volume_change_pct: -57.32, inflow_usd: 1_265_900, outflow_usd: 839_600, market_cap: 68_198_000_000, price: 1.09, price_change_pct: 0.57 },
  { volume_usd: 4_511_000, volume_change_pct: -82.11, inflow_usd: 2_467_600, outflow_usd: 2_043_500, market_cap: 224_600_000_000, price: 1_863.88, price_change_pct: 1.42 },
  { volume_usd: 1_309_700, volume_change_pct: -54.38, inflow_usd: 721_900, outflow_usd: 587_700, market_cap: 9_360_000_000, price: 562.17, price_change_pct: 3.87 },
  { volume_usd: 401_700, volume_change_pct: -31.04, inflow_usd: 257_000, outflow_usd: 144_700, market_cap: 6_210_000_000, price: 0.1672, price_change_pct: 0.97 },
  { volume_usd: 336_000, volume_change_pct: -71.28, inflow_usd: 220_600, outflow_usd: 115_400, market_cap: 1_363_000_000, price: 90.08, price_change_pct: 0.04 },
  { volume_usd: 129_800, volume_change_pct: -58.27, inflow_usd: 107_500, outflow_usd: 22_400, market_cap: 2_455_000_000, price: 4_007.94, price_change_pct: 0.01 },
  { volume_usd: 245_800, volume_change_pct: -72.14, inflow_usd: 156_600, outflow_usd: 89_100, market_cap: 6_243_000_000, price: 8.37, price_change_pct: 1.61 },
  { volume_usd: 113_600, volume_change_pct: -15.99, inflow_usd: 87_000, outflow_usd: 26_700, market_cap: 33_445_400, price: 0.00625, price_change_pct: -5.30 },
  { volume_usd: 125_100, volume_change_pct: -90.04, inflow_usd: 86_000, outflow_usd: 39_100, market_cap: 514_000_000, price: 5.17, price_change_pct: 1.81 },
  { volume_usd: 1_426_100, volume_change_pct: -72.84, inflow_usd: 735_600, outflow_usd: 690_500, market_cap: 13_403_000_000, price: 40.64, price_change_pct: 1.31 },
] as const;
const fundsAssetFixtures = Array.from({ length: 688 }, (_, index) => {
  const coin = fundsCoins[index] || `T${String(index + 1).padStart(3, "0")}`;
  const net = fundsNetFixtures[index] ?? Math.max(100, (22 - Math.min(index, 21)) * 10_000 + (index % 3) * 700);
  const target = mercuFundRows[index];
  const inflow = target?.inflow_usd ?? net * (2.1 + (index % 20) * 0.03);
  const outflow = target?.outflow_usd ?? inflow - net;
  return { symbol: `${coin}USDT`, coin, price: target?.price ?? (index === 0 ? 0.4356 : 0.08 + index * 0.127), price_change_pct: target?.price_change_pct ?? (index % 3 === 0 ? 8.17 - index * 0.01 : -0.99 - index * 0.02), net_flow_usd: net, net_flow_change_pct: null, inflow_usd: inflow, outflow_usd: outflow, volume_usd: target?.volume_usd ?? Math.max(10_000, 745_000 - index * 800), volume_change_pct: target?.volume_change_pct ?? (index % 2 ? -14.03 - index * 0.01 : 38.19 - index * 0.01), oi_usd: Math.max(100_000, 820_000_000 - index * 1_000_000), oi_change_pct: 1.8 - index * 0.01, funding_pct: -0.02 + index * 0.0001, market_cap: target?.market_cap ?? Math.max(500_000, 102_000_000 - index * 100_000), updated_at: "2026-07-18T17:44:00Z", data_status: "ready", sector: { primary_sector_id: fundsSectorFixtures[index % fundsSectorFixtures.length][0], primary_sector_label: fundsSectorFixtures[index % fundsSectorFixtures.length][1], sector_ids: [fundsSectorFixtures[index % fundsSectorFixtures.length][0]] } };
});
const fundsAssets = {
  schema_version: "2026-07-18",
  catalog_version: "2026.07.1",
  generated_at: "2026-07-18T23:05:00Z",
  window_sec: 3600,
  market_type: "spot",
  data_status: "ready",
  coverage: { assets: 688, flow: 688 },
  warnings: [],
  distribution: { oi_total_usd: 900_000_000, oi_covered_assets: 62, top_10_oi_share_pct: 74.5, top_50_oi_share_pct: 98.2 },
  pagination: { page: 1, page_size: 20, page_count: 35, total: 688 },
  items: fundsAssetFixtures.slice(0, 20)
};

const wideFundRows = [
  ["BTC", 4_147_300, 32_219_500, -55.25, 18_183_400, 14_036_100, 1_300_000_000_000, 64_796.03, 1.32],
  ["SOL", 711_200, 2_708_900, -38.33, 1_710_000, 998_800, 43_916_000_000, 75.54, 0.81],
  ["DATA", 513_500, 567_500, -51.57, 540_500, 27_000, 97_164_400, 0.2733, 0.20],
  ["ETH", 444_000, 4_490_500, -81.12, 2_467_200, 2_023_200, 224_600_000_000, 1_863.68, 1.40],
  ["XRP", 425_300, 2_110_500, -60.51, 1_267_900, 842_600, 68_198_000_000, 1.09, 0.55],
  ["ZEC", 222_900, 1_230_100, -59.82, 726_500, 503_600, 9_360_000_000, 562.50, 3.85],
  ["ADA", 122_200, 409_300, -25.76, 265_800, 143_600, 6_210_000_000, 0.1671, 0.95],
  ["HYPE", 79_600, 1_464_400, -73.49, 772_000, 692_400, 13_403_000_000, 60.38, 1.29],
  ["LINK", 60_900, 247_800, -66.44, 154_300, 93_400, 6_243_000_000, 8.37, 1.59],
  ["INJ", 59_900, 139_800, -86.90, 99_800, 39_900, 514_000_000, 5.17, 1.79],
  ["XAUT", 41_700, 76_000, -75.21, 58_900, 17_200, 2_455_000_000, 4_008.20, 0.01],
  ["APT", 35_200, 99_000, -33.08, 67_100, 31_900, 513_000_000, 0.6100, -0.42],
  ["ETHFI", 34_400, 110_700, -61.86, 72_500, 38_100, 439_000_000, 0.4530, 0.62],
  ["TLM", 28_400, 305_700, -75.43, 167_000, 138_600, 11_281_000, 0.001759, -0.31],
  ["SLX", 27_400, 123_600, 18.71, 75_500, 48_100, 26_606_500, 0.1098, 2.14],
] as const;

const wideFundsAssetFixtures = Array.from({ length: 691 }, (_, index) => {
  const target = wideFundRows[index];
  if (target) {
    const [coin, net_flow_usd, volume_usd, volume_change_pct, inflow_usd, outflow_usd, market_cap, price, price_change_pct] = target;
    return {
      ...fundsAssetFixtures[index], coin, symbol: `${coin}USDT`, net_flow_usd, net_flow_change_pct: null,
      volume_usd, volume_change_pct, inflow_usd, outflow_usd, market_cap, price, price_change_pct,
    };
  }
  const source = fundsAssetFixtures[index] || fundsAssetFixtures[fundsAssetFixtures.length - 1];
  const coin = index < fundsCoins.length ? fundsCoins[index] : `T${String(index + 1).padStart(3, "0")}`;
  return { ...source, coin, symbol: `${coin}USDT`, net_flow_usd: Math.max(100, 20_000 - index * 10) };
});

const wideSectorRows = [
  ["layer1", "L1", 1_210_700], ["privacy", "隐私", 399_600], ["staking", "质押", 353_400],
  ["gaming", "GameFi", 197_900], ["depin", "DePIN", 150_100], ["identity", "身份", 140_300],
  ["exchange", "平台币", 120_000], ["modular", "模块化", 83_900], ["layer2", "L2", 29_600],
  ["payments", "支付", 29_900], ["metals", "贵金属", 26_500], ["defi_inflow", "DeFi", 4_721.6],
  ["data", "数据", -324_200], ["rwa", "RWA", -298_600], ["oracle", "预言机", -207_500],
  ["meme", "Meme", -193_300], ["ai", "AI", -86_900], ["desci", "DeSci", -86_510],
  ["social", "社交", -63_300], ["btc", "BTC生态", -61_000], ["stocks", "股票", -23_400], ["nft", "NFT", -19_500], ["cross_chain", "跨链", -577.08],
] as const;

const wideFundsSectors = {
  ...fundsSectors,
  generated_at: "2026-07-18T23:06:00Z",
  coverage: { ...fundsSectors.coverage, assets: 691, flow: 691, gross_flow: 691 },
  summary: { ...fundsSectors.summary, net_flow_usd: 1_844_600, inflow_usd: 2_746_800, outflow_usd: 902_100, asset_count: 691, covered_assets: 691, leading_inflow_sector: "L1", leading_outflow_sector: "RWA" },
  catalog: wideSectorRows.map(([id, label]) => ({ id, label, description: `${label} 板块` })),
  sectors: wideSectorRows.map(([sector_id, label, net_flow_usd], index) => ({ sector_id, label, net_flow_usd, magnitude_usd: Math.abs(Number(net_flow_usd)), inflow_usd: Number(net_flow_usd) > 0 ? Math.abs(Number(net_flow_usd)) + 150_000 : 80_000 + index * 5_000, outflow_usd: Number(net_flow_usd) < 0 ? Math.abs(Number(net_flow_usd)) + 80_000 : 150_000, asset_count: 8 + index, covered_assets: 8 + index, coverage_ratio: 1, data_status: "ready", leaders: [{ symbol: "BTCUSDT", net_flow_usd }] }))
};

const wideFundsAssets = {
  ...fundsAssets,
  generated_at: "2026-07-18T23:06:00Z",
  coverage: { assets: 691, flow: 691 },
  pagination: { page: 1, page_size: 20, page_count: 35, total: 691 },
  items: wideFundsAssetFixtures.slice(0, 20),
};

const extraInfoFixtures = [
  ...([
    ["2030-07-18T19:13:00Z", "金十", "市场消息：有报道称，美国正对伊朗南部阿巴斯港进行空袭。", ["SINVITEREGI"]],
    ["2030-07-18T19:10:00Z", "金十", "市场消息：初步报道称，伊朗港口城市阿巴斯港发生爆炸。", ["SINVITEREGI"]],
    ["2030-07-18T19:06:00Z", "金十", "美国中央司令部：美军继续严格执行针对伊朗正在实施的海上封锁。", ["SINVITEREGI"]],
    ["2030-07-18T17:21:00Z", "金十", "据The Information：甲骨文(ORCL.N)的数据中心项目面临数十亿美元的意外成本增加。", ["ORCL"]],
    ["2030-07-18T17:19:00Z", "金十", "【晚间独家观点】", ["BTCUSDT", "ETHUSDT", "SOLUSDT"]],
    ["2030-07-18T16:05:00Z", "律动", "🔍 链上侦探｜Abraxas Capital向Hyperliquid存入300万枚USDC加码空单", ["BTCUSDT", "ETHUSDT"]],
    ["2030-07-18T16:04:00Z", "金十", "Uniswap拟首次针对部分v4池启用协议手续费，周日链上投票", ["UNISWAP", "ROBINHOOD"]],
    ["2030-07-18T16:01:00Z", "金十", "【晚间独家观点】", ["SOLUSDT"]],
    ["2030-07-18T15:39:00Z", "金十", "过去24小时全网爆仓1.16亿美元，主爆空单", ["BTCUSDT", "SINVITEREGI"]],
  ] as const).map(([published_at, source, title, symbols], index) => ({
    event_id: `zh_news_${index}`, published_at, collected_at: "2030-07-18T20:31:00Z", source, source_type: "news", title,
    summary: undefined, url: `https://example.com/zh/${index}`, symbols: [...symbols], importance: "medium", language: "zh",
    cluster_id: `cluster_zh_${index}`, cluster_size: 1, event_kind: "neutral", rights_status: "public_rss_link", timestamp_quality: "source", data_status: "ready", source_links: [],
  })),
  ...([
    ["2030-07-18T23:02:00Z", "wfwitness", "⚡🏁追寻追寻追寻，英格兰队击败法国队获得世界杯季军。 🟡 @wf见证人"],
    ["2030-07-18T23:01:00Z", "wfwitness", "⚡🇫🇷追逐信赖创造信赖信赖⚽ 英格兰队对阵法国队打入第六球。 4-6 @wf见证人"],
    ["2030-07-18T22:59:00Z", "wfwitness", "⚡🇫🇷追寻追寻追寻，法国队打入第四球，将比分追至4-5。 @wf见证人"],
    ["2030-07-18T22:58:00Z", "wfwitness", "⚡AURU继续男团新的弹道导弹威胁后，乌克兰基辅再次发出警报。 @wf见证人"],
    ["2030-07-18T22:56:00Z", "wfwitness", "⚡IR 塔斯尼姆通讯社报道，伊朗唐格发生爆炸。 @wf见证人"],
    ["2030-07-18T22:53:00Z", "wfwitness", "⚡IR 未经证实的报道称，伊朗伊姆发生爆炸。 @wf见证人"],
    ["2030-07-18T22:52:00Z", "wfwitness", "乌克兰基辅发生爆炸"],
    ["2030-07-18T22:51:00Z", "wfwitness", "⚡追逐信赖创造信赖信赖⚽ 英格兰队在点球大战中对法国队打进第五球。 3-5 @wf见证人"],
    ["2030-07-18T22:48:00Z", "wfwitness", "⚡IRUS 塔斯尼姆：阿巴斯港听到的声音已得到证实，但目前尚未发生爆炸。 @wf见证人"],
    ["2030-07-18T22:46:00Z", "wfwitness", "⚡IR 未经证实的报道称，伊朗班达尔伦格发生爆炸。 @wf见证人"],
    ["2030-07-18T22:40:00Z", "wfwitness", "⚡AURU 发射多枚爱国者导弹后，基辅上空传来爆炸声。 @wf见证人"],
  ] as const).map(([published_at, source, title], index) => ({
    event_id: `en_news_${index}`, published_at, collected_at: "2030-07-18T20:31:00Z", source, source_type: "news", title,
    summary: undefined, url: `https://example.com/en/${index}`, symbols: [], importance: "medium", language: "en",
    cluster_id: `cluster_en_${index}`, cluster_size: 1, event_kind: "neutral", rights_status: "public_rss_link", timestamp_quality: "source", data_status: "ready", source_links: [],
  })),
  ...Array.from({ length: 9 }, (_, index) => {
    const posts = [
      "残酷现实：Meme 泡沫的淘金者，一天亏损 89%｜Coinbase CEO 更换头像，Base Meme 市值快速升温。",
      "这就是为什么我如此热爱宏观，也是为什么我坚信它不仅要继续存在，而且可能会主宰下一个周期。",
      "休息吧",
      "周末，现在真有休息日了",
      "ONDO 团队关联地址向交易所转移2605万枚代币，价值979万美元；从 Ondo 团队多签处收到1.5亿枚 ONDO 的接收地址，11小时前终于向 Coinbase 充值。",
      "七月以来累计资金超1.03亿美元 ETH 和 WBTC 的巨鲸，实体再次加仓；现在累计囤积49407枚ETH和400枚WBTC。",
      "先定 10 个大目标：老哥 @Jason60704294 再次开多 BTC！今天凌晨他发推表示已开出69.4枚多单。",
      "📋 每日情报——2026-07-18｜07-17 08:00→07-18 08:00 SGT 覆盖24小时情报、群聊早报与代币雷达输出。",
      "再跌个20%，应该就差不多大底了"
    ];
    const authors = ["AI越", "OverDose", "a7lincrypto 热度资本", "a7lincrypto 热度资本", "AI越", "AI越", "AI越", "0xWizard", "a7lincrypto 热度资本"];
    const times = ["10:54", "10:33", "05:17", "05:17", "03:27", "03:22", "03:12", "00:51", "20:08"];
    return ({
    event_id: `kol_${index}`,
    published_at: `2030-07-17T${times[index]}:00Z`,
    collected_at: "2030-07-18T20:31:00Z",
    source: authors[index],
    source_type: "kol",
    title: posts[index],
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
  }); }),
  ...Array.from({ length: 14 }, (_, index) => {
    const coins = ["BANK", "SPCX", "AKE", "BTC", "SNDK", "ETH", "CL", "XAU"];
    const coin = coins[index % coins.length];
    return {
      event_id: `plaza_${index}`,
      published_at: index < 2 ? `2030-07-18T20:${String(29 - index).padStart(2, "0")}:00Z` : `2030-07-18T15:${String(57 - index).padStart(2, "0")}:00Z`,
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
  plaza_rankings: {
    schema_version: "workstation.info.plaza.v2",
    generated_at: "2030-07-18T20:35:00Z",
    data_status: "ready",
    provider: { id: "binance_square", label: "币安广场", kind: "target_visual_fixture", rights_status: "fixture_only" },
    coverage: { active_4h: 2, total_24h: 8, market_linked: 8 },
    active_4h: [
      { symbol: "BANKUSDT", coin: "BANK", posts: 76, recent_1h_posts: 1, previous_1h_posts: 1, recent_ratio: 1.0, is_new: false, positive: 44, negative: 32, positive_pct: 58, negative_pct: 42, sentiment: "bullish", sentiment_confidence_pct: 58, engagement: 18640, engagement_per_post: 245, price_change_pct: 1.0, futures_flow_usd: 4_800_000, futures_flow_strength: 71, summary: "BANK 公开讨论热度与资金方向同步升温。" },
      { symbol: "SPCXUSDT", coin: "SPCX", asset_type: "美股", posts: 103, recent_1h_posts: 1, previous_1h_posts: 1, recent_ratio: 0.8, is_new: false, positive: 57, negative: 46, positive_pct: 55, negative_pct: 45, sentiment: "neutral", sentiment_confidence_pct: 55, engagement: 22480, engagement_per_post: 218, price_change_pct: 10.8, futures_flow_usd: -3_100_000, futures_flow_strength: 66, summary: "SPCX 热度快速抬升，方向仍需价格确认。" }
    ],
    total_24h: [
      { symbol: "AKEUSDT", coin: "AKE", posts: 466, recent_1h_posts: 0, positive: 55, negative: 40, positive_pct: 55, negative_pct: 40, sentiment: "bullish", sentiment_confidence_pct: 55, engagement: 93800, engagement_per_post: 201, price_change_pct: 32.96, futures_flow_usd: -8_400_000, futures_flow_strength: 70, futures_long_pct: 30, futures_short_pct: 70, summary: "散户主流偏多，关注主力连续转弱与撮合偏萎，但亏损抱怨较多。共识强度中等，多空持仓均衡；若散户多头继续加仓而资金费率转负，警惕高位反向收割。" },
      { symbol: "BTCUSDT", coin: "BTC", posts: 393, recent_1h_posts: 0, positive: 25, negative: 55, positive_pct: 25, negative_pct: 55, sentiment: "bearish", sentiment_confidence_pct: 55, engagement: 87120, engagement_per_post: 222, price_change_pct: 1.32, futures_flow_usd: 12_800_000, futures_flow_strength: 59, futures_long_pct: 59, futures_short_pct: 41, summary: "散户主流偏空，核心说法为地缘冲突、日线暴跌及比特币独立下行缺口。共识强度中等偏空，但合约多空比未明确极端，警惕空头陷阱与周末流动性抽离。" },
      { symbol: "SNDKUSDT", coin: "SNDK", asset_type: "美股", posts: 123, recent_1h_posts: 0, positive: 40, negative: 45, positive_pct: 40, negative_pct: 45, sentiment: "neutral", sentiment_confidence_pct: 45, engagement: 31860, engagement_per_post: 259, price_change_pct: 0.19, futures_flow_usd: 2_300_000, futures_flow_strength: 81, futures_long_pct: 81, futures_short_pct: 19, summary: "散户主流偏空、担忧暴跌延缓，核心说法为华尔街看空、套牢盘沉重。共识分歧明显，看空略占优；若散户过度悲观而合约LSR未极空，警惕空头陷阱。" },
      { symbol: "ETHUSDT", coin: "ETH", posts: 252, recent_1h_posts: 0, positive: 60, negative: 20, positive_pct: 60, negative_pct: 20, sentiment: "bullish", sentiment_confidence_pct: 70, engagement: 58200, engagement_per_post: 231, price_change_pct: 1.35, futures_flow_usd: 9_700_000, futures_flow_strength: 70, futures_long_pct: 70, futures_short_pct: 30, summary: "散户主流看多 ETH，核心逻辑：ETH走强资金流入生态、做多盈利案例激增。共识中等偏强但链上层拥堵率与日线暴跌风险未消退，警惕散户杠杆接盘被收割。" },
      { symbol: "CLUSDT", coin: "CL", posts: 85, recent_1h_posts: 0, positive: 92, negative: 3, positive_pct: 92, negative_pct: 3, sentiment: "bullish", sentiment_confidence_pct: 92, engagement: 17240, engagement_per_post: 203, price_change_pct: 2.02, futures_flow_usd: 1_900_000, futures_flow_strength: 60, futures_long_pct: 60, futures_short_pct: 40, summary: "散户一边倒看多，核心逻辑是美伊冲突升级导致供应中断，油价将冲100。共识极强但地缘溢价已快速兑现，注意利多出尽后的多头踩踏风险。" },
      { symbol: "SPCXUSDT", coin: "SPCX", asset_type: "美股", posts: 104, recent_1h_posts: 1, positive: 20, negative: 60, positive_pct: 20, negative_pct: 60, sentiment: "bearish", sentiment_confidence_pct: 85, engagement: 24460, engagement_per_post: 235, price_change_pct: 2.93, futures_flow_usd: -4_600_000, futures_flow_strength: 85, futures_long_pct: 85, futures_short_pct: 15, summary: "散户主流看空，核心说法为高能估值、解锁压力、股价破发。共识较强但空头仓位极高，警惕轧空风险；散户做空可能被反向收割。" },
      { symbol: "BNBUSDT", coin: "BNB", posts: 68, recent_1h_posts: 0, positive: 65, negative: 10, positive_pct: 65, negative_pct: 10, sentiment: "bullish", sentiment_confidence_pct: 76, engagement: 14600, engagement_per_post: 215, price_change_pct: 0.53, futures_flow_usd: 3_100_000, futures_flow_strength: 76, futures_long_pct: 76, futures_short_pct: 24, summary: "散户主流偏多，核心说法为BNB销毁减少流通、长期看破千。共识强度中等，但缺乏空头对冲，需警惕一致性预期下的主力反向收割风险。" },
      { symbol: "SOLUSDT", coin: "SOL", posts: 78, recent_1h_posts: 0, positive: 58, negative: 27, positive_pct: 58, negative_pct: 27, sentiment: "bullish", sentiment_confidence_pct: 58, engagement: 13600, engagement_per_post: 174, price_change_pct: 0.77, futures_flow_usd: 2_100_000, futures_flow_strength: 62, futures_long_pct: 62, futures_short_pct: 38, summary: "SOL 讨论热度保持高位，主力资金与广场方向仍需持续确认。" }
    ]
  },
  items: [
    { event_id: "panews_btc", published_at: "2030-07-18T19:18:00Z", collected_at: "2030-07-18T20:31:00Z", source: "金十", source_type: "news", title: "据伊朗学生通讯社：几分钟开始，有关阿巴斯港传出爆炸声的消息陆续出现。", summary: "", url: "https://example.com/zh/lead", symbols: ["SINVITEREGI"], importance: "medium", language: "zh", cluster_id: "cluster_btc_zh", cluster_size: 1, event_kind: "neutral", rights_status: "public_rss_link", timestamp_quality: "source", data_status: "ready", source_links: [] },
    { event_id: "decrypt_eth", published_at: "2030-07-18T23:04:00Z", collected_at: "2030-07-18T23:04:10Z", source: "marketfeed", source_type: "news", title: "英格兰队 6-4 击败法国队，获得世界杯第三名 [...] (https://x.com/Deltaone/status/2078616750799679922)", summary: "", url: "https://x.com/Deltaone/status/2078616750799679922", symbols: [], importance: "medium", language: "en", cluster_id: "cluster_eth_en", cluster_size: 1, event_kind: "neutral", rights_status: "public_social_link", timestamp_quality: "source", data_status: "ready", source_links: [] },
    { event_id: "bsky_kol_sol", published_at: "2030-07-17T12:00:00Z", collected_at: "2030-07-18T20:31:00Z", source: "0xWizard", source_type: "kol", title: "热门币雷达（24h）｜2026-07-17 20:00（新加坡时间）分析 686 条频道消息，发现 13 个代币。BRIAN · 6源口详情，继续跟踪价格与资金确认。", url: "https://bsky.app/profile/analyst.bsky.social/post/sol", symbols: ["SOLUSDT"], importance: "high", language: "zh", cluster_id: "cluster_sol_kol", cluster_size: 1, event_kind: "neutral", rights_status: "public_social_link", timestamp_quality: "source", data_status: "ready", source_links: [], ai_analysis: { status: "not_generated", engagement: { likes: 188, reposts: 28, replies: 14, score: 258 } } },
    { event_id: "bsky_plaza_doge", published_at: "2030-07-18T15:30:00Z", collected_at: "2030-07-18T20:31:00Z", source: "@market.bsky.social", source_type: "plaza", title: "$DOGE breakout discussion is surging across the public feed.", url: "https://bsky.app/profile/market.bsky.social/post/doge", symbols: ["DOGEUSDT"], importance: "medium", language: "en", cluster_id: "cluster_doge_plaza", cluster_size: 1, event_kind: "opportunity", rights_status: "public_social_link", timestamp_quality: "source", data_status: "ready", source_links: [], ai_analysis: { status: "not_generated", engagement: { likes: 96, reposts: 12, replies: 8, score: 128 } } },
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

async function mockPublicApi(page: Page, options: { streamSignal?: boolean; agents?: unknown; assetWarnings?: string[]; healthStatus?: "ok" | "degraded"; radarVisual?: "1440x900" | "1920x1080"; radarFailure?: "momentum-windows" | "anomalies" | "surge" | "rank" } = {}) {
  let signalRequests = 0;
  let infoRequests = 0;
  let lastInfoSearch = "";
  let streamRequests = 0;
  let streamDelivered = false;
  let signalsFail = false;
  let agentsFail = false;
  let agentRequests = 0;
  let legacyRealtimeRequests = 0;
  const radarModuleRequests = new Set<string>();
  const visualRadar = options.radarVisual ? mercuRadarFixture(options.radarVisual) : null;
  const visualFundsSectors = options.radarVisual === "1920x1080" ? wideFundsSectors : fundsSectors;
  const visualFundsAssets = options.radarVisual === "1920x1080" ? wideFundsAssets : fundsAssets;
  const visualFundRows = options.radarVisual === "1920x1080" ? wideFundsAssetFixtures : fundsAssetFixtures;
  const radarRealtime = visualRadar?.realtime || realtimeIntelligence;
  if (!options.radarVisual) await page.route("https://cdn.jsdelivr.net/**", (route) => route.abort("failed"));
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
    if (url.pathname === "/public-api/market/overview") return route.fulfill({ json: { ok: true, data: visualRadar?.overview || marketOverview } });
    if (url.pathname === "/public-api/radar/boards") return route.fulfill({ json: { ok: true, data: visualRadar?.boards || radarBoards } });
    if (url.pathname === "/public-api/workstation/radar/momentum-windows") {
      radarModuleRequests.add("momentum-windows");
      if (options.radarFailure === "momentum-windows") return route.fulfill({ status: 503, json: { ok: false, message: "momentum unavailable" } });
      return route.fulfill({
        json: {
          ok: true,
          data: {
            windows: Object.fromEntries(["15m", "30m", "1h", "4h", "1d"].map((window) => [window, { ...(visualRadar?.boards || radarBoards), window }]))
          }
        }
      });
    }
    if (url.pathname === "/public-api/workstation/radar/momentum") return route.fulfill({ json: { ok: true, data: visualRadar?.boards || radarBoards } });
    if (url.pathname === "/public-api/workstation/radar/anomalies") {
      radarModuleRequests.add("anomalies");
      if (options.radarFailure === "anomalies") return route.fulfill({ status: 503, json: { ok: false, message: "anomalies unavailable" } });
      return route.fulfill({ json: { ok: true, data: { schema_version: "workstation.radar.anomalies.v1", generated_at: radarRealtime.generated_at, observed_at: radarRealtime.observed_at, data_status: radarRealtime.data_status, coverage: radarRealtime.coverage, items: radarRealtime.anomaly_events } } });
    }
    if (url.pathname === "/public-api/workstation/radar/surge") {
      radarModuleRequests.add("surge");
      if (options.radarFailure === "surge") return route.fulfill({ status: 503, json: { ok: false, message: "surge unavailable" } });
      return route.fulfill({ json: { ok: true, data: { schema_version: "workstation.radar.surge.v1", generated_at: radarRealtime.generated_at, observed_at: radarRealtime.observed_at, data_status: radarRealtime.data_status, coverage: radarRealtime.coverage, items: radarRealtime.items.filter((item) => item.surge?.triggered).sort((a, b) => Number(b.surge?.score || 0) - Number(a.surge?.score || 0)).slice(0, 5) } } });
    }
    if (url.pathname === "/public-api/workstation/radar/rank") {
      radarModuleRequests.add("rank");
      if (options.radarFailure === "rank") return route.fulfill({ status: 503, json: { ok: false, message: "rank unavailable" } });
      return route.fulfill({ json: { ok: true, data: { schema_version: "workstation.radar.rank.v1", generated_at: radarRealtime.generated_at, observed_at: radarRealtime.observed_at, data_status: radarRealtime.data_status, coverage: radarRealtime.coverage, universe: radarRealtime.items, total: radarRealtime.items.filter((item) => Number(item.anomaly_24h?.count || 0) > 0).sort((a, b) => Number(b.anomaly_24h?.count || 0) - Number(a.anomaly_24h?.count || 0)).slice(0, 14), ambush: radarRealtime.items.filter((item) => item.ambush?.triggered).sort((a, b) => Number(b.ambush?.score || 0) - Number(a.ambush?.score || 0)).slice(0, 8) } } });
    }
    if (url.pathname === "/public-api/workstation/radar/briefs") return route.fulfill({ json: { ok: true, data: { schema_version: "workstation.radar.briefs.v1", generated_at: radarRealtime.generated_at, observed_at: radarRealtime.observed_at, data_status: radarRealtime.data_status, coverage: radarRealtime.coverage, items: radarRealtime.anomaly_events.slice(0, 6).map((item) => ({ ...item, title: `${item.coin} ${item.label}`, summary: "detail" in item ? item.detail : "" })) } } });
    if (url.pathname === "/public-api/radar/realtime-intelligence") {
      legacyRealtimeRequests += 1;
      return route.fulfill({ json: { ok: true, data: radarRealtime } });
    }
    if (url.pathname === "/public-api/workstation/funds/open-interest") return route.fulfill({ json: { ok: true, data: { ...crossExchangeOi, symbol: url.searchParams.get("symbol") || "BTCUSDT" } } });
    if (url.pathname === "/public-api/funds/sectors") return route.fulfill({ json: { ok: true, data: visualFundsSectors } });
    if (url.pathname === "/public-api/funds/assets") {
      const page = Math.max(1, Number(url.searchParams.get("page") || 1));
      const pageSize = Math.max(1, Number(url.searchParams.get("page_size") || 20));
      const search = String(url.searchParams.get("search") || "").toUpperCase();
      const sort = String(url.searchParams.get("sort") || "net_flow_usd") as keyof (typeof fundsAssetFixtures)[number];
      const direction = url.searchParams.get("direction") === "asc" ? 1 : -1;
      const filtered = visualFundRows.filter((item) => !search || `${item.symbol} ${item.coin}`.includes(search));
      filtered.sort((a, b) => (Number(a[sort] ?? Number.NEGATIVE_INFINITY) - Number(b[sort] ?? Number.NEGATIVE_INFINITY)) * direction);
      const total = filtered.length;
      const items = filtered.slice((page - 1) * pageSize, page * pageSize);
      return route.fulfill({ json: { ok: true, data: { ...visualFundsAssets, market_type: url.searchParams.get("market_type") || "spot", window_sec: Number(url.searchParams.get("window_sec") || 900), warnings: options.assetWarnings || visualFundsAssets.warnings, sort: { key: sort, direction: direction === 1 ? "asc" : "desc" }, pagination: { page, page_size: pageSize, page_count: Math.max(1, Math.ceil(total / pageSize)), total }, items } } });
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
    legacyRealtimeRequests: () => legacyRealtimeRequests,
    radarModuleRequests: () => [...radarModuleRequests],
    releaseSignal: () => { streamDelivered = true; },
    failSignals: () => { signalsFail = true; },
    failAgents: () => { agentsFail = true; },
  };
}

test("desktop radar exposes the independent workstation modules", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.goto("/radar");

  for (const heading of ["异动监控", "热钱观察榜单", "全场态势", "Surge 飙升榜", "24h 异动总榜", "埋伏池"]) {
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
  }
  await expect(page.getByLabel(/五窗口共振/).first()).toBeVisible();
  await expect(page.getByText("强度榜").first()).toBeVisible();
  await expect(page.getByText(/96%/).first()).toBeVisible();
  await expect(page.getByText(/较上一周期 \+\$23\.0M → \+\$18\.0M/)).toBeVisible();
  await expect(page.getByText(/环比转正 \$12\.8M/)).toBeVisible();
  await expect(page.getByTestId("radar-side-intelligence").getByText(/3榜/).first()).toBeVisible();
  await expect(page.getByTestId("radar-side-intelligence").getByText(/4榜|5榜/)).toHaveCount(0);
  await expect(page.getByTestId("radar-event-feed").getByText(/多头共振|空头共振/)).toHaveCount(0);
  expect(new Set(state.radarModuleRequests())).toEqual(new Set(["momentum-windows", "anomalies", "surge", "rank"]));
  expect(state.legacyRealtimeRequests()).toBe(0);
});

test("radar preserves healthy modules when one independent request fails", async ({ page }) => {
  await mockPublicApi(page, { radarFailure: "surge" });
  await page.goto("/radar");

  await expect(page.getByText(/1 个雷达模块暂时不可用/)).toBeVisible();
  await expect(page.getByText("+5.80%", { exact: true }).first()).toBeVisible();
  await expect(page.getByTestId("radar-event-feed").getByText("BTC", { exact: false }).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "24h 异动总榜" })).toBeVisible();
});

test("desktop radar mirrors the target three-column scan hierarchy", async ({ page }) => {
  await page.setViewportSize({ width: 1152, height: 720 });
  await mockPublicApi(page);
  await page.goto("/radar");

  const eventBox = await page.getByTestId("radar-event-feed").boundingBox();
  const matrixBox = await page.getByTestId("radar-hot-money").boundingBox();
  const sideBox = await page.getByTestId("radar-side-intelligence").boundingBox();
  expect(eventBox?.x).toBeCloseTo(10, 0);
  expect(eventBox?.y).toBeCloseTo(54, 0);
  expect(eventBox?.width).toBeCloseTo(230, 0);
  expect(matrixBox?.x).toBeCloseTo(251, 0);
  expect(matrixBox?.width).toBeCloseTo(650, 0);
  expect(sideBox?.width).toBeCloseTo(230, 0);
  const sideRow = await page.getByTestId("radar-side-intelligence").locator("section").first().locator("a").first().boundingBox();
  expect(sideRow?.height).toBeCloseTo(24, 0);
  expect(eventBox?.height).toBeCloseTo(656, 0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1152);
});

test("1920 reference geometry keeps Mercu-sized radar rails and funds overview", async ({ page }) => {
  await page.setViewportSize({ width: 1536, height: 864 });
  await mockPublicApi(page);
  await page.goto("/radar");

  const event = await page.getByTestId("radar-event-feed").boundingBox();
  const center = await page.getByTestId("radar-hot-money").boundingBox();
  const side = await page.getByTestId("radar-side-intelligence").boundingBox();
  expect(event?.x).toBeCloseTo(10, 0);
  expect(event?.width).toBeCloseTo(230, 0);
  expect(center?.x).toBeCloseTo(251, 0);
  expect(center?.width).toBeCloseTo(1034, 0);
  expect(side?.x).toBeCloseTo(1296, 0);
  expect(side?.width).toBeCloseTo(230, 0);
  const strengthRow = await page.getByTestId("radar-strength-grid").first().locator("a").first().boundingBox();
  expect(strengthRow?.height).toBeCloseTo(40, 0);

  await page.goto("/info");
  const infoBanner = await page.getByRole("heading", { name: "AI 信息蒸馏" }).locator("xpath=ancestor::section").boundingBox();
  const infoColumns = await page.getByTestId("info-four-columns").boundingBox();
  expect(infoBanner?.y).toBeCloseTo(52, 0);
  expect(infoBanner?.height).toBeCloseTo(36, 0);
  expect(infoColumns?.y).toBeCloseTo(101, 0);
  const infoDigestIcon = await page.getByTestId("info-digest-icon").boundingBox();
  const infoDigestButton = await page.getByRole("button", { name: /4h AI 综合分析/ }).boundingBox();
  expect(infoDigestIcon?.x).toBeCloseTo(15, 0);
  expect(infoDigestIcon?.width).toBeCloseTo(30, 0);
  expect(infoDigestButton?.width).toBeGreaterThanOrEqual(125);

  await page.goto("/funds");
  const sector = await page.getByRole("heading", { name: "板块资金流" }).locator("xpath=ancestor::section").boundingBox();
  const assets = await page.getByTestId("funds-assets-overview").boundingBox();
  const assetSearch = await page.getByLabel("搜索全体代币").boundingBox();
  expect(sector?.x).toBeCloseTo(16, 0);
  expect(sector?.y).toBeCloseTo(106, 0);
  expect(sector?.width).toBeCloseTo(290, 0);
  expect(assets?.x).toBeCloseTo(318, 0);
  expect(assets?.width).toBeCloseTo(1202, 0);
  expect(assetSearch?.x).toBeCloseTo(1259, 0);
  expect(assetSearch?.width).toBeCloseTo(276, 0);
  const wideFundColumns = await page.getByTestId("funds-asset-row").first().locator(":scope > *").evaluateAll((elements) => elements.slice(0, 3).map((element) => element.getBoundingClientRect().width));
  expect(wideFundColumns).toEqual([50, 158, 132]);
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
  expect(infoColumns.reduce((sum, column) => sum + column.width, 0)).toBeGreaterThanOrEqual(884);
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
  const compactFundColumns = await page.getByTestId("funds-asset-row").first().locator(":scope > *").evaluateAll((elements) => elements.slice(0, 3).map((element) => element.getBoundingClientRect().width));
  expect(compactFundColumns).toEqual([40, 150, 120]);
});

test("funds table sorting and browser-local favorites are functional", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/funds");

  await expect(page.getByTestId("funds-asset-row").first().locator(":scope > span").nth(2)).toHaveText("—");

  await page.getByLabel("添加ALLO自选").click();
  await expect(page.getByLabel("取消ALLO自选")).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("取消ALLO自选")).toBeVisible();

  const volumeSort = page.getByLabel("按交易量($)排序");
  await volumeSort.click();
  await expect(volumeSort).toHaveText("交易量($)↓");
  await volumeSort.click();
  await expect(volumeSort).toHaveText("交易量($)↑");
  await expect(page.getByTestId("funds-assets-overview").locator('[role="button"]').filter({ hasText: "T688" })).toBeVisible();
});

for (const viewport of [
  { css: { width: 1152, height: 720 }, pixels: { width: 1440, height: 900 } },
  { css: { width: 1536, height: 864 }, pixels: { width: 1920, height: 1080 } },
]) {
  test(`workstation visual fixtures remain stable at ${viewport.pixels.width}x${viewport.pixels.height}`, async ({ page }) => {
    await page.setViewportSize(viewport.css);
    await mockPublicApi(page, { radarVisual: viewport.pixels.width === 1440 ? "1440x900" : "1920x1080" });
    for (const route of ["radar", "info", "funds"] as const) {
      const fixedTimes = viewport.pixels.width === 1440
        ? { radar: "2030-07-18T23:00:07Z", info: "2030-07-18T23:04:42Z", funds: "2030-07-18T23:05:11Z" }
        : { radar: "2030-07-18T23:04:13Z", info: "2030-07-18T23:04:58Z", funds: "2030-07-18T23:06:32Z" };
      await page.clock.setFixedTime(new Date(fixedTimes[route]));
      await page.goto(`/${route}`);
      await expect(page.getByTestId(`${route}-workstation`)).toBeVisible();
      await expect(page.getByTestId(`${route}-workstation`)).toHaveAttribute("aria-busy", "false");
      await page.locator("img").evaluateAll(async (images) => {
        await Promise.all(images.map((image) => (image as HTMLImageElement).decode().catch(() => undefined)));
      });
      await page.addStyleTag({ content: "nextjs-portal { display: none !important; }" });
      await expect(page).toHaveScreenshot(`${route}-${viewport.pixels.width}x${viewport.pixels.height}.png`, { animations: "disabled", maxDiffPixelRatio: 0.035, scale: "device" });
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

  await expect(page.getByText("共 688 个代币 · 每页 20 条 · 第 1/35 页")).toBeVisible();
  const secondPageResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/public-api/funds/assets" && url.searchParams.get("market_type") === "spot" && url.searchParams.get("page") === "2";
  });
  await page.getByRole("button", { name: "下一页" }).click();
  await secondPageResponse;
  await expect(page.getByText("共 688 个代币 · 每页 20 条 · 第 2/35 页")).toBeVisible();
  await expect(page.getByText("21", { exact: true }).first()).toBeVisible();

  const searchResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/public-api/funds/assets" && url.searchParams.get("market_type") === "spot" && url.searchParams.get("search") === "BTC";
  });
  await page.getByLabel("搜索全体代币").fill("BTC");
  await searchResponse;
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
  for (const heading of ["聚合资讯", "英文流资讯", "KOL聚合资讯", "币安广场情绪"]) await expect(page.getByRole("heading", { name: heading }).first()).toBeVisible();
  await expect(page.getByLabel("搜索KOL聚合资讯")).toHaveCount(0);
  await expect(page.getByText(/广场 多/).first()).toBeVisible();
  await expect(page.getByText("466 帖", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: /据伊朗学生通讯社/ }).first()).toBeVisible();
  await expect(page.getByRole("link").filter({ hasText: "据伊朗学生通讯社" }).first()).toHaveAttribute("rel", "noreferrer");
});

test("information workstation loads each source column independently", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.goto("/info");

  await expect(page.getByRole("heading", { name: /据伊朗学生通讯社/ }).first()).toBeVisible();
  await expect.poll(state.infoRequests).toBe(4);
  await page.getByRole("button", { name: /4h AI 综合分析/ }).click();
  await expect.poll(state.infoRequests).toBe(8);
});

test("390px information workstation stacks its four columns", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockPublicApi(page);
  await page.goto("/info");

  await expect(page.getByRole("heading", { name: "聚合资讯" }).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: /据伊朗学生通讯社/ }).first()).toBeVisible();
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
  await page.setViewportSize({ width: 390, height: 844 });
  await mockPublicApi(page);
  await page.goto("/radar");

  await expect(page.getByText("30s 增量", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "暂停" }).click();
  await expect(page.getByText("已暂停", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "立即更新" }).click();
  await expect(page.getByRole("heading", { name: "异动总榜" })).toBeVisible();
  await page.getByRole("button", { name: "继续" }).click();
  await expect(page.getByText("30s 增量", { exact: false })).toBeVisible();
});

test("reserved AI surface never exposes copied directional conclusions", async ({ page }) => {
  const state = await mockPublicApi(page, { agents: agentsOverview });
  await page.goto("/agents");

  await expect(page.getByText("当前页面不请求 AI 决策接口", { exact: false })).toBeVisible();
  await expect(page.getByText("同步增强", { exact: true })).toHaveCount(0);
  expect(state.agentRequests()).toBe(0);
});
