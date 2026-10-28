# 设计文档：`/strategy` 页支持 T+0 ETF 分时回测

## 1. 背景与目标

当前 `/strategy`（策略回测页）的工作方式是：

- 用户选择「策略 + 投资标的 + 数据源 + 开始/结束日期」；
- 点击「运行回测」后，前端先调用 `POST /api/data/symbols/<symbol>/files` 拉取/更新该标的的 **日线** CSV 数据（`data/raw/<SYMBOL>.csv`）；
- 再调用 `POST /api/backtests` 让后端 `BacktestEngine` 读取该 CSV，按 **每个交易日一根 bar** 逐根回放（买入/卖出均按该 bar 的 `Close` 执行）。

这套流程本质是「按天回测」，无法观察 **一天内部** 的分时行情，也无法用于 T+0 ETF（同一天内可反复买卖）的场景。

**目标**：在尽量小改动的前提下，让 `/strategy` 页支持：

1. 观察一天内的 **分钟级** 行情（含 1m）；
2. 用同一个标的、同一个策略，在 **分钟 bar** 上回测，模拟 T+0 ETF 的分时交易；
3. 与现有 **日线回测** 完全共存、互不破坏。

## 2. 范围（Scope）

### 包含（In scope）

- 在回测页新增「K线周期」选择器：`1d / 5m / 15m / 60m / 1m`；
- 数据抓取链路（`/api/data/symbols/<symbol>/files`）支持按周期拉取并落盘 **独立文件**；
- 回测链路（`/api/backtests`）支持分钟级 bar 回放；
- 数据源与周期的 **后端校验**（yfinance 仅 1d；baostock 5/15/60m；miniqmt 可含 1m）；
- 结果 JSON 持久化保存 `interval` 元数据；
- 与现有日线的完全兼容（不传 `interval` 时默认 `1d`，文件名/日期格式不变）。

### 不包含（Out of scope，MVP 明确跳过）

为避免改动扩散，以下均 **不做**，后续再考虑：

- ETF 100 股整手（lot size）取整；
- 强制收盘平仓（force-close EOD / T+0 硬约束）；
- 滑点模型在分钟级的细化（沿用现有 `slippage` 传参）；
- 回测历史列表 / 结果详情的分时专属 UI 展示；
- 年化收益、夏普比率在分钟 bar 下的语义修正（`periods_per_year` 调整）——MVP 下这些指标仍按 bar 数粗略计算，数值会偏“每 bar”而非“每日”，属于已知偏差，不阻塞功能；
- 修改 `deltafq` 安装包（site-packages）内任何代码。

## 3. 关键结论（来自代码探查）

| 能力 | 现状 | 结论 |
|---|---|---|
| `deltafq.DataFetcher.fetch_data` | 已支持 `interval` 参数（`1m/5m/15m/30m/60m/1d...`） | 只是 `data_manager.py` 里写死成 `'1d'`，**透传即可** |
| `deltafq.BacktestEngine.run_backtest` | 逐 bar 回放，按该 bar 的 `Close` 成交 | 喂分钟 `Close` 序列即可实现分钟级/T+0 回放，**无需改 deltafq** |
| 策略契约 `generate_signals(data)` | 返回与输入同索引的信号序列，对 bar 粒度无假设 | 现有策略可原样跑在分钟 bar 上（参数为“天数”调优的会偏多触发，属策略层面问题，MVP 不处理） |
| `data_manager.fetch_data` L177 | `interval='1d'` 写死 | 需加 `interval` 参数 |
| `data_manager.save_data` / 文件命名 L201 | 一律 `SYMBOL.csv` | 需按周期区分文件，避免日线与分钟线互相覆盖 |
| `_standardize_dataframe` L87 | `Date` 强制截成 `%Y-%m-%d` | 分钟数据需保留 `%H:%M:%S` |
| `backtest_engine.load_and_prepare_data` L141 | 区间过滤用 `pd.to_datetime(end_date)`（00:00:00） | 日线没问题；分钟数据会漏掉结束日当天的 bar，需修 |
| `yfinance` | 不支持 A 股代码；分钟数据仅限近 7–30 天 | 对 A 股 T+0 ETF 实际不可用，**限制为 1d** |
| `baostock` | 支持 `5m/15m/30m/60m`，**不支持 1m** | 周期校验据此 |
| `miniqmt` | 支持 `1m/5m/15m/30m/60m/1d`（依赖本地 miniQMT + `xtquant`） | 1m 唯一来源 |

## 4. 数据约定

### 文件命名

| 周期 | 文件名 | `Date` 列格式 |
|---|---|---|
| `1d`（默认） | `<SYMBOL>.csv` | `YYYY-MM-DD` |
| `5m` | `<SYMBOL>_5m.csv` | `YYYY-MM-DD HH:MM:SS` |
| `15m` | `<SYMBOL>_15m.csv` | `YYYY-MM-DD HH:MM:SS` |
| `60m` | `<SYMBOL>_60m.csv` | `YYYY-MM-DD HH:MM:SS` |
| `1m` | `<SYMBOL>_1m.csv` | `YYYY-MM-DD HH:MM:SS` |

