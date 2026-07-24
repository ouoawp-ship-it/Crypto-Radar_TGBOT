# Launch lifecycle V2

P2.1 adds a durable, Binance-native observation layer underneath the existing
launch alert. It is intentionally a shadow feature in this phase: it records
and validates lifecycle facts without changing Telegram message formatting or
delivery behavior.

## Data contract

The feature stores two tables in the existing `signals.db`:

- `launch_lifecycle_cycles`: one row per symbol lifecycle, including cycle
  number, current/peak stage, start/end windows, failure reason, consecutive
  invalid-window count, and confirmed breakout price.
- `launch_lifecycle_observations`: one idempotent row per closed 15-minute
  window, including absolute closed price, absolute Binance OI value, quote
  volume, score inputs, funding rate/interval, 8-hour-normalized funding, data
  confirmation, and exact deltas from both the first and previous observation.

The unique `(cycle_id, window_end_ts)` constraint prevents repeated scans of the
same closed candle from advancing the lifecycle twice.

## Lifecycle rules

1. A cycle opens only when the score is at least `LAUNCH_MIN_SCORE_PUSH`
   (default `60`) and the Binance confirmation gate is complete.
2. Scores at or above `LAUNCH_WATCH_SCORE` (default `45`) keep the cycle active.
3. A score below `45` marks the cycle as cooling. Two consecutive valid closed
   windows below `45` end the cycle.
4. Once a real breakout has been confirmed, two consecutive closes below that
   breakout price also end the cycle.
5. Missing, stale, invalid, or confirmation-blocked data freezes the lifecycle;
   it never counts as a failed window.
6. Repeated scans of a failed window cannot create a new cycle. A later valid
   closed window with a new score of at least `60` starts cycle `N+1`.

Active lifecycle symbols are scanned ahead of new high-volume candidates. This
keeps an already-open signal under observation even if its 24-hour volume later
falls below the normal discovery threshold. The existing request budgets remain
hard limits.

## Closed-candle price-action follow-up

`LAUNCH_PRICE_ACTION_V3_ENABLE=true` adds a price-action state to each lifecycle
observation. It does not start a second scanner. New candidates retain the
existing 17-bar request; already-active lifecycle symbols request enough 15m
history to build a closed 1h box without consuming another request slot.

The detector freezes the original structure level when a valid 15m breakout is
found. A valid breakout requires:

- the preceding range to fit within `LAUNCH_PA_MAX_BOX_RANGE_PCT`;
- the 15m close to finish outside that range;
- candle direction to agree with the breakout; and
- body/range to be at least `LAUNCH_PA_MIN_BODY_RATIO`.

It then evaluates only completed higher-timeframe candles against that frozen
level. The current chain is `15m -> 1h -> 4h`. A wick through the level followed
by a close back inside is classified as a liquidity sweep when wick/body is at
least `LAUNCH_PA_WICK_BODY_RATIO`; a close back inside without the required wick
is a failed breakout, not a sweep.

Price-action state changes are lifecycle package checkpoints, so a confirmed
breakout or false breakout updates the existing symbol package instead of
creating a separate Telegram stream. Repeated scans of the same 15m window stay
idempotent through the existing `(cycle_id, window_end_ts)` constraint.

Safe rollout defaults:

```dotenv
LAUNCH_PRICE_ACTION_V3_ENABLE=false
LAUNCH_PA_BOX_LOOKBACK=16
LAUNCH_PA_MAX_BOX_RANGE_PCT=12
LAUNCH_PA_MIN_BODY_RATIO=0.45
LAUNCH_PA_WICK_BODY_RATIO=1.5
```

Lifecycle V2 is required for durable monitoring. Without message-package V2 the
detector runs in shadow mode and records state without sending structure-only
Telegram updates.

