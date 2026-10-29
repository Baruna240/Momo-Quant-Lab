# Research findings

These are honest results from the simulator, with no real money involved. The numbers below are snapshots from 28–30 October 2026 and change as the lab keeps running.

## 1. Meme coins (pump.fun): Strategy Lab
4,777 simulated positions across 13 variants, each with a $1 position size. Total PnL: **−$828**.

| Variant | Positions | PnL | Win rate |
|---|---:|---:|---:|
| Momentum + Anti-rug + Moonbag | 26 | −$2.90 | 23% |
| Momentum + Anti-rug | 73 | −$11.45 | 27% |
| Slow filter + Moonbag | 47 | −$16.15 | 17% |
| Auto (picks best variant) | 100 | −$20.70 | 16% |
| Slow filter 180s | 133 | −$28.53 | 23% |
| Momentum 60s | 178 | −$30.50 | 29% |
| Filter + Anti-rug | 180 | −$41.83 | 19% |
| Filter + high TP | 268 | −$60.53 | 15% |
| Filter + tight SL | 312 | −$65.13 | 18% |
| Filter 90s | 288 | −$67.28 | 20% |
| Fast filter 45s | 363 | −$82.03 | 16% |
| Buy every token | 889 | −$152.43 | 7% |
| Buy every token + cut loss | 1,920 | −$248.56 | 8% |

**Where the losses come from** (by exit reason):

| Exit | Positions | PnL |
|---|---:|---:|
| Stop loss | 1,618 | −$766 |
| "Dead coin" (no price movement) | 2,056 | −$231 |
| Trailing stop (after a rally) | 353 | **+$297** |

**Costs are the main finding.** The 1% platform fee per side, a network fee per transaction, and velocity-based slippage (averaging about 4.4% per side on entries the filters selected) add up to about **11% per round trip**. That is why 2,056 "dead" coins whose price did not move still lost money. Anti-rug checks and slower, stricter filters reduce losses, but none of them covers the cost hurdle.

**Base rates** from 2,466 randomly sampled tokens:
- 24.5% reach 2× their launch price at some point.
- Only **3–6%** are profitable if bought 30–180 seconds after launch and held for 20 minutes.
- Tokens that are already hot (≥20 SOL inflow, or 2–4× after launch) win more often (17–31%), but their median outcome is about 0.4–0.5×. The distribution is fat-tailed and mostly crashes.

## 2. Meme coins: machine-learning model
- **Data:** 2,532 tokens with full price paths and launch features.
- **Method:** every variant's exit rules are replayed over each token's recorded price path, including costs. A gradient-boosting classifier is trained on features known at entry time only. Validation is time-ordered: the model trains on the oldest 70% and is tested on the newest 30% (about 760 tokens).

| Entry at | AUC | Buy all | Top 20% by model | Top 5% by model |
|---|---:|---:|---:|---:|
| 60 s | 0.853 | 0.911× | 0.862× | 0.722× |
| 90 s | 0.885 | 0.933× | 0.917× | 0.957× |
| 180 s | 0.864 | 0.951× | 0.965× | 0.884× |

**Lesson: a high AUC does not mean profit.** The model is very good at ranking which tokens *will have a winning trade*: the win rate goes from about 5% to 25–39% in its top picks. But those same tokens also produce the biggest losers, so the average outcome stays below 1×. In effect it learned to detect **volatility, not direction**.

One weak signal was found. With the *Momentum + Anti-rug* exit rules, the model's top 5% averaged **1.06×** (39% win rate). That is only 38 tokens, which is too small a sample to trust. It will be re-tested as more data arrives before it is ever used as a live variant.

## 3. Altcoins and futures (Binance data, about 117–133 days of backtest)

| Strategy | Market | Backtest |
|---|---|---:|
| HODL BTC (benchmark) | spot | **+26.3%** |
| Swing 4h | spot | +1.1% |
| Breakout 48h | spot | −6.1% |
| RSI dip | spot | −9.7% |
| Trend EMA 1h | spot | −27.9% |
| Futures AI (walk-forward, 35 days) | perp 2× | −18.8% |
| Futures swing long/short | perp 3× | −25.7% |
| Futures short RSI | perp 2× | −29.0% |
| Futures short breakdown | perp 3× | −61.5% |

- No active strategy beat simply holding BTC.
- **Leverage turns a weak edge into a large loss.** The same swing logic was +1.1% on spot and −25.7% as a 3× long/short perpetual.
- The futures AI (18 features, 12-hour horizon) had validation accuracy slightly *below* the majority-class baseline, and its long and short hit rates flipped between training windows. It is therefore **gated**: it only trades a direction that passes an out-of-sample test (≥200 signals, >50% hit rate, mean > costs). At the moment no direction passes, so it does not trade.

## What this shows
1. Build the measurement first. A simulator with realistic costs and an honest baseline saves real money.
2. Make the validation match the deployment. Time-ordered splits, no look-ahead, and costs inside the label.
3. Watch the metric, not the hype. AUC, win rate and average return can disagree, and only the last one pays.

## Next steps
- Keep collecting data and re-test the 1.06× signal on a larger sample.
- Add microstructure features (unique buyers, buy/sell flow) if a free data source becomes available.
- Move the lab to a VPS for 24/7 data collection.