要点：

- **日线沿用原文件名与日期格式**，完全向后兼容；
- 每个周期 **独立文件**，互不覆盖、互不干扰；
- 文件必须含 `Close` 列（引擎只用 `Close` 做回放）；OHLCV 其余列可选保留；
- 周期到文件的映射必须是 **精确文件名匹配**，不能沿用现有 `find_latest_file` 的“前缀 + 最近修改时间”逻辑——否则 `SH.510300.csv`（日线）与 `SH.510300_5m.csv` 会互相命中。

### 数据源 × 周期 支持矩阵

| 数据源 | 允许周期 |
|---|---|
| `yfinance` | `1d` |
| `baostock` | `5m, 15m, 60m` |
| `miniqmt` | `1m, 5m, 15m, 60m` |

- 超出矩阵的组合，由 **后端** 返回 400 并给出明确错误信息（不做前端禁用，按“后端校验”）；
- `30m` 各数据源虽支持，但 MVP UI 不暴露，UI 只给 `1d/5m/15m/60m/1m`；
- 未传 `interval` 时一律视为 `1d`。

## 5. 详细改动设计

### 5.1 `frontend/templates/backtest.html`

在「数据源」下拉框（`#backtestDataSource`）之后、开始日期之前，新增一个「K线周期」选择器 `#backtestInterval`：

```
1d (日线) / 5m / 15m / 60m / 1m
```

- 默认选中 `1d`；
- 不改动现有 symbol 建议、基准、历史列表、结果卡片与图表；
- 仅追加一个 `select`，用户可见改动最小。

### 5.2 `frontend/static/js/backtest.js`

只改三处既有函数，串起 `interval`：

1. **`getBacktestParams()`（L632）**：新增读取
   ```
   interval: $('backtestInterval')?.value || '1d'
   ```
2. **`syncMarketData(symbol, startDate, endDate, dataSource, silent)`（L648）**：
   - 增加形参 `interval = '1d'`；
   - GET 本地缓存检查与 POST 拉取，都在 URL / body 上带上 `interval`：
     - `GET /api/data/symbols/${symbol}/files?interval=${interval}`
     - `POST body: { start_date, end_date, data_source, force_refresh, interval }`
   - 命中本地文件时，按该周期对应的文件名返回（后端保证文件名与周期一致）。
3. **`runBacktest()`（L706）**：
   - 把 `params.interval` 传给 `syncMarketData(...)`；
   - 在 `POST /api/backtests` 的 body 里带上 `interval`（仅作元数据）。

无需改动 `parseValuesDf`、`drawBacktestCharts`、`getBenchmarkNormalizedData`：分钟行会自然按时间戳绘制；基准（`000001.SS` 日线）仍按“天”对齐 forward-fill，MVP 可接受。

### 5.3 `backend/api/data_api.py`

`handle_symbol_files`（L287）是回测页实际调用的数据接口，需透传 `interval`：

- **GET**：`interval = request.args.get('interval', '1d')`，传给 `dm.get_file_info(symbol, interval=interval)`；
- **POST**：`interval = data.get('interval', '1d')`，传给 `dm.fetch_data(..., interval=interval)`；
- 响应体新增 `'interval': interval`；
- 新增/扩展校验入口：当 `data_source` 与 `interval` 组合不被支持时，`dm.fetch_data` 抛错 → 该接口返回 400 + 明确 message（前端 `syncMarketData` 已有 `showAlert(error)`，能直接展示）。

其余数据接口（`/files`、`/template`、`create_file`、`/symbols/catalog`、`/live`）**不改**，保证 trader 页与 agent 工具不受影响。

### 5.4 `backend/core/data_manager.py`（核心）

新增一个周期常量与校验：

```
ALLOWED_INTERVALS = {'1d','5m','15m','60m','1m'}
SUPPORTED = {
    'yfinance':  {'1d'},
    'baostock':  {'5m','15m','60m'},
    'miniqmt':   {'1m','5m','15m','60m'},
}
```

改动点：

1. **`_expected_filename(symbol, interval)`**（新增私有方法）
   - `1d → <SYMBOL>.csv`
   - 其余 → `<SYMBOL>_<interval>.csv`
   - `SYMBOL` 用现有 `_canonicalize_symbol` 归一化。
2. **`fetch_data(..., interval='1d')`**（L111）
   - 归一化 `interval = (interval or '1d').lower()`；
   - 校验：不在 `ALLOWED_INTERVALS` 或不在 `SUPPORTED[source]` → 抛 `ValueError`（含可读信息，如 “baostock 支持 5m/15m/60m”）；
   - 本地命中判断：对 `1d` 沿用现有 `find_latest_file` 前缀逻辑（向后兼容）；对 **分钟周期只用 `_expected_filename` 精确匹配**，避免跨周期误命中；
   - 拉取：`fetcher.fetch_data(..., interval=interval)`（替换 L177 写死的 `'1d'`）；
   - 标准化：`_standardize_dataframe` 按 interval 区分 `Date` 格式（`1d` 保持 `%Y-%m-%d`；分钟用 `%Y-%m-%d %H:%M:%S`）；
   - 保存：用 `_expected_filename` 生成文件名；
   - 返回的 `filename` 即周期专属文件，供回测读取。
