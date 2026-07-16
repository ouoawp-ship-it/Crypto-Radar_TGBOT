export type ApiEnvelope<T> = {
  ok?: boolean;
  data?: T;
  items?: unknown[];
  message?: string;
  error?: unknown;
  _meta?: Record<string, unknown>;
};

export type ApiResult<T> = {
  ok: boolean;
  data?: T;
  error?: string;
  status?: number;
  path?: string;
};

export type Tone = "good" | "warn" | "bad" | "info" | "neutral";

export type DisplayInfo = {
  title?: string;
  module_label?: string;
  status_label?: string;
  symbol_label?: string;
  time_label?: string;
  score_label?: string;
  stage_label?: string;
  summary?: string;
  card_tone?: Tone;
  badges?: Array<{ label?: string; tone?: Tone }>;
};

export type SignalRank = {
  available?: boolean;
  label?: string;
  value?: number;
  rank?: number;
  sample_size?: number;
  percentile?: number;
  method?: string;
  reason?: string;
  metric?: { key?: string; label?: string; unit?: string; value?: number; quality?: string };
};

export type SignalResonance = {
  label?: string;
  active_count?: number;
  window_count?: number;
  available?: boolean;
  method?: string;
  windows?: Array<{ key?: string; seconds?: number; active?: boolean; module_count?: number; signal_count?: number; modules?: string[] }>;
};

export type SignalLifecycle = {
  state?: string;
  label?: string;
  derived?: boolean;
  observed_at?: string;
  age_sec?: number;
  basis?: string;
  previous_signal_id?: number | null;
};

export type SignalIntelligence = {
  self_rank?: SignalRank;
  market_strength_rank?: SignalRank;
  market_absolute_rank?: SignalRank;
  resonance?: SignalResonance;
  lifecycle?: SignalLifecycle;
};

export type SignalItem = {
  id?: number;
  public_ref?: string;
  time?: string;
  symbol?: string;
  coin?: string;
  module?: string;
  status?: string;
  signal_type?: string;
  score?: number | string | null;
  stage?: string;
  excerpt?: string;
  display?: DisplayInfo;
  intelligence?: SignalIntelligence;
};

export type ListPayload<T> = {
  items?: T[];
  count?: number;
  next_cursor?: number | null;
  filters?: Record<string, unknown>;
};

export type MetricStatus = "fresh" | "stale" | "degraded" | "unavailable";

export type MarketMetric = {
  value?: number | null;
  unit?: "usd" | "percent" | "percent_per_cycle" | "ratio" | string;
  source?: string;
  observed_at?: string;
  age_sec?: number;
  status?: MetricStatus;
  quality?: "direct" | "derived" | "missing" | string;
};

export type FundingExchange = {
  exchange?: string;
  funding_pct?: number | null;
  interval_hours?: number;
  last_funding_time?: string;
  next_funding_time?: string;
  extreme_label?: string;
};

export type MarketSnapshot = {
  schema_version?: string;
  symbol?: string;
  coin?: string;
  status?: MetricStatus;
  updated_at?: string;
  age_sec?: number;
  metrics?: Record<string, MarketMetric>;
  funding_exchanges?: FundingExchange[];
  tiers?: { market_cap?: string; liquidity?: string };
};

export type SignalEvidence = {
  key?: string;
  label?: string;
  description?: string;
  metric?: MarketMetric;
  value?: string;
  tone?: string;
};

export type SignalContext = {
  schema_version?: string;
  signal?: SignalItem;
  market?: MarketSnapshot | null;
  market_error?: string;
  evidence?: SignalEvidence[];
  lifecycle?: SignalLifecycle & { started_at?: string; duration_sec?: number };
  rankings?: { self?: SignalRank; market_strength?: SignalRank; market_absolute?: SignalRank };
  resonance?: SignalResonance;
  related?: { same_symbol?: SignalItem[] };
  actions?: { signal_url?: string; symbol_url?: string; ai_url?: string; alert_url?: string };
};

export type IntelligenceEntry = { signal?: SignalItem; intelligence?: SignalIntelligence };

export type OpportunityBoard = {
  key?: "launch" | "resonance" | "funding" | "risk" | string;
  title?: string;
  description?: string;
  count?: number;
  items?: IntelligenceEntry[];
};

export type RadarIntelligence = {
  schema_version?: string;
  generated_at?: string;
  window_sec?: number;
  data_status?: "ready" | "empty" | string;
  methodology?: Record<string, string>;
  summary?: { signals?: number; symbols?: number; resonance_symbols?: number; enhancing_symbols?: number };
  projection?: { requested?: number; returned?: number; max_items?: number };
  items?: IntelligenceEntry[];
  boards?: OpportunityBoard[];
};

export type CoinContext = {
  schema_version?: string;
  symbol?: string;
  coin?: string;
  market?: MarketSnapshot | null;
  market_error?: string;
  summary?: { signal_count?: number; sent_count?: number; module_counts?: Record<string, number>; latest_at?: string };
  timeline?: SignalItem[];
  actions?: { radar_url?: string; ai_url?: string; alert_url?: string };
};

export type WatchlistMarketItem = {
  symbol?: string;
  ok?: boolean;
  market?: MarketSnapshot | null;
  error?: string;
  coin_url?: string;
};

export type WatchlistMarketPayload = { items?: WatchlistMarketItem[]; count?: number; invalid?: string[] };
