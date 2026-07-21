type Viewport = "1440x900" | "1920x1080";
type Unit = "percent" | "usd";
type RankedValue = readonly [coin: string, value: number, strength?: number, windowPattern?: string];
const WINDOW_KEYS = ["15m", "30m", "1h", "4h", "1d"] as const;

function ranked(entries: readonly RankedValue[], unit: Unit) {
  return entries.map(([coin, value, strength, windowPattern], index) => ({
    symbol: `${coin}USDT`,
    coin,
    value,
    unit,
    strength_percentile: strength ?? Math.max(84, 100 - index * 2),
    window_states: windowPattern
      ? Object.fromEntries(WINDOW_KEYS.map((key, windowIndex) => [key, windowPattern[windowIndex] === "1"]))
      : undefined,
  }));
}

function board(
  key: string,
  unit: Unit,
  amountPositive: readonly RankedValue[],
  amountNegative: readonly RankedValue[],
  strengthPositive: readonly RankedValue[],
  strengthNegative: readonly RankedValue[],
) {
  return {
    key,
    title: key,
    available: true,
    coverage: 100,
    amount_positive: { title: "量级榜", items: ranked(amountPositive, unit) },
    amount_negative: { title: "量级榜", items: ranked(amountNegative, unit) },
    strength_positive: { title: "强度榜", items: ranked(strengthPositive, unit) },
    strength_negative: { title: "强度榜", items: ranked(strengthNegative, unit) },
  };
}

