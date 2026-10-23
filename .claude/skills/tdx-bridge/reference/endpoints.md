# 桥接口清单（2026-10-23 实测）

> 统一入口：`http://<windows-ip>:8550`；全部端点（除 health/ui/cache）需要
> `Authorization: Bearer <token>`。错误统一为
> `{"success": false, "error": {"code", "message", "details"}}`。
> 限流：未鉴权 600/分（5 次失败封 IP 30s）；已鉴权 6000/分；请求体 ≤512KB。

## 1. 统一行情（推荐先用这组）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/market/quote?code=600519.SH` | 三源并行 + 逐字段合并。可选 `prefer=ths,aidata,tdx`、`aidata=1` |
| GET | `/api/v1/market/raw?code=...&aidata=1` | 三源原始返回（测试台/排障） |
| GET | `/api/v1/market/fields` | 字段字典（含义/单位/可用源） |
| GET | `/api/v1/market/sources` | 各源调用数/成功率/延迟/最近错误 |
| GET | `/api/v1/ths/compare?code=...` | TDX 与 THS 同尺对照（两源同一套归一化） |

`quote` 返回：`data`（合并字段）、`source`（每字段来自哪个源）、`missing`、
`timings`（各源耗时/成败）、`fastest`。

## 2. 同花顺旁路（worker 46 端点，桥代理）

