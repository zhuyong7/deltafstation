"""513310（中韩半导体ETF华泰柏瑞）网格交易 — 波动率估算 + 网格参数建议 + 逐bar回测。

本脚本独立运行（不依赖 deltafstation 回测引擎），因为网格策略的多档位挂单逻辑
无法用 deltafq 的 1/-1 信号表达：

    python etf_grid_513310.py --symbol 513310 --capital 150000

流程：
  1. 从 data/raw/513310.SH.csv 读日线（或本地 CSV / baostock）
  2. 计算 14 日 ATR 与日收益率波动率
  3. 按 ATR×0.5 + 最小成本约束（g ≥ 2×手续费绝对值）推荐 grid_size / lot_size
  4. 逐 bar 回测：买格 p0 - k·g，资金耗尽线 p0 - n·g 之后只卖不买；
     每笔卖出收益 = g×lot - 双边手续费 - 滑点，可正可负（如实反映亏损成交）

数据格式：Date,Open,High,Low,Close,Volume

---

今日已跑通（2026-10-28）：
  ✓ 本地数据加载（513310.SH.csv）
  ✓ ATR / 日收益率波动率计算
  ✓ 参数推荐（g=0.0815, lot=5800, n=7, 资金耗尽线 3.07）
  ✓ 逐 bar 回测（基础版：同一bar只处理一档买入）

明日继续（2026-10-29）：
  [ ] 回测中同bar多档位买入的级联处理（当前已实现，待实测验证）
  [ ] 卖出指针逻辑：当前按"最低未平档"推断，极端行情下可能有 bug
  [ ] 资金耗尽后只卖不买的实际验证（n_lost_fills 计数是否正确）
  [ ] 加入权益曲线 / 资金峰值 / 最大回撤的详细展示
  [ ] 输出 JSON 到 data/results/，方便前端查看
  [ ] 回测参数 sweep：g ∈ {ATR×0.3, 0.5, 0.7}，lot 按比例缩放，对比胜率
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.normpath(os.path.join(_THIS_DIR, ".."))
_RAW_DIR = os.path.join(_DATA_DIR, "raw")


# ---------------------------------------------------------------------------
# 1. 数据获取
# ---------------------------------------------------------------------------

def load_price_data(
    symbol: str = "513310",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    prefer_local: bool = True,
) -> pd.DataFrame:
    """加载日线数据，列：Date,Open,High,Low,Close,Volume。"""
    if prefer_local:
        # 匹配 data/raw 下 513310*.csv（如 513310.SH.csv）
        candidates = sorted(
            f for f in os.listdir(_RAW_DIR)
            if f.endswith(".csv") and f.upper().startswith(symbol.upper())
        )
        if candidates:
            path = os.path.join(_RAW_DIR, candidates[-1])
            df = pd.read_csv(path)
            df["Date"] = pd.to_datetime(df["Date"])
            df = df.sort_values("Date").reset_index(drop=True)
            print(f"[data] 本地: {path} ({len(df)} rows)")
            return _filter_range(df, start_date, end_date)

    # 回退：baostock
    try:
        import baostock as bs
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_msg}")
        code = f"sh.{symbol}" if symbol.startswith("5") else f"sz.{symbol}"
        s = start_date or (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        e = end_date or datetime.now().strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            code, "date,open,high,low,close,volume",
            start_date=s, end_date=e, frequency="d",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        bs.logout()
        df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
        for c in ["Open", "High", "Low", "Close", "Volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.dropna(subset=["Close"])
        print(f"[data] baostock: {code} ({len(df)} rows)")
        return df
    except Exception as e:
        raise RuntimeError(f"无法获取 {symbol} 数据: {e}") from e


def _filter_range(df: pd.DataFrame, start_date: Optional[str], end_date: Optional[str]) -> pd.DataFrame:
    if start_date:
        df = df[df["Date"] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df["Date"] <= pd.to_datetime(end_date)]
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2. 波动率
# ---------------------------------------------------------------------------

def compute_volatility(df: pd.DataFrame, window: int = 14) -> dict:
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)

    tr = pd.concat([high - low,
                    (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr_abs = float(tr.rolling(window).mean().iloc[-1])
    last_close = float(close.iloc[-1])
    atr_pct = atr_abs / last_close

    ret = close.pct_change().dropna()
    daily_vol = float(ret.std())

    rng = (high - low)
    avg_range = float(rng.mean())

    return {
        "atr14_abs": round(atr_abs, 4),
        "atr14_pct": round(atr_pct, 4),
        "daily_return_std": round(daily_vol, 4),
        "avg_daily_range_pct": round(float((rng / close).mean() * 100), 3),
        "price_last": last_close,
        "price_min": float(low.min()),
        "price_max": float(high.max()),
        "date_first": str(df["Date"].iloc[0].date()),
        "date_last": str(df["Date"].iloc[-1].date()),
        "n_days": len(df),
    }


# ---------------------------------------------------------------------------
# 3. 参数建议
# ---------------------------------------------------------------------------

@dataclass
class GridConfig:
    symbol: str
    p0: float          # 起始价（锚点）
    grid_size: float   # 间距 g（元）
    lot_size: int      # 每格份数
    n_grids: int       # 网格数 n
    capital: float     # 总资金
    fee_rate: float    # 单边手续费率
    slippage_pct: float  # 滑点率

    @property
    def lower_limit(self) -> float:
        """资金耗尽线：p0 - n·g。"""
        return round(self.p0 - self.n_grids * self.grid_size, 4)

    @property
    def cost_per_grid(self) -> float:
        return round(self.lot_size * self.grid_size + 2 * self.fee_rate * self.lot_size * self.p0, 2)


def recommend_grid(
    symbol: str,
    capital: float,
    vol: dict,
    p0: Optional[float] = None,
    max_depth_pct: float = 0.15,
    fee_rate: float = 0.0004,
    slippage_pct: float = 0.0003,
) -> GridConfig:
    """由 ATR 与资金推导 (g, lot, n)。"""
    p0 = p0 or vol["price_last"]
    atr = vol["atr14_abs"]

    # 最小间距约束：g ≥ (2×fee + 2×slip) × p0，否则单格毛利覆盖不了双边成本
    min_g = 2 * (fee_rate + slippage_pct) * p0
    g = round(max(round(atr * 0.5, 4), min_g), 4)

    # n = 允许的最大下跌深度
    n = max(2, int(round(max_depth_pct * p0 / g)))

    # lot：总资金平摊到 n 格（含每格双边成本），取 100 股整数倍
    per_grid = capital / n
    lot = int(per_grid / p0) // 100 * 100
    if lot < 100:
        lot = 100

    return GridConfig(
        symbol=symbol, p0=p0, grid_size=g, lot_size=lot,
        n_grids=n, capital=capital, fee_rate=fee_rate, slippage_pct=slippage_pct,
    )


# ---------------------------------------------------------------------------
# 4. 逐bar回测
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    cfg: GridConfig
    initial_capital: float
    final_equity: float
    n_trades: int
    n_buys: int
    n_sells: int
    total_fees: float
    total_slippage: float
    n_lost_fills: int          # 因资金不足而失败成交的笔数（亏损）
    max_equity_dd_pct: float   # 权益最大回撤 %
    min_cash: float            # 盘中最低可用资金
    grid_breakdown_pct: float  # 各档位成交次数占比（最常用档位）
    equity_curve: list         # [date, equity]
    grid_fills: dict           # 档位 k → 成交次数

    def summary(self) -> dict:
        pnl = self.final_equity - self.initial_capital
        return {
            "initial_capital": round(self.initial_capital, 2),
            "final_equity": round(self.final_equity, 2),
            "net_pnl": round(pnl, 2),
            "net_pnl_pct": round(pnl / self.initial_capital * 100, 2),
            "n_trades": self.n_trades,
            "n_buys": self.n_buys,
            "n_sells": self.n_sells,
            "total_fees": round(self.total_fees, 2),
            "total_slippage": round(self.total_slippage, 2),
            "n_lost_fills": self.n_lost_fills,
            "max_equity_dd_pct": round(self.max_equity_dd_pct, 2),
            "min_cash": round(self.min_cash, 2),
            "grid_fills": {f"level_{k}": v for k, v in self.grid_fills.items()},
            "period": f"{self.equity_curve[0][0]} ~ {self.equity_curve[-1][0]}" if self.equity_curve else "",
        }


def run_grid_backtest(cfg: GridConfig, df: pd.DataFrame) -> BacktestResult:
    """逐bar网格回测。

    买格: L_k = p0 - k·g,  k = 1..n      跌到 L_k 买入 lot 股
    卖格: L_k + g           每个持仓档位独立对应，涨回卖出 lot 股
    资金耗尽线: L_n 之下不再买入
    """
    g = cfg.grid_size
    lot = cfg.lot_size
    n = cfg.n_grids
    p0 = cfg.p0
    fee_rate = cfg.fee_rate
    slip = cfg.slippage_pct
    capital = cfg.capital

    close = df["Close"].astype(float).values
    dates = df["Date"].astype(str).str.slice(0, 10).values
    if len(close) < 2:
        raise ValueError("数据不足")

    buy_levels = [p0 - k * g for k in range(1, n + 1)]
    cash = capital
    slots: list[dict] = []     # 持仓档位: {"k": k, "buy_price": L_k, "sell_price": L_k + g}
    grid_fills: dict = {}
    n_lost = 0
    total_fees = 0.0
    total_slip = 0.0
    n_buys = 0
    n_sells = 0
    min_cash = cash
    equity_curve: list = []
    peak = capital
    max_dd = 0.0

    for i, price in enumerate(close):
        # --- 买入：跌穿所有未持仓的买格（含资金耗尽检查）---
        for k, lv in enumerate(buy_levels, start=1):
            if price <= lv and not any(s["k"] == k for s in slots):
                buy_px = lv * (1 - slip)           # 滑点：买贵一点
                cost = buy_px * lot
                fee = fee_rate * cost
                need = cost + fee
                if cash >= need:
                    cash -= need
                    slots.append({"k": k, "buy_price": lv, "sell_price": lv + g})
                    grid_fills[k] = grid_fills.get(k, 0) + 1
                    total_fees += fee
                    total_slip += (lv - buy_px) * lot
                    n_buys += 1
                    min_cash = min(min_cash, cash)
                else:
                    n_lost += 1                     # 资金不足，失败成交（本笔无成本）
                # 同bar内继续检查更低档位

        # --- 卖出：触及各自卖格的持仓档位（卖便宜一点）---
        still = []
        for s in slots:
            sell_px = s["sell_price"] * (1 - slip)
            if price >= s["sell_price"]:
                proceeds = sell_px * lot
                fee = fee_rate * proceeds
                cash += proceeds - fee
                total_fees += fee
                total_slip += (s["sell_price"] - sell_px) * lot
                n_sells += 1
            else:
                still.append(s)
        slots = still

        equity = cash + sum(s["k"] * 0 + s["buy_price"] * lot for s in slots) + (len(slots) * 0)
        # 权益 = 现金 + 持仓市值（按收盘）
        equity = cash + len(slots) * lot * price
        equity_curve.append((dates[i], round(equity, 2)))
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100)

    eq0 = equity_curve[0][1] if equity_curve else capital
    final_eq = equity_curve[-1][1] if equity_curve else capital

    # 最常用档位占比
    if grid_fills:
        top = max(grid_fills, key=grid_fills.get)
        breakdown = round(grid_fills[top] / sum(grid_fills.values()) * 100, 1)
    else:
        breakdown = 0.0

    return BacktestResult(
        cfg=cfg, initial_capital=capital, final_equity=final_eq,
        n_trades=n_buys + n_sells, n_buys=n_buys, n_sells=n_sells,
        total_fees=total_fees, total_slippage=total_slip,
        n_lost_fills=n_lost, max_equity_dd_pct=max_dd,
        min_cash=min_cash, grid_breakdown_pct=breakdown,
        equity_curve=equity_curve, grid_fills=grid_fills,
    )


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------

def run(symbol: str = "513310", capital: float = 150000.0,
        lookback_days: int = 60, max_depth_pct: float = 0.15,
        fee_rate: float = 0.0004, slippage_pct: float = 0.0003,
        p0: Optional[float] = None, output_file: Optional[str] = None) -> dict:
    df = load_price_data(symbol)

    # 全量算波动率，回测用最近 lookback_days
    vol = compute_volatility(df)
    bt_df = df.tail(lookback_days).reset_index(drop=True)

    cfg = recommend_grid(symbol, capital, vol, p0=p0,
                         max_depth_pct=max_depth_pct,
                         fee_rate=fee_rate, slippage_pct=slippage_pct)

    print("\n=== 波动率（全量）===")
    for k, v in vol.items():
        print(f"  {k}: {v}")

    print("\n=== 推荐参数 ===")
    print(f"  p0 = {cfg.p0}, g = {cfg.grid_size}, lot = {cfg.lot_size}, n = {cfg.n_grids}")
    print(f"  资金耗尽线 = {cfg.lower_limit}, 每格成本 ≈ {cfg.cost_per_grid}")
    print(f"  总资金 {cfg.capital:.0f}, 满格资金需求 ≈ "
          f"{cfg.cost_per_grid * cfg.n_grids:,.0f}")

    # 回测
    bt = run_grid_backtest(cfg, bt_df)
    s = bt.summary()
    print(f"\n=== 回测（最近 {lookback_days} 天）===")
    for k, v in s.items():
        if k not in ("grid_fills", "equity_curve"):
            print(f"  {k}: {v}")

    out = {"volatility": vol, "grid_config": {
        "symbol": cfg.symbol, "p0": cfg.p0, "grid_size": cfg.grid_size,
        "lot_size": cfg.lot_size, "n_grids": cfg.n_grids, "capital": cfg.capital,
        "fee_rate": cfg.fee_rate, "slippage_pct": cfg.slippage_pct,
        "lower_limit": cfg.lower_limit, "cost_per_grid": cfg.cost_per_grid,
    }, "backtest": s, "grid_fills": {f"level_{k}": v for k, v in bt.grid_fills.items()}}

    if output_file:
        os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n[saved] {output_file}")

    return out


def main():
    p = argparse.ArgumentParser(description="513310 网格：波动率 + 参数建议 + 回测")
    p.add_argument("--symbol", default="513310")
    p.add_argument("--capital", type=float, default=150000.0)
    p.add_argument("--lookback", type=int, default=60, help="回测天数")
    p.add_argument("--depth", type=float, default=0.15, help="最大下跌深度（比例）")
    p.add_argument("--fee", type=float, default=0.0004, help="单边手续费率")
    p.add_argument("--slippage", type=float, default=0.0003, help="滑点率")
    p.add_argument("--p0", type=float, default=None, help="手动指定锚点价（默认用最新收盘）")
    p.add_argument("--output", default=None, help="输出 JSON 路径")
    a = p.parse_args()

    run(symbol=a.symbol, capital=a.capital, lookback_days=a.lookback,
        max_depth_pct=a.depth, fee_rate=a.fee, slippage_pct=a.slippage,
        p0=a.p0, output_file=a.output)


if __name__ == "__main__":
    main()