const compactBoards = [
  board(
    "price", "percent",
    [["CASHCAT", 3.72, undefined, "11110"], ["PROM", 2.46, undefined, "11111"], ["XVG", 1.75, undefined, "11100"], ["H", 1.41, undefined, "00000"], ["ATH", 1.37, undefined, "11110"], ["US", 1.36, undefined, "10000"], ["PUMP", 1.35, undefined, "10000"]],
    [["SKHYNIX", -3.38, undefined, "11000"], ["ZEREBRO", -2.95, undefined, "10000"], ["BABA", -1.97, undefined, "11000"], ["JELLYJELLY", -1.76, undefined, "11111"], ["XEC", -1.43, undefined, "10000"], ["1000XEC", -1.10, undefined, "00000"], ["EDGE", -1.09, undefined, "10000"]],
    [["CASHCAT", 3.72, 100, "11100"], ["XVG", 1.75, 100, "11000"], ["PROM", 2.46, 99, "11100"], ["GIGGLE", 1.20, 99, "10000"], ["CYS", 0.85, 99, "10000"], ["AVAX", 0.80, 97, "10000"], ["PUMP", 1.35, 97, "10000"], ["US", 1.10, 96, "10000"]],
    [["BABA", -1.97, 100, "11000"], ["ZEREBRO", -2.95, 99, "10000"], ["SKHYNIX", -3.38, 99, "11000"], ["PROM", -0.90, 98, "10000"], ["XEC", -1.43, 98, "10000"], ["JELLYJELLY", -1.76, 97, "11100"], ["YB", -0.77, 96, "10000"], ["CRV", -0.70, 93, "10000"]],
  ),
  board(
    "oi", "usd",
    [["BTC", 23_900_000, 100, "11111"], ["ETH", 7_800_000, 99, "11111"], ["PUMP", 2_800_000, 98, "11111"], ["ATH", 1_800_000, 98, "11000"], ["SKHYNIX", 1_600_000, 97, "11110"], ["HYPE", 1_000_000, 97, "11111"], ["ALT", 980_000, 96, "11100"]],
    [["US", -3_300_000, 100, "11000"], ["ZEC", -2_600_000, 99, "11111"], ["XRP", -2_600_000, 99, "11000"], ["BANK", -2_000_000, 98, "11100"], ["NEAR", -1_700_000, 98, "10000"], ["LIT", -883_000, 97, "10000"], ["ZEREBRO", -761_000, 96, "10000"]],
    [["SKHYNIX", 1_600_000, 100, "11110"], ["ATH", 980_000, 98, "11100"], ["XVG", 116_000, 100, "11000"], ["DOGE", 19_000, 99, "10000"], ["PROM", 246_000, 99, "11100"], ["PUMP", 2_800_000, 97, "11111"], ["XEC", 26_000, 98, "11000"], ["GIGGLE", 19_000, 97, "10000"]],
    [["PROM", -1_003_000, 100, "11000"], ["SKHYNIX", -3_300_000, 100, "11000"], ["BANK", -2_000_000, 99, "11100"], ["YB", -770_000, 99, "10000"], ["ZEREBRO", -761_000, 99, "10000"], ["JELLYJELLY", -220_000, 98, "10000"], ["XEC", -30_000, 98, "10000"], ["KAITO", -18_000, 97, "10000"]],
  ),
  board(
    "futures_flow", "usd",
    [["BTC", 20_700_000, 100, "11111"], ["SOL", 11_300_000, 99, "11111"], ["ETH", 7_200_000, 99, "11111"], ["HYPE", 1_500_000, 98, "11111"], ["XRP", 900_000, 98, "11011"], ["PUMP", 600_000, 97, "11100"], ["ESPORTS", 500_000, 96, "11100"]],
    [["ONDO", -5_900_000, 100, "11111"], ["LINK", -1_600_000, 99, "11000"], ["BABA", -1_400_000, 99, "11000"], ["SKHYNIX", -1_100_000, 98, "11000"], ["ATH", -900_000, 97, "10000"], ["DOGE", -900_000, 96, "11100"], ["ZEC", -600_000, 95, "10000"]],
    [["BTC", 20_700_000, 100, "11111"], ["SOL", 11_300_000, 99, "11111"], ["ETH", 7_200_000, 99, "11111"], ["HYPE", 1_500_000, 98, "11111"], ["XRP", 900_000, 97, "11011"], ["PUMP", 600_000, 96, "11100"], ["ESPORTS", 500_000, 95, "11100"], ["ZEC", 300_000, 94, "10000"]],
    [["ONDO", -5_900_000, 100, "11111"], ["LINK", -1_600_000, 99, "11000"], ["BABA", -1_400_000, 99, "11000"], ["SKHYNIX", -1_100_000, 98, "11000"], ["ATH", -900_000, 97, "10000"], ["DOGE", -900_000, 96, "11100"], ["ZEC", -600_000, 95, "10000"], ["XEC", -300_000, 94, "10000"]],
  ),
  board(
    "spot_flow", "usd",
    [["XRP", 200_000, 100, "11000"], ["TRX", 100_000, 99, "11000"], ["PUMP", 100_000, 99, "11100"], ["ALICE", 100_000, 98, "10000"], ["SUI", 100_000, 97, "10000"], ["BNB", 40_000, 97, "11100"], ["DOGE", 30_000, 96, "10000"]],
    [["BTC", -300_000, 100, "11000"], ["LINK", -200_000, 99, "11000"], ["ETH", -200_000, 99, "10000"], ["BNB", -200_000, 98, "11100"], ["HYPE", -100_000, 97, "10000"], ["NEAR", -100_000, 97, "10000"], ["ONDO", -100_000, 96, "11000"]],
    [["XRP", 200_000, 100, "11000"], ["TRX", 100_000, 99, "11000"], ["PUMP", 100_000, 99, "11100"], ["ALICE", 100_000, 98, "10000"], ["SUI", 100_000, 97, "10000"], ["BNB", 40_000, 97, "11100"], ["DOGE", 30_000, 96, "10000"], ["ZEC", 20_000, 95, "10000"]],
    [["BTC", -300_000, 100, "11000"], ["LINK", -200_000, 99, "11000"], ["ETH", -200_000, 99, "10000"], ["BNB", -200_000, 98, "11100"], ["HYPE", -100_000, 97, "10000"], ["NEAR", -100_000, 97, "10000"], ["ONDO", -100_000, 96, "11000"], ["XEC", -50_000, 95, "10000"]],
  ),
];

