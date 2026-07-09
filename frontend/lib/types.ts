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
  subtitle?: string;
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

export type SignalItem = {
  id?: number;
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
  decision?: DecisionBlock;
};

export type DecisionBlock = {
  label?: string;
  code?: string;
  tone?: string;
  confidence?: number;
  risk_level?: string;
  summary?: string;
  not_advice?: string;
};

export type DecisionItem = {
  symbol?: string;
  coin?: string;
  model_version?: string;
  decision?: DecisionBlock;
  scores?: Record<string, number>;
  reasons?: string[];
  risks?: string[];
  watch_points?: string[];
  factor_explanations?: Array<{ factor?: string; label?: string; score?: number; explanation?: string }>;
  calibration?: Record<string, unknown>;
  related_signals?: SignalItem[];
};

export type OutcomeItem = {
  symbol?: string;
  horizon?: string;
  signal_time?: string;
  module?: string;
  signal_type?: string;
  decision_label?: string;
  decision_code?: string;
  decision_confidence?: number | null;
  risk_level?: string;
  result_label?: string;
  result_tone?: Tone;
  data_status?: string;
  final_return_pct?: number | null;
  max_gain_pct?: number | null;
  max_drawdown_pct?: number | null;
  entry_price?: number | null;
  future_price?: number | null;
};

export type LifecycleItem = {
  id?: number;
  symbol?: string;
  coin?: string;
  current_state?: string;
  state_label?: string;
  first_signal_id?: number | null;
  first_signal_at?: string;
  first_signal_module?: string;
  first_signal_type?: string;
  first_signal_level?: string;
  highest_level?: string;
  lifecycle_score?: number | null;
  risk_score?: number | null;
  latest_signal_id?: number | null;
  latest_signal_at?: string;
  latest_price?: number | null;
  latest_oi?: number | null;
  latest_funding_rate?: number | null;
  price_change_from_first_pct?: number | null;
  oi_change_from_first_pct?: number | null;
  futures_cvd_status?: string;
  spot_cvd_status?: string;
  funding_status?: string;
  exchange_context?: Record<string, unknown>;
  metrics?: Record<string, unknown>;
  reasons?: string[];
  not_advice?: string;
};

export type LifecycleEvent = {
  id?: number;
  symbol?: string;
  event_time?: string;
  event_type?: string;
  event_label?: string;
  event_level?: string;
  state_label?: string;
  previous_state?: string;
  new_state?: string;
  price_change_from_first_pct?: number | null;
  oi_change_pct?: number | null;
  futures_cvd_delta?: number | null;
  spot_cvd_delta?: number | null;
  funding_rate?: number | null;
  event_score?: number | null;
  risk_score?: number | null;
  reasons?: string[];
};

export type LifecycleSummaryPayload = {
  summary?: Record<string, unknown>;
  items?: LifecycleItem[];
  not_advice?: string;
};

export type LifecycleDetailPayload = {
  symbol?: string;
  lifecycle?: LifecycleItem;
  events?: LifecycleEvent[];
  metrics?: Array<Record<string, unknown>>;
  not_advice?: string;
};

export type BacktestGroup = {
  key?: string;
  label?: string;
  decision_code?: string;
  decision_label?: string;
  total_count?: number;
  success_count?: number;
  pending_count?: number;
  unavailable_count?: number;
  error_count?: number;
  coverage_ratio?: number;
  avg_final_return_pct?: number | null;
  median_final_return_pct?: number | null;
  avg_max_gain_pct?: number | null;
  avg_max_drawdown_pct?: number | null;
  positive_ratio?: number;
  strong_ratio?: number;
  drawdown_ratio?: number;
  avg_gain_drawdown_ratio?: number | null;
  expectancy_score?: number | null;
  sample_quality?: string;
  diagnosis?: string;
};

export type BacktestPayload = {
  summary?: BacktestGroup & { headline?: string };
  decision_groups?: BacktestGroup[];
  module_groups?: BacktestGroup[];
  risk_groups?: BacktestGroup[];
  confidence_groups?: BacktestGroup[];
  model_diagnosis?: {
    overall_label?: string;
    overall_summary?: string;
    strengths?: string[];
    weaknesses?: string[];
    calibration_hints?: string[];
    data_warnings?: string[];
  };
  coverage?: Record<string, number>;
  filters?: Record<string, unknown>;
};

export type BacktestMatrixPayload = {
  items?: Array<{
    decision_code?: string;
    decision_label?: string;
    horizons?: Record<string, BacktestGroup>;
  }>;
  horizons?: string[];
};

export type CoinItem = {
  coin?: string;
  symbol?: string;
  count?: number;
  latest_at?: string;
  module_count?: number;
  failed_count?: number;
  label?: string;
  subtitle?: string;
};

export type ListPayload<T> = {
  items?: T[];
  count?: number;
  next_cursor?: string | number | null;
  summary?: Record<string, unknown>;
  filters?: Record<string, unknown>;
  pagination?: Record<string, unknown>;
};
