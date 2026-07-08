export type ApiEnvelope<T> = {
  ok?: boolean;
  data?: T;
  items?: unknown[];
  message?: string;
  error?: unknown;
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
  decision?: DecisionBlock;
  scores?: Record<string, number>;
  reasons?: string[];
  risks?: string[];
  watch_points?: string[];
  factor_explanations?: Array<{ factor?: string; label?: string; score?: number; explanation?: string }>;
  calibration?: Record<string, unknown>;
};

export type OutcomeItem = {
  symbol?: string;
  horizon?: string;
  signal_time?: string;
  module?: string;
  decision_label?: string;
  result_label?: string;
  result_tone?: Tone;
  data_status?: string;
  final_return_pct?: number | null;
  max_gain_pct?: number | null;
  max_drawdown_pct?: number | null;
};

export type BacktestGroup = {
  key?: string;
  label?: string;
  total_count?: number;
  success_count?: number;
  coverage_ratio?: number;
  avg_final_return_pct?: number | null;
  avg_max_gain_pct?: number | null;
  avg_max_drawdown_pct?: number | null;
  positive_ratio?: number;
  drawdown_ratio?: number;
  expectancy_score?: number | null;
  sample_quality?: string;
};

export type BacktestPayload = {
  summary?: BacktestGroup & { headline?: string };
  decision_groups?: BacktestGroup[];
  model_diagnosis?: {
    overall_label?: string;
    overall_summary?: string;
    strengths?: string[];
    weaknesses?: string[];
    calibration_hints?: string[];
    data_warnings?: string[];
  };
  coverage?: Record<string, number>;
};

export type BacktestMatrixPayload = {
  items?: Array<{
    decision_code?: string;
    decision_label?: string;
    horizons?: Record<string, BacktestGroup>;
  }>;
  horizons?: string[];
};