const wideBoards = [
  board(
    "price", "percent",
    [["CASHCAT", 7.39, undefined, "11111"], ["ESPORTS", 6.42, undefined, "11000"], ["JCT", 2.60, undefined, "11000"], ["SKHYNIX", 2.14, undefined, "11100"], ["US", 2.01, undefined, "10000"], ["SYN", 1.58, undefined, "00000"], ["SKL", 1.57, undefined, "11000"]],
    [["PROM", -4.73, undefined, "11000"], ["BANK", -2.48, undefined, "11000"], ["TLM", -1.90, undefined, "11100"], ["JELLYJELLY", -1.89, undefined, "10000"], ["BABA", -1.58, undefined, "11000"], ["BLESS", -1.31, undefined, "10000"], ["1000XEC", -1.21, undefined, "10000"]],
    [["CASHCAT", 7.39, 100, "11111"], ["ESPORTS", 6.42, 99, "11000"], ["JCT", 2.60, 99, "11000"], ["SKL", 1.57, 98, "11000"], ["PUMP", 1.45, 97, "10000"], ["ONDO", 1.25, 97, "10000"], ["PROM", 1.26, 95, "11000"], ["US", 2.01, 95, "10000"]],
    [["BANK", -1.58, 100, "11000"], ["PROM", -4.73, 100, "11000"], ["JELLYJELLY", -1.89, 100, "10000"], ["ZEREBRO", -2.48, 99, "11000"], ["XVG", -0.87, 99, "10000"], ["XEC", -1.19, 99, "10000"], ["HYPE", -0.83, 96, "10000"], ["CRV", -0.61, 96, "10000"]],
  ),
  board(
    "oi", "usd",
    [["SKHYNIX", 9_600_000, 100, "11111"], ["MU", 4_300_000, 99, "11100"], ["SNDK", 3_800_000, 99, "11000"], ["PUMP", 3_100_000, 98, "11111"], ["ESPORTS", 2_400_000, 97, "11100"], ["XAUT", 1_300_000, 97, "11000"], ["AMD", 1_100_000, 96, "11000"]],
    [["ETH", -52_400_000, 100, "11100"], ["BTC", -33_500_000, 99, "11000"], ["SOL", -4_800_000, 99, "11000"], ["US", -3_600_000, 98, "11100"], ["XRP", -3_400_000, 98, "10000"], ["ZEC", -2_900_000, 97, "11111"], ["BANK", -2_200_000, 97, "11100"]],
    [["SKHYNIX", 9_600_000, 100, "11111"], ["ESPORTS", 2_400_000, 99, "11100"], ["ONDO", 205_000, 99, "11000"], ["PUMP", 3_100_000, 99, "11111"], ["ATH", 178_000, 98, "11000"], ["PROM", 109_000, 97, "11000"], ["VANRY", 98_000, 97, "10000"], ["SNDK", 136_000, 97, "11000"]],
    [["PROM", -370_000, 100, "11000"], ["YB", -265_000, 100, "10000"], ["BANK", -358_000, 100, "11000"], ["US", -3_600_000, 99, "11100"], ["BANK", -2_200_000, 100, "11100"], ["JELLYJELLY", -444_000, 99, "10000"], ["XEC", -337_000, 98, "10000"], ["KAITO", -315_000, 97, "10000"]],
  ),
  board(
    "futures_flow", "usd",
    [["BTC", 19_900_000, 100, "11111"], ["SOL", 10_400_000, 99, "11111"], ["SKHYNIX", 3_500_000, 99, "11100"], ["SNDK", 3_100_000, 98, "11000"], ["MU", 1_800_000, 98, "11100"], ["HYPE", 1_300_000, 97, "11111"], ["ESPORTS", 800_000, 97, "11100"]],
    [["ETH", -8_300_000, 100, "11100"], ["ONDO", -6_000_000, 100, "11111"], ["BABA", -1_200_000, 99, "11000"], ["ZEC", -1_200_000, 99, "11111"], ["DOGE", -500_000, 98, "11100"], ["XAUT", -500_000, 97, "11000"], ["AVAX", -400_000, 96, "10000"]],
    [["SKHYNIX", 3_500_000, 100, "11100"], ["ESPORTS", 800_000, 99, "11100"], ["BTC", 19_900_000, 99, "11111"], ["SOL", 10_400_000, 99, "11111"], ["MU", 1_800_000, 98, "11100"], ["HYPE", 1_300_000, 97, "11111"], ["SNDK", 3_100_000, 97, "11000"], ["ZEC", 600_000, 96, "11111"]],
    [["YB", -100_000, 100, "10000"], ["PROM", -400_000, 100, "11000"], ["ONDO", -6_000_000, 100, "11111"], ["ZEREBRO", -100_000, 100, "10000"], ["BANK", -100_000, 97, "11000"], ["JELLYJELLY", -100_000, 97, "10000"], ["XEC", -100_000, 96, "10000"], ["ATH", -100_000, 96, "11000"]],
  ),
  board(
    "spot_flow", "usd",
    [["TRX", 100_000, 100, "11100"], ["ESPORTS", 100_000, 99, "11100"], ["PEPE", 100_000, 99, "10000"], ["DOGE", 100_000, 98, "11000"], ["G", 40_000, 97, "10000"], ["PUMP", 30_000, 97, "11000"], ["OG", 20_000, 96, "10000"]],
    [["BTC", -4_100_000, 100, "11100"], ["ETH", -1_700_000, 99, "11100"], ["ZEC", -900_000, 99, "11000"], ["SOL", -200_000, 98, "11100"], ["WLD", -200_000, 97, "11000"], ["AERO", -100_000, 97, "10000"], ["LTC", -100_000, 96, "11000"]],
    [["ESPORTS", 100_000, 100, "11100"], ["G", 40_000, 99, "10000"], ["CASHCAT", 20_000, 99, "10000"], ["PUMP", 30_000, 98, "11000"], ["TRX", 100_000, 98, "11100"], ["DOGE", 100_000, 97, "11000"], ["OG", 20_000, 97, "10000"], ["PEPE", 10_000, 96, "10000"]],
    [["AERO", -100_000, 100, "10000"], ["XEC", -100_000, 100, "10000"], ["PROM", -100_000, 99, "11000"], ["LINK", -100_000, 99, "10000"], ["BANK", -100_000, 97, "11000"], ["BILL", -100_000, 97, "10000"], ["YGG", -100_000, 96, "10000"], ["ONDO", -100_000, 96, "11000"]],
  ),
];