3. **`get_file_info(symbol, interval='1d')`**（L73）
   - `1d`：保持原 `find_latest_file` 行为；
   - 分钟：只查 `_expected_filename(symbol, interval)`，命中才返回；
   - 返回体带上 `interval`。
4. **`_standardize_dataframe`**（L87）增加 `interval` 形参决定日期粒度。

> 说明：`find_latest_file` 本身不动，只是分钟周期不走它。

### 5.5 `backend/core/backtest_engine.py`

`load_and_prepare_data`（L100）**只需一处区间过滤修正**：

现状（L141）：

```python
start = pd.to_datetime(start_date)
end   = pd.to_datetime(end_date)
df = df[(df.index >= start) & (df.index <= end)]
```

分钟数据中 `end_date=2026-10-08` 会被解析成 `2026-10-08 00:00:00`，导致结束日当天的所有分钟 bar 被排除。

修正为“结束日含当天全天”（对日线同样安全）：

```python
start = pd.to_datetime(start_date)
end   = pd.to_datetime(end_date) + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
df = df[(df.index >= start) & (df.index <= end)]
```

其余（读 `Date` 建 `DatetimeIndex`、取 `Close`、调 `strategy.generate_signals`）**不改**——分钟数据天然可走通。`run_backtest_from_file` 与 `run_backtest` 无需改动。

### 5.6 `backend/api/backtest_api.py`

`run_backtest`（L55）：

- 读取 `payload.get('interval', '1d')`；
- 把 `interval` 写进持久化的 `result_data`（`_DATA_RESULTS_FOLDER` 的 JSON）与响应体，便于后续追溯；
- 不需要把 `interval` 传进引擎（引擎是“读文件回放”，数据本身已带时间戳）；
- 结果文件名里的 `result_id` 已含时间戳，天然不会与日线结果冲突；如需更强隔离，可在 `result_id` 追加 `_{interval}`（可选，默认不加以保持最小改动）。

## 6. 兼容性与风险

- **默认值全部为 `1d`**：前端、API、`fetch_data`、`get_file_info` 缺省都是 `1d`，老调用（trader 页、agent 工具、旧书签）行为完全不变。
- **日线文件与日期格式不动**：`<SYMBOL>.csv` 与 `%Y-%m-%d` 保持原样。
- **已知偏差（可接受，MVP 不修）**：
  - 年化/夏普等指标按 bar 数粗算，分钟下语义偏移；
  - 基准 `000001.SS` 仍按日线对齐分钟结果；
  - 未做整手取整、未做强制收盘平仓；
  - 现有策略（如 BOLL 的 `period=10`）是按“天”调参，用在分钟上会频繁触发——属于策略参数问题，不在本次范围。
- **1m 依赖 miniQMT**：`miniqmt` 需要本地安装并运行 miniQMT 与 `xtquant`；未就绪时后端会返回明确错误，前端 `syncMarketData` 的 `showAlert` 直接展示，不新增处理。
- **跨周期文件互不干扰**：靠“精确文件名匹配”而非前缀/最近修改，杜绝日线与分钟互相覆盖。

## 7. 改动清单（文件级）

| 文件 | 改动 | 规模 |
|---|---|---|
| `frontend/templates/backtest.html` | 新增 `#backtestInterval` 选择器 | 小（加一个 select） |
| `frontend/static/js/backtest.js` | `getBacktestParams` / `syncMarketData` / `runBacktest` 透传 `interval` | 小（3 处） |
| `backend/api/data_api.py` | `handle_symbol_files` GET/POST 透传 `interval` + 校验 | 中 |
| `backend/core/data_manager.py` | 周期常量与校验、`_expected_filename`、`fetch_data`/`get_file_info`/`_standardize_dataframe` 周期化 | 核心 |
| `backend/core/backtest_engine.py` | `load_and_prepare_data` 结束日全天包含修正 | 极小（1 行语义） |
| `backend/api/backtest_api.py` | 读取并持久化 `interval` 元数据 | 小 |

> 不需要改动：`strategy_loader.py`、`deltafq` 安装包、`live_data_manager.py`、trader 页、agent 工具。

## 8. 验收标准

1. **日线不变**：默认 `1d` 时，`/strategy` 的拉数与回测行为、文件、日期格式与现状一致。
2. **分钟可用**：选 `baostock + 5m/15m/60m`（或 `miniqmt + 1m`）+ 一个 T+0 ETF 标的，点击运行回测，后端能生成 `<SYMBOL>_<interval>.csv`，`/api/backtests` 按分钟 bar 回放并返回 `trades_df / values_df / metrics`。
3. **校验生效**：`yfinance + 5m`、`baostock + 1m` 等非法组合，后端返回 400 且前端弹出对应错误。
4. **结束日包含**：分钟回测时，结束日当天的最后几根 bar 不被 `load_and_prepare_data` 漏掉。
5. **结果可追溯**：回测结果 JSON 内含 `interval` 字段。
6. **零 site-packages 改动**：以上全部在应用代码内完成，不碰 `deltafq`。
