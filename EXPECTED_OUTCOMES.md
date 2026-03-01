# Expected Outcomes — Paper Trading v3 (HTF filter)

Backtest date: 2026-02-28
Deploy date: __________ (fill in when deployed)
Review date: __________ (fill in after 6-8 weeks)

## Settings Deployed

| Setting | Value |
|---|---|
| FAST_EMA | 9 |
| SLOW_EMA | 21 |
| ATR_MIN_PCT | 0.25 |
| STOP_ATR_MULTIPLIER | 1.8 |
| TAKE_PROFIT_R | 2.0 |
| COOLDOWN_CANDLES | 24 |
| **HTF_TIMEFRAME** | **30m** |
| VOLUME_MIN_MULT | 0 (disabled) |
| ENTRY_FEE_BPS | 2 (limit order / maker) |
| EXIT_FEE_BPS | 5 (market order / taker) |
| SLIPPAGE_BPS | 2 |
| MAX_CONSECUTIVE_LOSSES | 3 |
| MAX_DAILY_LOSS_PCT | 2.0 |
| RISK_PER_TRADE_PCT | 0.5 |
| TIMEFRAME | 5m |
| POLL_SECONDS | 20 |

## Backtest Results (45 days, Jan 15 — Mar 1, 2026)

| Metric | v2 (no HTF) | v3 (HTF 30m) |
|---|---|---|
| Net P&L | -$89 (-0.9%) | **+$310 (+3.1%)** |
| Profit factor | 0.53 | **1.65** |
| Max drawdown | $180 (1.8%) | $180 (1.8%) |
| Total trades | 4 | **18** |
| Win rate | 25% (1/4) | **50% (9/18)** |
| Avg win | $93 | $96 |
| Avg loss | -$58 | -$58 |
| Win/Loss ratio | 1.60 | **1.66** |
| Stop exits | 3 (75%) | 9 (50%) |
| TP exits | 1 (25%) | **9 (50%)** |
| Max consec losses | 3 | 3 |
| Avg trade P&L | -$22.25 | **+$17.23** |
| Total fees | $21 | $78 |
| Trades per day | ~0.1 | **~0.40** |

### Why 30m HTF was chosen over 1h

| HTF timeframe | Trades | Win rate | P&L | PF |
|---|---|---|---|---|
| None (off) | 4 | 25% | -$89 | 0.53 |
| 1h | 16 | 50% | +$279 | 1.66 |
| **30m** | **18** | **50%** | **+$310** | **1.65** |

30m is slightly more responsive — picks up 2 extra aligned entries while
maintaining the same win rate and profit factor. Best overall P&L.

### Why volume filter was rejected

| Volume threshold | Trades | Win rate | P&L | PF |
|---|---|---|---|---|
| None (off) | 16 | 50% | +$279 | 1.66 |
| >= 0.8x avg | 13 | 39% | -$9 | 1.03 |
| >= 1.2x avg | 12 | 42% | +$51 | 1.18 |

Volume filtering removed more winners than losers — counterproductive.

### ATR threshold grid (all with HTF 1h, CD 24)

| ATR_MIN_PCT | Trades | Win rate | P&L | PF |
|---|---|---|---|---|
| 0.15 | 4 | 25% | -$109 | 0.48 |
| 0.20 | 7 | 29% | -$123 | 0.63 |
| **0.25** | **16** | **50%** | **+$279** | **1.66** |

Lower ATR thresholds let in choppy-market entries that lose quickly,
trigger the 3-loss consecutive limit, and shut down trading for the day.

## What to Expect Live (per week, ~2-3 trades)

| Metric | Expected Range | Red Flag |
|---|---|---|
| Win rate | 40-60% | Below 30% for 3+ weeks |
| Profit factor | 1.20 - 2.50 | Below 0.90 for 3+ weeks |
| Avg win / Avg loss | 1.4 - 2.0 | Below 1.2 |
| Stop exits % | 40-60% | Above 75% |
| Trades per week | 2-4 | 0 trades for 2 weeks |
| Weekly P&L | -$60 to +$80 | Loss > $200 in a single week |
| Max drawdown | up to $200 | Exceeds $400 |

## How to Compare

1. Pull logs after each week:
   ```
   scp user@server:~/crypto-trade-bot/logs/*.jsonl ./logs/
   ```

2. Count wins/losses:
   ```
   python -c "
   import json
   trades = [json.loads(l) for l in open('logs/trades.jsonl') if l.strip()]
   wins = [t for t in trades if t['pnl'] > 0]
   losses = [t for t in trades if t['pnl'] <= 0]
   total_pnl = sum(t['pnl'] for t in trades)
   total_fees = sum(t['fees'] for t in trades)
   print(f'Trades: {len(trades)}')
   print(f'Win rate: {len(wins)}/{len(trades)} ({len(wins)/max(len(trades),1)*100:.1f}%)')
   print(f'Avg win: {sum(t[\"pnl\"] for t in wins)/max(len(wins),1):.2f}')
   print(f'Avg loss: {sum(t[\"pnl\"] for t in losses)/max(len(losses),1):.2f}')
   print(f'Net P&L: {total_pnl:.2f}')
   print(f'Total fees: {total_fees:.2f}')
   print(f'PF: {sum(t[\"pnl\"] for t in wins)/abs(sum(t[\"pnl\"] for t in losses)):.2f}')
   "
   ```

3. Check for limit order fills vs cancels in events log:
   ```
   python -c "
   import json
   events = [json.loads(l) for l in open('logs/events.jsonl') if l.strip()]
   fills = [e for e in events if e['event'] == 'position_open']
   cancels = [e for e in events if e['event'] == 'limit_order_cancelled']
   print(f'Fills: {len(fills)}, Cancels: {len(cancels)}')
   print(f'Fill rate: {len(fills)/max(len(fills)+len(cancels),1)*100:.1f}%')
   "
   ```

## Decision Gate (after 3-4 weeks)

| Result | Action |
|---|---|
| PF > 1.30, win rate > 40% | Strategy outperforming expectations — continue |
| PF 1.00-1.30, win rate > 35% | On track — continue |
| PF 0.80-1.00, win rate > 30% | Marginal — extend paper run 2 more weeks |
| PF < 0.80 or win rate < 30% | Strategy underperforming — stop and re-evaluate |
| Max drawdown > $400 | Risk controls may need tightening — stop and review |
| Fill rate < 50% | Limit order timeout too short — increase LIMIT_TIMEOUT_SECONDS |

## Previous Runs (for reference)

### v1 — original (Feb 22 — Mar 1, 2026, 7.5 days live)
- Net P&L: -$1,451.28 (-14.5%)
- Win rate: 32.7% (16/49)
- Profit factor: ~0.48
- Issues: no cooldown, immediate re-entry after stops, ATR filter too low,
  EMA 20/50 too slow, TP too far, fees overcharged

### v2 — bug fixes + limit orders, no HTF (backtest)
- Net P&L: -$89 (-0.9%)
- Win rate: 25% (1/4)
- Profit factor: 0.53
- Improvement: dramatically fewer trades (cooldown working), but still net negative