type EventRanks = readonly [self: number, marketStrength: number, marketAbsolute: number];

function event(
  coin: string,
  label: string,
  direction: "long" | "short",
  observedAt: string,
  detail: string,
  index: number,
  ranks: EventRanks,
) {
  return {
    id: `${observedAt}:${coin}:${label}:${index}`,
    symbol: `${coin}USDT`,
    coin,
    observed_at: observedAt,
    window: "5m",
    event_type: label,
    label,
    detail,
    metric: "state",
    direction,
    value: null,
    rankings: {
      self: { available: true, rank: ranks[0], sample_size: 288, percentile: 96, method: "同币历史窗口" },
      market_strength: { available: true, rank: ranks[1], sample_size: 100, percentile: 95, method: "全场强度" },
      market_absolute: { available: true, rank: ranks[2], sample_size: 100, percentile: 94, method: "全场量级" },
    },
  };
}

const compactEvents = [
  event("ONDO", "Vol 爆发", "long", "2030-07-18T22:47:00Z", "5 分钟内 成交量 618万 (+1.6%)", 0, [9, 3, 6]),
  event("BANK", "OI 暴跌", "short", "2030-07-18T22:46:00Z", "15 分钟内 oi -516万 (-2.9%)", 1, [3, 4, 2]),
  event("BANK", "OI 暴涨", "long", "2030-07-18T22:38:00Z", "5 分钟内 oi +412万 (+2.3%)", 2, [9, 3, 2]),
  event("BANK", "OI 暴跌", "short", "2030-07-18T22:38:00Z", "15 分钟内 oi -622万 (-3.4%)", 3, [3, 2, 4]),
  event("JUP", "OI 暴跌", "short", "2030-07-18T22:32:00Z", "1 小时内 oi -478万 (-9.7%)", 4, [4, 19, 13]),
  event("JUP", "OI 暴跌", "short", "2030-07-18T22:32:00Z", "15 分钟内 oi -533万 (-10.8%)", 5, [3, 2, 1]),
  event("BANK", "OI 暴跌", "short", "2030-07-18T22:31:00Z", "5 分钟内 oi -898万 (-5.0%)", 6, [9, 3, 2]),
  event("FET", "OI 暴跌", "short", "2030-07-18T22:21:00Z", "5 分钟内 oi -228万 (-3.0%)", 7, [9, 7, 7]),
  event("BANK", "OI 暴涨", "long", "2030-07-18T22:19:00Z", "5 分钟内 oi +388万 (+2.1%)", 8, [9, 3, 2]),
  event("ONDO", "Vol 爆发", "long", "2030-07-18T22:17:00Z", "15 分钟内 成交量 522万 (+1.4%)", 9, [3, 4, 6]),
  event("BANK", "OI 暴跌", "short", "2030-07-18T22:12:00Z", "15 分钟内 oi -420万 (-2.3%)", 10, [3, 4, 2]),
  event("JUP", "OI 暴涨", "long", "2030-07-18T22:08:00Z", "5 分钟内 oi +70万 (+1.4%)", 11, [9, 5, 8]),
];