桥代理路径：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/ths/status` | 桥侧状态 + worker 状态 + `qr`（扫码状态/账号） |
| POST | `/api/v1/ths/call` | 通用透传 `{"method","params"}`（白名单 43 方法） |
| GET | `/api/v1/ths/quote?codes=600519` | 原始报价 |
| GET | `/api/v1/ths/depth/{code}?levels=5` | 盘口（10 档需 L2） |
| GET | `/api/v1/ths/flow?code=...` | 资金流（quotes_ext） |
| GET | `/api/v1/ths/dxjl?limit=20` | 短线精灵异动 |
| GET | `/api/v1/ths/boards` | 全部板块（2768 个） |
| POST | `/api/v1/ths/connect` | 触发 worker 连接 |
| POST | `/api/v1/ths/qr/start` / GET `/api/v1/ths/qr/status` / GET `/api/v1/ths/qr.png` | 扫码登录 |
| GET | `/api/v1/ths/gates`（未代理；用 `/ths/status`） | worker 闸门计数在 `/api/bridge/gates`（仅本机） |

`/ths/call` 可用的 worker 方法（`params` 即查询参数）：

| 类别 | 方法（path 模板） | 参数 |
|---|---|---|
| 状态 | `status` (`/api/status`) | — |
| 报价 | `quote` (`/api/quote`) / `quotes_ext` (`/api/quotes_ext`) | `codes`（逗号分隔，裸码） |
| 盘口 | `depth` (`/api/depth/{code}`) | `code`, `levels`(5), `market` |
| 盘口 | `market_view` / `market_view_fast` | `code`, `period/count/levels` |
| 分时 | `timeline` / `history_timeline` | `code`, `date` |
| K线 | `kline` | `code`, `period`(1min/5min/15min/30min/60min/day/week/month/quarter/year), `count`, `anchor`, `fuquan` |
| 竞价 | `auction` / `closing_auction` / `intraday_auctions` | `code`, `trade_date` |
| 盘中 | `intraday` | `code`, `trade_date` |
| 逐笔/盘口 | `superorder` / `superorder_window` / `superorder-replay` / `order-details` / `order-queues` | `code`, `start`, `end`（≤300s 窗）、`trade_date` |
| 异动 | `dxjl` / `dxjl/latest` | `pages`, `endtime` |
| 板块 | `boards` / `board_categories` / `hot_boards` / `dynamic_plates` | `category` |
| 板块 | `board_constituents` / `board_quotes` / `board_timeline` / `board_kline` / `board_auction` | `code`=板块 ID（如 `BC9E`） |
| 榜单 | `hot` / `dde_rank` / `stock_list_ranked` / `stocks` / `stocks2` | `count`, `sort_by`, `sort_dir`, `with_names` |
| 自选 | `self_stocks` / `groups` / `group` | `name` |
| 全市场 | `market_snapshot` | —（实测空） |

> 方法名在桥侧 `THS_METHODS` 里是 `board_constituents` 等下划线名；
> worker 路径里的 `-` 原样保留（如 `order-details`）。

## 3. TDX 免费通路（只读白名单）

`POST /api/v1/tdx/call`，body `{"method","params"}`。允许的方法：

`health_check` `health_check_fast` `get_market_snapshot` `get_market_data`
`get_latest_price` `get_stock_info` `get_more_info` `get_stock_list`
`get_sector_list` `get_stock_list_in_sector` `get_user_sector`
`get_match_stkinfo` `get_relation` `get_divid_factors` `get_gb_info`
`get_gb_info_by_date` `get_kzz_info` `get_ipo_info` `get_trackzs_etf_info`
`get_pricevol` `get_exday_data` `get_zdt_data` `get_trading_dates`
`get_financial_data(_by_date)` `get_gpjy_value(_by_date)`
`get_bkjy_value(_by_date)` `get_scjy_value(_by_date)` `get_gp_one_data`

写操作（`exec_to_tdx`/`send_file`/`formula_set_data`/`create_sector`）
与交易（`order_stock`/`cancel_order_stock`）一律 403。

常用参数：

| 方法 | params |
|---|---|
| `get_market_snapshot` | `stock_code`（**后缀式** `600519.SH`） |
| `get_market_data` | `stock_list:["600519.SH"]`, `period`(1d/1m/5m), `count`, `dividend_type` |
| `get_more_info` / `get_stock_info` | `stock_code` |
| `get_pricevol` | `stock_list:[...]` |
| `get_exday_data` | `stock_code`, `count` |
| `get_zdt_data` | `stock_list:[...]` |
| `get_relation` | `stock_code` |
| `get_divid_factors` | `stock_code`, `start_time`, `end_time` |
| `get_gb_info` | `stock_code`, `count` |
| `get_trading_dates` | `market`, `start_time`, `end_time`, `count` |
| `get_stock_list_in_sector` | `block_code`, `block_type`, `list_type` |

## 4. TdxAiData 付费通路

`POST /api/v1/aidata/call`（`params` 与 tqs 一致）。只读数 40+ 方法，常用：

| 方法 | params | 计费 |
|---|---|---|
| `get_market_snapshot` | `stock_code` | 1 词元 |
| `get_more_info` / `get_stock_info` | `stock_code` | 1 |
| `get_minute_data` | `stock_code`, `date` | 1 |
| `get_tick_data` | `stock_code`, `date`, `startxh`, `wantnum` | 1/50 行 |
| `get_call_auction` | `stock_code`, `type` | 1 |
| `get_market_data` | `stock_list`, `period`, `count` | 1 |
| `get_zdt_data` | `stock_list` | 1 |
| `get_gb_info` / `get_divid_factors` | `stock_code` | 1 |
| `get_relation` | `stock_code` | 1 |
| `get_domain_sorthq` | `domain`, `colname`, `wantnum` | 1 |
| `get_trading_dates` | `market`, `start_time`, `end_time` | 1 |

额度：`rows_per_token=200`（向上取整、最低 1）；每日硬预算 `daily_token_budget`（默认 500）；
状态与用量：`GET /api/v1/aidata/status`（含 `usage`、`stuck_for_s`）。
`get_pricevol` 在 DLL 中不可用。

## 5. 订阅 / 运维

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/v1/subscription` | 实时订阅状态/配置（tq 免费 ≤100；aidata 付费默认禁） |
| GET | `/api/v1/subscription/pushes` | 最近推送 |
| GET | `/api/v1/subscription/search?q=` | 代码模糊搜索 |
| POST | `/api/v1/subscription/import` | 一键导入持仓/委托（手动） |
| GET/POST | `/api/v1/subscription/auto` | 自动跟随实盘：GET 状态 / POST `{enabled,interval_s,mode,include_orders}` |
| GET | `/api/v1/subscription/live-codes?include_orders=1&limit=100` | 当前实盘代码（供订阅/同花顺跟随） |
| GET | `/api/v1/health` | 匿名；桥/TQ 健康 + 分槽占用 |
| GET | `/api/v1/stats` | **需鉴权**；账户/缓存/安全计数 |
| GET | `/api/v1/system` | **需鉴权**；CPU/内存/磁盘/线程/分槽 |
| GET | `/api/v1/cache/kline|get` | 匿名行情缓存 |
| GET | `/api/v1/cache/logs|trades` | 需鉴权（调用/交易台账） |

## 6. 错误与状态码

| HTTP | code | 含义 |
|---|---|---|
| 400 | `BAD_*` / `UNKNOWN_METHOD` | 参数/方法不合法 |
| 401 | `AUTH_FAILED` | token 缺失/错误 |
| 403 | `METHOD_FORBIDDEN` / `METHOD_NOT_ALLOWED` | 交易或写操作被拒 |
| 429 | `RATE_LIMITED` | 限流/封禁 |
| 502 | `THS_UNAVAILABLE` / `MARKET_UNAVAILABLE` | 上游失败/全源失败 |
| 503 | `THS_BUSY` / `THS_CIRCUIT_OPEN` / `WORKER_BUSY` | 饱和/熔断（聚合层会回落） |
| 504 | `worker_timeout` | worker 上游挂死，已触发自愈 |

> `THS_BUSY` 与 `THS_CIRCUIT_OPEN` 对统一行情无感（自动回落 TDX），
> 直连 `/ths/*` 时需要重试或等待。
