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
by a close back inside is classified as a long-wick rejection when wick/body is
at least `LAUNCH_PA_WICK_BODY_RATIO`; a close back inside without the required
wick is a failed breakout.

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

## Asset classification labels

Launch candidates carry a display-only Binance instrument classification. The
classifier separates crypto perpetuals from reviewed TradFi perpetuals,
including individual equities, ETF/index products, leveraged ETFs, precious
metals, energy, industrial metals, and forex metadata. Crypto contracts retain
their Binance theme metadata and are labelled as core, major, or altcoin by a
small reviewed tier list. Unknown future metadata falls back safely without
changing the launch score, thresholds, lifecycle, or 15m trigger.

The category and its source are included in the launch record. Telegram and
the PNG both show compact Chinese labels. The chart bundles only the reviewed
glyph subset it needs, so rendering remains deterministic and does not depend
on Pillow or operating-system fonts.

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
price-action V3 event starts, the image uses fully closed Binance 1h candles as
its main view. It retains the frozen 15m consolidation area and key level, a
Chinese current-status label, and numbered lifecycle events, so the earlier
15m trigger remains visible without filling the chart with all 15m history.
Long-wick rejections and failed breakouts are described by that status label.
After the first package is sent, later packages for the same symbol reply to
the previous successful package. Successful launch-topic history is retained;
the bot does not schedule old packages for deletion. If an operator manually
removes the reply target, Telegram delivery falls back once to a standalone
message and that new message becomes the next reply head. P2.4 stores one outcome per completed
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

One lifecycle is one sample, regardless of how many Telegram reply-chain
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