const wideEvents = compactEvents;

function realtime(boards: ReturnType<typeof board>[], events: ReturnType<typeof event>[], observedAt: string) {
  const allItems = boards.flatMap((entry) => [
    ...entry.amount_positive.items,
    ...entry.amount_negative.items,
    ...entry.strength_positive.items,
    ...entry.strength_negative.items,
  ]);
  const unique = new Map(allItems.map((item) => [item.symbol, item]));
  const items = [...unique.values()].map((item, index) => ({
    symbol: item.symbol,
    coin: item.coin,
    observed_at: observedAt,
    data_status: "ready",
    windows: { "5m": { available: true, coverage_ratio: 1, price_change_pct: item.unit === "percent" ? item.value : 0, cvd_usd: item.unit === "usd" ? item.value : 0 } },
    resonance: { available: true, direction: item.value >= 0 ? "long" : "short", active_count: Math.max(1, Math.min(5, Math.round(item.strength_percentile / 20))), window_count: 5, windows: [] },
    surge: { available: true, triggered: index < 5, direction: item.value >= 0 ? "long" : "short", score: 100 - index },
    ambush: { available: true, triggered: index >= 5 && index < 10, direction: item.value >= 0 ? "long" : "short", score: 95 - index },
    anomaly_24h: { count: Math.max(1, 30 - index), long_count: item.value >= 0 ? 20 : 6, short_count: item.value < 0 ? 20 : 6 },
  }));
  return { schema_version: "mercu-visual-v1", generated_at: observedAt, observed_at: observedAt, data_status: "ready", coverage: { symbols: items.length }, items, anomaly_events: events, boards: [] };
}

