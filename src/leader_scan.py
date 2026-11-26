"""Score Hyperliquid traders as copy-trading candidates.

Research only: this module reads public data and never signs or places an order.

Pipeline
--------
1. **Leaderboard prefilter** (one cached 40 MB download). Drop vaults, losers,
   tiny accounts and anything whose monthly volume/equity turnover looks like a
   market maker or scalper.
2. **Deep scan** of the best ``--deep`` survivors, one address at a time and
   rate-limited to stay under Hyperliquid's info-API weight budget:

   - ``clearinghouseState`` + spot stablecoins: the address's *own* equity. The
     leaderboard rolls sub-accounts up into the master, so a master whose
     sub-accounts hold more than it does is flagged, not copied.
   - ``portfolio``: perp P&L history -> drawdown, profitable months, track-record age.
   - ``userFillsByTime``: fills over ``--days`` -> round trips, win rate, profit
     factor, hold time, maker share.

3. **Copy cost.** Every fill is charged ``--copy-cost-bps`` on its notional, the
   slippage a follower pays for arriving seconds late. Win rate, profit factor and
   ROI are all computed *after* this cost, so scalpers whose edge is smaller than
   the delay score as losers, which for a copier they are.
4. **Score** (0-100, see ``score``) and write ``data/leader_scan.{json,md}``.

Usage::

    python -m src.leader_scan                 # default: deep-scan 40, show top 10
    python -m src.leader_scan --deep 80 --days 90 --refresh
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
VAULTS_URL = "https://stats-data.hyperliquid.xyz/Mainnet/vaults"
EXPLORER_URL = "https://app.hyperliquid.xyz/explorer/address/"

DAY_MS = 86_400_000
STABLES = {"USDC", "USDH", "USDT0", "USDE"}  # spot balances counted as collateral
FILLS_PAGE = 2000  # Hyperliquid caps one userFillsByTime response at 2000 fills

# Rejection reasons, in the order they are checked. Used as report keys.
R_SUBACCOUNTS = "trades via sub-accounts"
R_EMPTY = "own equity below --min-equity (withdrawn)"
R_TOO_ACTIVE ="more fills than the cap (too active to copy)"
R_FEW_TRADES = "too few round trips"
R_SHORT_HOLD = "holds too short to copy"
R_MAKER = "mostly maker fills (market maker)"
R_NEGATIVE = "unprofitable after copy cost"
R_ERROR = "fetch error"


# --------------------------------------------------------------------------- data


@dataclass
class Candidate:
    address: str
    lb_equity: float
    month_pnl: float
    month_roi: float
    month_volume: float
    all_time_pnl: float
    all_time_roi: float
    display_name: Optional[str] = None

    @property
    def turnover(self) -> float:
        return self.month_volume / self.lb_equity if self.lb_equity > 0 else math.inf


@dataclass
class RoundTrip:
    """One position on one coin, from flat (or first seen) back to flat."""

    coin: str
    side: str  # "long" | "short"
    open_ms: int
    close_ms: Optional[int] = None
    closed_pnl: float = 0.0
    fees: float = 0.0
    notional: float = 0.0
    max_size: float = 0.0
    open_observed: bool = True  # False if the window starts mid-position

    def net(self, cost_bps: float = 0.0) -> float:
        return self.closed_pnl - self.fees - self.notional * cost_bps / 10_000

    @property
    def hold_hours(self) -> Optional[float]:
        if self.close_ms is None or not self.open_observed:
            return None
        return (self.close_ms - self.open_ms) / 3_600_000


@dataclass
class FillStats:
    fills: int = 0
    round_trips: int = 0
    realized_pnl: float = 0.0  # closedPnl - fees, every perp fill in the window
    copy_cost: float = 0.0
    notional: float = 0.0
    win_rate: float = 0.0  # after copy cost, completed round trips
    profit_factor: float = 0.0  # after copy cost; inf when there are no losers
    median_hold_hours: Optional[float] = None
    trades_per_day: float = 0.0
    maker_ratio: float = 0.0  # share of fill notional that rested on the book
    hip3_share: float = 0.0  # share of notional on builder-deployed perps ("dex:COIN")
    long_share: float = 0.0  # share of round trips that were long
    pnl_without_top3: float = 0.0  # after copy cost
    top3_share: float = 0.0  # best 3 round trips / total, after copy cost
    top_coins: list[str] = field(default_factory=list)
    first_fill_ms: Optional[int] = None
    truncated: bool = False  # hit the max-fills cap before the window start

    @property
    def pnl_after_cost(self) -> float:
        return self.realized_pnl - self.copy_cost


@dataclass
class PortfolioStats:
    max_drawdown_pct: float = 0.0  # all-time, time-weighted
    current_drawdown_pct: float = 0.0  # below the all-time peak right now
    month_drawdown_pct: float = 0.0  # worst within the last 30 days
    twr: float = 0.0  # all-time time-weighted return
    months: int = 0
    profitable_months: int = 0
    track_days: float = 0.0
    all_time_pnl: float = 0.0

    @property
    def profitable_month_ratio(self) -> float:
        return self.profitable_months / self.months if self.months else 0.0


@dataclass
class Result:
    candidate: Candidate
    own_equity: float = 0.0
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    fills: Optional[FillStats] = None
    portfolio: Optional[PortfolioStats] = None
    vault: Optional[dict[str, Any]] = None
    rejected: Optional[str] = None
    score: float = 0.0
    score_parts: dict[str, float] = field(default_factory=dict)


# ------------------------------------------------------------------ pure metrics


def parse_leaderboard(rows: Iterable[dict[str, Any]]) -> list[Candidate]:
    out: list[Candidate] = []
    for r in rows:
        perf = {k: v for k, v in r.get("windowPerformances", [])}
        month, alltime = perf.get("month", {}), perf.get("allTime", {})
        try:
            out.append(
                Candidate(
                    address=r["ethAddress"].lower(),
                    lb_equity=float(r["accountValue"]),
                    month_pnl=float(month.get("pnl", 0)),
                    month_roi=float(month.get("roi", 0)),
                    month_volume=float(month.get("vlm", 0)),
                    all_time_pnl=float(alltime.get("pnl", 0)),
                    all_time_roi=float(alltime.get("roi", 0)),
                    display_name=r.get("displayName"),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def prefilter(
    cands: Iterable[Candidate],
    *,
    vault_addresses: set[str],
    min_equity: float,
    max_equity: float,
    max_turnover: float,
) -> list[Candidate]:
    """Cheap leaderboard-only screen, best (by all-time P&L) first.

    All-time P&L as the sort key favours accounts that have *stayed* profitable
    over one lucky month; monthly ROI and turnover are gates, not ranks.
    """
    keep = [
        c
        for c in cands
        if c.address not in vault_addresses
        and min_equity <= c.lb_equity <= max_equity
        and c.all_time_pnl > 0
        and c.month_pnl > 0
        and c.month_volume > 0
        and c.turnover <= max_turnover
    ]
    keep.sort(key=lambda c: c.all_time_pnl, reverse=True)
    return keep


def _is_perp(coin: str) -> bool:
    # Spot fills look like "@107" or "PURR/USDC"; perps (incl. HIP-3 "dex:COIN") don't.
    return not coin.startswith("@") and "/" not in coin


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def round_trips(fills: Iterable[dict[str, Any]]) -> tuple[list[RoundTrip], list[RoundTrip]]:
    """Rebuild round trips from raw fills. Returns ``(completed, still_open)``.

    Uses each fill's ``startPosition`` (signed size before the fill) rather than
    the ``dir`` label, so flips and liquidations need no special cases. A flip
    fill is split: the part that takes the position to zero closes the old trip
    (and carries all of ``closedPnl``), the remainder opens the new one.
    """
    by_coin: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in fills:
        if _is_perp(str(f.get("coin", ""))):
            by_coin[f["coin"]].append(f)

    done: list[RoundTrip] = []
    still_open: list[RoundTrip] = []
    for coin, cf in by_coin.items():
        cf.sort(key=lambda f: (int(f["time"]), int(f.get("tid", 0))))
        trip: Optional[RoundTrip] = None
        for f in cf:
            t = int(f["time"])
            px, sz = float(f["px"]), float(f["sz"])
            fee, pnl = float(f.get("fee", 0) or 0), float(f.get("closedPnl", 0) or 0)
            start = float(f["startPosition"])
            end = round(start + (sz if f["side"] == "B" else -sz), 10)
            if sz <= 0:
                continue

            if trip is None:
                if start == 0:
                    trip = RoundTrip(coin, "long" if end > 0 else "short", t)
                else:  # window opened mid-position: hold time unknown
                    trip = RoundTrip(coin, "long" if start > 0 else "short", t, open_observed=False)

            flip = start != 0 and end != 0 and _sign(end) != _sign(start)
            close_frac = min(abs(start) / sz, 1.0) if flip else 1.0

            trip.closed_pnl += pnl
            trip.fees += fee * close_frac
            trip.notional += px * sz * close_frac
            trip.max_size = max(trip.max_size, abs(start), abs(end) if not flip else 0.0)

            if end == 0 or flip:
                trip.close_ms = t
                done.append(trip)
                trip = None
            if flip:
                rest = 1.0 - close_frac
                trip = RoundTrip(
                    coin,
                    "long" if end > 0 else "short",
                    t,
                    fees=fee * rest,
                    notional=px * sz * rest,
                    max_size=abs(end),
                )
        if trip is not None:
            still_open.append(trip)
    return done, still_open


def fill_stats(
    fills: list[dict[str, Any]],
    *,
    window_days: float,
    copy_cost_bps: float,
    truncated: bool = False,
) -> FillStats:
    perp = [f for f in fills if _is_perp(str(f.get("coin", "")))]
    st = FillStats(fills=len(perp), truncated=truncated)
    if not perp:
        return st

    notional_by_coin: dict[str, float] = defaultdict(float)
    maker_notional = 0.0
    for f in perp:
        n = float(f["px"]) * float(f["sz"])
        st.notional += n
        notional_by_coin[f["coin"]] += n
        if not f.get("crossed", True):
            maker_notional += n
        st.realized_pnl += float(f.get("closedPnl", 0) or 0) - float(f.get("fee", 0) or 0)
    st.copy_cost = st.notional * copy_cost_bps / 10_000
    st.maker_ratio = maker_notional / st.notional if st.notional else 0.0
    hip3 = sum(n for c, n in notional_by_coin.items() if ":" in c)
    st.hip3_share = hip3 / st.notional if st.notional else 0.0
    st.top_coins = [c for c, _ in sorted(notional_by_coin.items(), key=lambda kv: -kv[1])[:4]]
    st.first_fill_ms = min(int(f["time"]) for f in perp)

    done, _ = round_trips(perp)
    st.round_trips = len(done)
    st.trades_per_day = len(done) / window_days if window_days > 0 else 0.0
    if not done:
        return st

    nets = [rt.net(copy_cost_bps) for rt in done]
    wins = [n for n in nets if n > 0]
    losses = [-n for n in nets if n < 0]
    st.win_rate = len(wins) / len(nets)
    st.profit_factor = sum(wins) / sum(losses) if losses else math.inf
    st.long_share = sum(1 for rt in done if rt.side == "long") / len(done)
    holds = [h for h in (rt.hold_hours for rt in done) if h is not None]
    st.median_hold_hours = statistics.median(holds) if holds else None

    total = sum(nets)
    top3 = sum(sorted(nets, reverse=True)[:3])
    st.pnl_without_top3 = total - top3
    st.top3_share = top3 / total if total > 0 else math.inf
    return st


def twr_drawdown(window: dict[str, Any]) -> tuple[float, float, float]:
    """Time-weighted ``(max_drawdown, current_drawdown, total_return)`` of one window.

    Each interval's return is its P&L change over the capital at work: the
    larger of the starting account value and the ending value minus the P&L
    (the start plus anything deposited during the interval). Chaining these
    returns gives an index that deposits and withdrawals don't move, which is
    what a proportional copier would have experienced.

    Hyperliquid samples the history every few days at best, so intraday
    drawdowns are understated.
    """
    pnl = sorted((int(t), float(v)) for t, v in window.get("pnlHistory", []))
    av = {int(t): float(v) for t, v in window.get("accountValueHistory", [])}
    idx = peak = 1.0
    max_dd = 0.0
    for (t0, p0), (t1, p1) in zip(pnl, pnl[1:]):
        d = p1 - p0
        capital = max(av.get(t0, 0.0), av.get(t1, 0.0) - d)
        if capital <= 1.0:  # account empty: nothing at risk, no return
            continue
        idx *= 1 + max(d / capital, -1.0)
        peak = max(peak, idx)
        max_dd = max(max_dd, 1 - idx / peak)
    return max_dd, (1 - idx / peak if peak > 0 else 0.0), idx - 1


def portfolio_stats(portfolio: list[list[Any]], key: str = "allTime") -> PortfolioStats:
    """Drawdown and monthly consistency from the ``portfolio`` info response.

    Uses the whole account (``allTime``), not ``perpAllTime``: on a unified
    account the perp value is only the margin committed to open positions, so
    returns measured against it are wildly inflated.
    """
    windows = {k: v for k, v in portfolio}
    w = windows.get(key) or {}
    pnl_hist = [(int(t), float(v)) for t, v in w.get("pnlHistory", [])]
    av = {int(t): float(v) for t, v in w.get("accountValueHistory", [])}
    st = PortfolioStats()
    if not pnl_hist:
        return st
    pnl_hist.sort()
    st.all_time_pnl = pnl_hist[-1][1]

    active = [t for t, v in sorted(av.items()) if v > 0]
    if active:
        st.track_days = (pnl_hist[-1][0] - active[0]) / DAY_MS

    st.max_drawdown_pct, st.current_drawdown_pct, st.twr = twr_drawdown(w)
    st.month_drawdown_pct, _, _ = twr_drawdown(windows.get("month") or {})

    # Month-end P&L, then month-over-month change.
    month_end: dict[str, float] = {}
    for t, p in pnl_hist:
        month_end[datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m")] = p
    prev = pnl_hist[0][1]
    for m in sorted(month_end):
        delta = month_end[m] - prev
        prev = month_end[m]
        if abs(delta) < 1e-9:
            continue
        st.months += 1
        st.profitable_months += delta > 0
    return st


def reject_reason(
    r: Result, *, min_trades: int, min_hold_hours: float, max_maker_ratio: float, min_equity: float = 0.0
) -> Optional[str]:
    f = r.fills
    if f is None:
        return R_ERROR
    if r.own_equity < min_equity:
        return R_EMPTY
    if f.truncated:
        return R_TOO_ACTIVE
    if f.round_trips < min_trades:
        return R_FEW_TRADES
    if f.median_hold_hours is None or f.median_hold_hours < min_hold_hours:
        return R_SHORT_HOLD
    if f.maker_ratio > max_maker_ratio:
        return R_MAKER
    if f.pnl_after_cost <= 0:
        return R_NEGATIVE
    return None


def score(r: Result) -> tuple[float, dict[str, float]]:
    """Transparent 0-100 score. Every part is clipped to [0, 1] before weighting.

    ============  ======  ========================================================
    part          weight  1.0 means
    ============  ======  ========================================================
    edge            30    profit factor after copy cost >= 3
    consistency     20    every month in the track record was profitable
    drawdown        20    no drawdown at all (0.0 at >= 50% given back)
    breadth         15    best 3 trades are a small share of total P&L
    return          15    window ROI after copy cost >= 50% of own equity
    ============  ======  ========================================================
    """
    f, p = r.fills, r.portfolio
    assert f is not None and p is not None
    clip = lambda x: max(0.0, min(1.0, x))  # noqa: E731
    equity = r.own_equity or r.candidate.lb_equity
    pf = f.profit_factor if math.isfinite(f.profit_factor) else 3.0
    parts = {
        "edge": 30 * clip((pf - 1.0) / 2.0),
        "consistency": 20 * clip(p.profitable_month_ratio),
        "drawdown": 20 * clip(1.0 - p.max_drawdown_pct / 0.5),
        "breadth": 15 * clip(1.0 - f.top3_share) if math.isfinite(f.top3_share) else 0.0,
        "return": 15 * clip((f.pnl_after_cost / equity) / 0.5) if equity > 0 else 0.0,
    }
    return round(sum(parts.values()), 1), {k: round(v, 1) for k, v in parts.items()}


# ----------------------------------------------------------------------- network


class InfoClient:
    """Minimal Hyperliquid ``/info`` client with a client-side weight budget.

    Hyperliquid allows 1200 weight per minute per IP. Each request is charged
    its documented base weight up front (plus 1 per 20 fills returned, after the
    fact); we spend at most ``budget`` per rolling minute and back off on 429.
    """

    WEIGHTS = {"clearinghouseState": 2, "spotClearinghouseState": 2, "portfolio": 20, "subAccounts": 20, "userFillsByTime": 20}

    def __init__(self, budget: int = 900, session: Optional[requests.Session] = None) -> None:
        self.budget = budget
        self.session = session or requests.Session()
        self._spent: list[tuple[float, int]] = []

    def _charge(self, weight: int) -> None:
        while True:
            now = time.monotonic()
            self._spent = [(t, w) for t, w in self._spent if now - t < 60]
            if sum(w for _, w in self._spent) + weight <= self.budget:
                self._spent.append((now, weight))
                return
            time.sleep(max(0.5, 60 - (now - self._spent[0][0])))

    def post(self, body: dict[str, Any]) -> Any:
        self._charge(self.WEIGHTS.get(body["type"], 20))
        for attempt in range(6):
            resp = self.session.post(INFO_URL, json=body, timeout=30)
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            if body["type"] == "userFillsByTime" and isinstance(data, list):
                self._charge(len(data) // 20)
            return data
        raise RuntimeError(f"rate limited: {body['type']}")

    def fills(self, user: str, start_ms: int, max_fills: int) -> tuple[list[dict[str, Any]], bool]:
        """Fills since ``start_ms``, paging forward (the API returns oldest first).

        Returns ``(fills, truncated)``. Truncated means ``max_fills`` was hit
        before reaching the present, so the most recent fills are *missing*;
        callers must not score a truncated history.
        """
        out: list[dict[str, Any]] = []
        seen: set[tuple[Any, Any]] = set()
        while len(out) < max_fills:
            page = self.post(
                {"type": "userFillsByTime", "user": user, "startTime": start_ms, "aggregateByTime": True}
            )
            fresh = [f for f in page if (f.get("tid"), f.get("time")) not in seen]
            for f in fresh:
                seen.add((f.get("tid"), f.get("time")))
            out.extend(fresh)
            if len(page) < FILLS_PAGE or not fresh:
                return out, False
            start_ms = max(int(f["time"]) for f in page)  # inclusive: dedup handles the overlap
        return out, True


def load_json_cached(url: str, path: Path, refresh: bool, max_age_h: float = 12) -> Any:
    if path.exists() and not refresh and time.time() - path.stat().st_mtime < max_age_h * 3600:
        return json.loads(path.read_text(encoding="utf-8"))
    print(f"downloading {url} ...")
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(resp.text, encoding="utf-8")
    return resp.json()


def open_vaults_by_leader(vaults: list[dict[str, Any]]) -> tuple[set[str], dict[str, dict[str, Any]]]:
    addresses: set[str] = set()
    by_leader: dict[str, dict[str, Any]] = {}
    for v in vaults:
        s = v.get("summary", {})
        addr = str(s.get("vaultAddress", "")).lower()
        if addr:
            addresses.add(addr)
        leader = str(s.get("leader", "")).lower()
        if leader and not s.get("isClosed"):
            tvl = float(s.get("tvl", 0) or 0)
            if tvl > by_leader.get(leader, {}).get("tvl", -1):
                by_leader[leader] = {"name": s.get("name"), "address": addr, "tvl": tvl}
    return addresses, by_leader


def fetch_raw(
    client: InfoClient,
    c: Candidate,
    *,
    days: int,
    max_fills: int,
    cache_dir: Optional[Path] = None,
    max_age_h: float = 12,
) -> dict[str, Any]:
    """Every API response one candidate needs, cached so re-scoring is free.

    The cache key includes ``days`` and ``max_fills``: a different window needs
    different fills. ``subs`` is only fetched (and is otherwise ``None``) when
    the address holds under half its leaderboard equity itself.
    """
    path = cache_dir / f"{c.address}.json" if cache_dir else None
    if path and path.exists() and time.time() - path.stat().st_mtime < max_age_h * 3600:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("days") == days and raw.get("max_fills") == max_fills:
            return raw

    raw: dict[str, Any] = {"days": days, "max_fills": max_fills, "fetched_ms": int(time.time() * 1000)}
    raw["state"] = client.post({"type": "clearinghouseState", "user": c.address})
    raw["spot"] = client.post({"type": "spotClearinghouseState", "user": c.address}) or {}
    raw["subs"] = None
    if own_equity(raw) < 0.5 * c.lb_equity:
        raw["subs"] = client.post({"type": "subAccounts", "user": c.address}) or []
    if not _subaccount_dominated(raw):
        raw["portfolio"] = client.post({"type": "portfolio", "user": c.address})
        start = raw["fetched_ms"] - days * DAY_MS
        raw["fills"], raw["truncated"] = client.fills(c.address, start, max_fills)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw), encoding="utf-8")
    return raw


def own_equity(raw: dict[str, Any]) -> float:
    """Perp account value plus spot stablecoins.

    Unified accounts keep idle collateral in the spot wallet: without it, a
    trader who is flat right now looks like an empty account.
    """
    perp = float(raw["state"]["marginSummary"]["accountValue"])
    spot = sum(
        float(b.get("total", 0) or 0) for b in raw.get("spot", {}).get("balances", []) if b.get("coin") in STABLES
    )
    return perp + spot


def _subaccount_dominated(raw: dict[str, Any]) -> bool:
    """The leaderboard sums sub-accounts into the master. If they hold more than
    the master does, the trading this scan would score happens elsewhere."""
    subs = raw.get("subs") or []
    sub_equity = sum(
        float(s.get("clearinghouseState", {}).get("marginSummary", {}).get("accountValue", 0) or 0) for s in subs
    )
    return sub_equity > own_equity(raw)


def evaluate(raw: dict[str, Any], c: Candidate, *, copy_cost_bps: float) -> Result:
    r = Result(candidate=c, own_equity=own_equity(raw))
    state = raw["state"]
    for ap in state.get("assetPositions", []):
        p = ap.get("position", {})
        szi = float(p.get("szi", 0) or 0)
        if szi:
            r.open_positions.append(
                {
                    "coin": p.get("coin"),
                    "side": "long" if szi > 0 else "short",
                    "notional": abs(float(p.get("positionValue", 0) or 0)),
                    "leverage": (p.get("leverage") or {}).get("value"),
                    "unrealized_pnl": float(p.get("unrealizedPnl", 0) or 0),
                }
            )
    if _subaccount_dominated(raw):
        r.rejected = R_SUBACCOUNTS
        return r
    r.portfolio = portfolio_stats(raw["portfolio"])
    r.fills = fill_stats(
        raw["fills"], window_days=raw["days"], copy_cost_bps=copy_cost_bps, truncated=raw["truncated"]
    )
    return r


# ----------------------------------------------------------------------- reports


def _usd(x: float) -> str:
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1e6:
        return f"{sign}${x / 1e6:.2f}M"
    if x >= 1e3:
        return f"{sign}${x / 1e3:.1f}k"
    return f"{sign}${x:.0f}"


def _pf(x: float) -> str:
    return "inf" if not math.isfinite(x) else f"{x:.2f}"


def _hold(h: Optional[float]) -> str:
    if h is None:
        return "?"
    return f"{h / 24:.1f}d" if h >= 48 else f"{h:.1f}h"


def markdown_report(top: list[Result], rejected: dict[str, int], meta: dict[str, Any]) -> str:
    lines = [
        "# Hyperliquid copy-trading candidates",
        "",
        f"Generated {meta['generated']} UTC. Window: last {meta['days']} days of fills. "
        f"Copy cost: {meta['copy_cost_bps']} bps per fill. "
        f"Leaderboard rows: {meta['leaderboard_rows']}, passed prefilter: {meta['prefiltered']}, "
        f"deep-scanned: {meta['scanned']}, qualified: {meta['qualified']}.",
        "",
        "P&L, ROI, win rate and profit factor are **after** the copy cost. Equity is the "
        "address's own perp value plus spot stablecoins. Drawdowns are time-weighted (deposits and "
        "withdrawals don't count) from history Hyperliquid samples every few days, so intraday dips are "
        "understated. HIP-3 = share of volume on builder-deployed `dex:` perps (stocks, indices). "
        "Past results are not a forecast.",
        "",
        "| # | Address | Score | Equity | P&L (window) | ROI | PF | Win | Trips | Hold | Trips/day "
        "| Max DD | DD 30d | Below peak | Months + | Long % | HIP-3 | Coins |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(top, 1):
        f, p, c = r.fills, r.portfolio, r.candidate
        assert f and p
        eq = r.own_equity or c.lb_equity
        lines.append(
            f"| {i} | [`{c.address[:6]}..{c.address[-4:]}`]({EXPLORER_URL}{c.address}) | {r.score} "
            f"| {_usd(eq)} | {_usd(f.pnl_after_cost)} | {f.pnl_after_cost / eq:+.0%} | {_pf(f.profit_factor)} "
            f"| {f.win_rate:.0%} | {f.round_trips} | {_hold(f.median_hold_hours)} | {f.trades_per_day:.1f} "
            f"| {p.max_drawdown_pct:.0%} | {p.month_drawdown_pct:.0%} | {p.current_drawdown_pct:.0%} "
            f"| {p.profitable_months}/{p.months} | {f.long_share:.0%} | {f.hip3_share:.0%} "
            f"| {', '.join(f.top_coins)} |"
        )
    lines += ["", "## Details", ""]
    for i, r in enumerate(top, 1):
        f, p, c = r.fills, r.portfolio, r.candidate
        assert f and p
        name = f" ({c.display_name})" if c.display_name else ""
        lines += [
            f"### {i}. `{c.address}`{name}",
            "",
            f"- Score {r.score}: " + ", ".join(f"{k} {v}" for k, v in r.score_parts.items()),
            f"- Track record {p.track_days:.0f} days, all-time P&L {_usd(p.all_time_pnl)}, "
            f"time-weighted return {p.twr:+.0%}, max drawdown {p.max_drawdown_pct:.0%}, "
            f"now {p.current_drawdown_pct:.0%} below peak",
            f"- Window: {f.fills} fills, {f.round_trips} round trips, gross notional {_usd(f.notional)}, "
            f"copy cost {_usd(f.copy_cost)}, P&L without best 3 trips {_usd(f.pnl_without_top3)}",
            f"- Maker share {f.maker_ratio:.0%}, {f.trades_per_day:.1f} round trips/day, "
            f"HIP-3 share {f.hip3_share:.0%}",
        ]
        if r.open_positions:
            pos = "; ".join(
                f"{o['side']} {o['coin']} {_usd(o['notional'])} @ {o['leverage']}x (uPnL {_usd(o['unrealized_pnl'])})"
                for o in r.open_positions
            )
            lines.append(f"- Open now: {pos}")
        else:
            lines.append("- Open now: flat")
        if r.vault:
            lines.append(
                f"- Runs vault **{r.vault['name']}** (TVL {_usd(r.vault['tvl'])}). Depositing copies "
                "them exactly with no delay - compare against copying before building it."
            )
        lines.append(f"- Explorer: {EXPLORER_URL}{c.address}")
        lines.append("")
    lines += ["## Rejected in deep scan", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(rejected.items(), key=lambda kv: -kv[1])] or ["- none"]
    return "\n".join(lines) + "\n"


def console_table(top: list[Result]) -> str:
    hdr = f"{'#':>2}  {'address':<42} {'score':>5} {'equity':>8} {'pnl':>8} {'roi':>6} {'pf':>5} {'win':>4} {'trips':>5} {'hold':>6} {'maxdd':>5} {'dd30':>5} {'mo+':>5} {'hip3':>4}  coins"
    rows = [hdr, "-" * len(hdr)]
    for i, r in enumerate(top, 1):
        f, p, c = r.fills, r.portfolio, r.candidate
        assert f and p
        eq = r.own_equity or c.lb_equity
        rows.append(
            f"{i:>2}  {c.address:<42} {r.score:>5} {_usd(eq):>8} {_usd(f.pnl_after_cost):>8} "
            f"{f.pnl_after_cost / eq:>+6.0%} {_pf(f.profit_factor):>5} {f.win_rate:>4.0%} {f.round_trips:>5} "
            f"{_hold(f.median_hold_hours):>6} {p.max_drawdown_pct:>5.0%} {p.month_drawdown_pct:>5.0%} "
            f"{p.profitable_months:>2}/{p.months:<2} {f.hip3_share:>4.0%}  "
            f"{','.join(f.top_coins)}"
            + ("  [vault]" if r.vault else "")
        )
    return "\n".join(rows)


# -------------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description="Score Hyperliquid traders as copy-trading candidates (read-only)")
    ap.add_argument("--top", type=int, default=10, help="how many to report")
    ap.add_argument("--deep", type=int, default=40, help="how many prefiltered candidates to deep-scan")
    ap.add_argument("--days", type=int, default=60, help="fill-history window in days")
    ap.add_argument("--max-fills", type=int, default=6000, help="fill cap per address (HL keeps 10k)")
    ap.add_argument("--copy-cost-bps", type=float, default=5.0, help="slippage charged per fill for copying late")
    ap.add_argument("--min-equity", type=float, default=25_000)
    ap.add_argument("--max-equity", type=float, default=50_000_000)
    ap.add_argument("--max-turnover", type=float, default=300, help="max monthly volume / equity")
    ap.add_argument("--min-trades", type=int, default=10, help="min completed round trips in the window")
    ap.add_argument("--min-hold-hours", type=float, default=1.0)
    ap.add_argument("--max-maker-ratio", type=float, default=0.8)
    ap.add_argument("--refresh", action="store_true", help="re-download leaderboard and vault lists")
    ap.add_argument("--out", default="data", help="output directory")
    args = ap.parse_args()

    out = Path(args.out)
    lb = load_json_cached(LEADERBOARD_URL, out / "leaderboard.json", args.refresh)
    vaults = load_json_cached(VAULTS_URL, out / "vaults.json", args.refresh)
    vault_addrs, vault_by_leader = open_vaults_by_leader(vaults)

    cands = parse_leaderboard(lb.get("leaderboardRows", []))
    pre = prefilter(
        cands,
        vault_addresses=vault_addrs,
        min_equity=args.min_equity,
        max_equity=args.max_equity,
        max_turnover=args.max_turnover,
    )
    print(f"leaderboard rows {len(cands)}, passed prefilter {len(pre)}, deep-scanning {min(args.deep, len(pre))}")

    client = InfoClient()
    results: list[Result] = []
    rejected: dict[str, int] = defaultdict(int)
    for i, c in enumerate(pre[: args.deep], 1):
        try:
            raw = fetch_raw(client, c, days=args.days, max_fills=args.max_fills, cache_dir=out / "leader_cache")
            r = evaluate(raw, c, copy_cost_bps=args.copy_cost_bps)
        except Exception as exc:  # noqa: BLE001 - one bad address must not end the scan
            r = Result(candidate=c, rejected=f"{R_ERROR}: {exc}"[:120])
        if r.rejected is None:
            r.rejected = reject_reason(
                r,
                min_trades=args.min_trades,
                min_hold_hours=args.min_hold_hours,
                max_maker_ratio=args.max_maker_ratio,
                min_equity=args.min_equity,
            )
        if r.rejected is None:
            r.score, r.score_parts = score(r)
            r.vault = vault_by_leader.get(c.address)
            results.append(r)
        rejected[(r.rejected or "").split(":")[0]] += r.rejected is not None
        status = r.rejected or f"score {r.score}"
        print(f"[{i:>3}/{min(args.deep, len(pre))}] {c.address}  {status}")

    rejected = {k: v for k, v in rejected.items() if v}
    results.sort(key=lambda r: r.score, reverse=True)
    top = results[: args.top]
    meta = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "days": args.days,
        "copy_cost_bps": args.copy_cost_bps,
        "leaderboard_rows": len(cands),
        "prefiltered": len(pre),
        "scanned": min(args.deep, len(pre)),
        "qualified": len(results),
    }
    (out / "leader_scan.md").write_text(markdown_report(top, rejected, meta), encoding="utf-8")
    (out / "leader_scan.json").write_text(
        json.dumps({"meta": meta, "rejected": rejected, "results": [asdict(r) for r in results]}, indent=2, default=str),
        encoding="utf-8",
    )
    print()
    print(console_table(top))
    print(f"\nreport: {out / 'leader_scan.md'}  (all {len(results)} qualified in {out / 'leader_scan.json'})")


if __name__ == "__main__":
    main()