The implementation is original to this repository. Its transparent rule design
was cross-checked against the MIT-licensed
[`stockalgo/stolgo`](https://github.com/stockalgo/stolgo),
[`coding-kitties/PyIndicators`](https://github.com/coding-kitties/PyIndicators),
[`joshyattridge/smart-money-concepts`](https://github.com/joshyattridge/smart-money-concepts),
and [`xgboosted/pandas-ta-classic`](https://github.com/xgboosted/pandas-ta-classic)
projects; no external runtime dependency or copied source file is introduced.

## Full closed-candle SMC V4

`LAUNCH_SMC_V4_ENABLE=true` extends the price-action state with a deterministic
SMC layer for active lifecycle symbols. It is separately gated and defaults to
disabled. It reuses the symbol's existing Binance 15m kline request; no second
scanner, service, database, or Telegram stream is created.

The implementation covers:

- confirmed `HH`, `HL`, `LH`, and `LL` swing structure;
- close-confirmed `BOS`, `CHoCH`, and displacement-qualified `MSS`;
- equal-high/equal-low buy-side and sell-side liquidity pools and wick sweeps;
- three-candle fair value gaps with mitigation/fill state;
- order blocks derived from a confirmed structure break;
- breaker blocks after an order block is invalidated by a close;
- mitigation events on the first valid OB or FVG revisit;
- current dealing-range premium, equilibrium, and discount;
- 4h/1h higher-timeframe bias and 15m execution alignment; and
- the persistent sequence `sweep -> CHoCH/MSS -> displacement/FVG ->
  OB/FVG retest -> BOS`.

Anti-repaint rules are part of the data contract:

- the Binance 15m parser accepts only candles closed before the requested
  boundary;
- a swing is not published until `LAUNCH_SMC_SWING_LENGTH` closed candles exist
  on its right side;
- BOS, CHoCH, MSS, order-block invalidation, and breaker creation require a
  candle close, not an intrabar wick;
- 1h and 4h candles are aggregated only when every constituent 15m candle is
  present and the higher-timeframe candle is fully closed; and
- event keys are persisted in the lifecycle price-action JSON, so rescanning
  the same closed window is idempotent.

The in-memory PNG uses the same state snapshot. It draws swing labels,
structure-break lines, BSL/SSL and sweep markers, FVG/OB/Breaker/Mitigation
zones, and premium/discount equilibrium on the existing lifecycle chart.

Safe defaults:

```dotenv
LAUNCH_SMC_V4_ENABLE=false
LAUNCH_SMC_HISTORY_BARS=400
LAUNCH_SMC_SWING_LENGTH=2
LAUNCH_SMC_EQUAL_TOLERANCE_ATR=0.15
LAUNCH_SMC_DISPLACEMENT_BODY_ATR=1.0
LAUNCH_SMC_MAX_ZONE_AGE_BARS=96
```

The rule definitions were cross-checked against the MIT-licensed
[`joshyattridge/smart-money-concepts`](https://github.com/joshyattridge/smart-money-concepts)
indicator contract. The bot implementation is original, dependency-free, and
uses stricter closed-candle confirmation for lifecycle monitoring.

## Rollout

The feature defaults to disabled:

```dotenv
LAUNCH_LIFECYCLE_V2_ENABLE=false
LAUNCH_LIFECYCLE_INVALID_WINDOWS=2
```

Enable it first in production shadow mode by setting
`LAUNCH_LIFECYCLE_V2_ENABLE=true`. The launch diagnostics then report:

- lifecycle mode (`shadow` or `degraded`);
- active and forced-monitor symbol counts;
- newly opened, recorded, failed, frozen, and error counts.

P2.2 consumes this stored contract to build and atomically replace the latest
Telegram package. P2.3 adds the in-memory K-line image. The production package
is one photo message whose caption contains the dynamic lifecycle text, links,
and copyable symbol. Static chart/data/lifecycle guidance lives in the pinned
launch-topic introduction so each symbol does not repeat boilerplate. After a
price-action V3 event starts, that same image includes the frozen 15m
consolidation box and structure level, plus `15M BO`, `1H OK`, and `4H OK`
close-confirmation markers as they occur. A long-wick re-entry is marked
`SWEEP H` or `SWEEP L`; a body-close invalidation is marked `FAIL`.
After a new package is sent and committed, the bot deletes older launch-topic signal
messages and keeps only the pinned introduction plus the latest package. Failed
deletions remain discoverable in Telegram delivery history and are retried
while they remain inside Telegram's deletion window. Older records are marked
undeletable and no longer retried. P2.4 stores one outcome per completed
lifecycle, reports close-based favorable/adverse movement and stage timing, and
keeps historical rates hidden until enough completed cycles exist under the
same rule key.

## P2.4 outcome contract

Enable the evaluator only together with lifecycle V2:

```dotenv
LAUNCH_OUTCOME_V2_ENABLE=true
LAUNCH_OUTCOME_FOLLOW_THROUGH_PCT=3.0
LAUNCH_OUTCOME_MIN_SAMPLES=20
```

One lifecycle is one sample, regardless of how many Telegram replacement
packages it publishes. Old `launch-package:*` deliveries are removed from the
generic event-level outcome table so one cycle cannot be counted multiple
times.

The evaluator persists:

- first and last price;
- highest and lowest observed 15-minute close relative to the first close;
- highest and lowest observed OI relative to the first OI;
- final return at lifecycle invalidation;
- peak score and peak stage;
- time to `breakout` and `launched`;
- whether the highest observed close reached the configured follow-through
  threshold.

These are descriptive lifecycle measurements, not a trade PnL or a promise of
profit. Intrabar highs and lows are deliberately excluded because the lifecycle
contract only admits closed 15-minute observations.

Each cycle freezes its rule key when it opens. A later threshold change starts
a new cohort and does not relabel historical cycles. Before the current cohort
has `LAUNCH_OUTCOME_MIN_SAMPLES` completed cycles, messages show raw counts only;
rates and medians remain hidden. No result automatically changes production
thresholds.