function overview(viewport: Viewport) {
  const wide = viewport === "1920x1080";
  const current = wide
    ? { advancing: 276, declining: 113, breadth_pct: 42, futures_net_flow_usd: 24_000_000, spot_net_flow_usd: -7_000_000, oi_net_change_usd: -89_000_000, futures_positive_ratio: .68, spot_positive_ratio: .12, oi_positive_ratio: .24 }
    : { advancing: 258, declining: 124, breadth_pct: 35, futures_net_flow_usd: 27_000_000, spot_net_flow_usd: -5_000_000, oi_net_change_usd: -72_000_000, futures_positive_ratio: .78, spot_positive_ratio: .22, oi_positive_ratio: .18 };
  const previous = wide
    ? { advancing: 265, declining: 126, breadth_pct: 36, futures_net_flow_usd: -44_000_000, spot_net_flow_usd: -8_000_000, oi_net_change_usd: -93_000_000 }
    : { advancing: 244, declining: 139, breadth_pct: 27, futures_net_flow_usd: -118_000_000, spot_net_flow_usd: -8_000_000, oi_net_change_usd: -80_000_000 };
  return {
    schema_version: "mercu-visual-v1",
    generated_at: wide ? "2030-07-18T22:59:00Z" : "2030-07-18T22:52:00Z",
    window_sec: 900,
    data_status: "ready",
    warnings: [],
    coverage: { assets: current.advancing + current.declining },
    overview: {
      ...current,
      bias: "inflow",
      comparison: {
        previous,
        delta: {
          breadth_pct: current.breadth_pct - previous.breadth_pct,
          futures_net_flow_usd: current.futures_net_flow_usd - previous.futures_net_flow_usd,
          spot_net_flow_usd: current.spot_net_flow_usd - previous.spot_net_flow_usd,
          oi_net_change_usd: current.oi_net_change_usd - previous.oi_net_change_usd,
        },
      },
    },
  };
}

function confluenceItem(
  coin: string,
  boardCount: number,
  direction: "positive" | "negative",
  divergent = false,
) {
  return { symbol: `${coin}USDT`, coin, board_count: boardCount, direction, divergent };
}

const compactConfluence = {
  amount: [
    confluenceItem("PUMP", 3, "positive"),
    confluenceItem("ONDO", 2, "negative"),
    confluenceItem("LINK", 2, "negative"),
    confluenceItem("NEAR", 2, "negative"),
    confluenceItem("XRP", 2, "positive", true),
    confluenceItem("HYPE", 2, "positive", true),
    confluenceItem("ZEC", 2, "negative"),
  ],
  strength: [
    confluenceItem("XVG", 3, "positive"),
    confluenceItem("ATH", 2, "negative", true),
    confluenceItem("YB", 2, "negative"),
    confluenceItem("ZEREBRO", 2, "negative"),
    confluenceItem("XEC", 2, "negative"),
    confluenceItem("BANK", 2, "negative"),
    confluenceItem("PUMP", 2, "positive"),
  ],
};

const wideConfluence = {
  amount: [
    confluenceItem("ESPORTS", 3, "positive"),
    confluenceItem("ZEC", 3, "negative"),
    confluenceItem("ETH", 3, "negative"),
    confluenceItem("SKHYNIX", 2, "positive"),
    confluenceItem("PUMP", 2, "positive"),
    confluenceItem("BTC", 2, "negative", true),
    confluenceItem("SOL", 2, "negative", true),
  ],
  strength: [
    confluenceItem("ESPORTS", 2, "positive"),
    confluenceItem("PROM", 3, "negative"),
    confluenceItem("SKHYNIX", 2, "positive"),
    confluenceItem("YB", 2, "negative"),
    confluenceItem("XEC", 2, "negative"),
    confluenceItem("PUMP", 2, "positive"),
  ],
};

export function mercuRadarFixture(viewport: Viewport) {
  const wide = viewport === "1920x1080";
  const boards = wide ? wideBoards : compactBoards;
  const observedAt = wide ? "2030-07-18T22:59:00Z" : "2030-07-18T22:52:00Z";
  return {
    overview: overview(viewport),
    boards: {
      schema_version: "mercu-visual-v1",
      generated_at: observedAt,
      window_sec: 900,
      data_status: "ready",
      warnings: [],
      coverage: { assets: 100 },
      methodology: { flow: "固定视觉回归数据；生产环境使用实时数据" },
      boards,
      confluence: wide ? wideConfluence : compactConfluence,
    },
    realtime: realtime(boards, wide ? wideEvents : compactEvents, observedAt),
  };
}
