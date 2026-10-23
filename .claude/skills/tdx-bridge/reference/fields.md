# 字段与口径（2026-10-23 实测）

> 唯一出处：`tools/bridge-windows/src/market/schema.py` 的 `FIELD_DEFS` / `SOURCE_DEFS`。
> 统一行情只输出下表的 20 个字段；缺失一律 `null` 并进 `missing`，**绝不填 0**。

## 1. 统一字段（`/api/v1/market/quote`）

| 字段 | 中文 | 单位 | 说明 | 源优先级 |
|---|---|---|---|---|
| `last` | 最新价 | 元 | 最后一笔 | ths → aidata → tdx |
| `prev_close` | 昨收 | 元 | | ths → aidata → tdx |
| `open` | 今开 | 元 | | ths → aidata → tdx |
| `high` / `low` | 最高/最低 | 元 | | ths → aidata → tdx |
| `volume` | 成交量 | 手 | THS `quotes_ext` 无此字段，实际多由 TDX 提供 | ths → aidata → tdx |
| `amount` | 成交额 | 元 | TDX 原始万元已 ×10000 | ths → aidata → tdx |
| `change_pct` | 涨跌幅 | % | 源内直取或本地算 | ths → aidata → tdx |
| `bids` / `asks` | 买/卖盘 | 档位数组 | `[{level, price, volume}]`，volume=**手** | ths → aidata → tdx |
| `inside` / `outside` | 内盘/外盘 | 手 | TDX 快照 | aidata → tdx |
| `main_inflow` | 主力净流入 | 元 | THS 0xc4 金额表 | ths |
| `dde_main` | DDE 主力 | 亿 | THS dt248 | ths |
| `market_cap` | 总市值 | 元 | THS dt202 | ths |
| `seal_amount` | 封单额 | 元 | THS dt44 | ths |
| `auction_chg_pct` | 竞价涨幅 | % | THS 统一列表派生 | ths |
| `auction_amount` | 竞价金额 | 元 | THS 统一列表派生 | ths |
| `speed_4m` | 4分钟涨速 | % | THS 统一列表派生 | ths |

## 2. 源原始字段

### TDX `get_market_snapshot`（26）
`Now` `LastClose` `Open` `Max` `Min` `Volume`(手) `Amount`(**万元**) `Average`
`NowVol` `TickDiff` `Zangsu` `ZAFPre3` `Before5MinNow` `Inside` `Outside`
`Buyp/Buyv`(5档，手) `Sellp/Sellv`(5档，手) `UpHome` `DownHome` `InOutFlag`
`ItemNum` `Jjjz` `XsFlag` `ErrorId`

### TDX `get_more_info`（60+，节选）
`DynaPE` `DYRatio` `BetaValue` `Ltsz` `MA5Value` `HisHigh/HisLow` `IPO_Price`
`MorePE` `MainBusiness` `RDInputFee` `OpenZAF` `OpenAmo` `PreMinum`
`SafeValue/ShapeValue/ShineValue` `BCancel/SCancel` `L2OrderNum/L2TicNum`
`EverZTCount` `LastStartZT` `DTDate_Recent/DTPrice` `RecentDZDate/RecentGGJYDate/
RecentHGDate/RecentReleaseDate` `ReportDate` `HqDate` `IsKzz/IsT0Fund/IsZCZGP`

### TDX `get_stock_info`（60+，节选）
`Name` `ActiveCapital` `MinPrice` `BelongHS300/HSGT/RZRQ` `IsSTGP/IsQuitGP`
`J_zgb/J_zzc/J_jzc/J_jly/J_yysy/J_jyxjl/J_mgsy/J_mgjzc/J_gdqyb/J_gdrs/
J_yszk/J_wxzc/J_yycb/J_ldzc/J_ldfz/J_zbgjj`

### TDX 其他
- `get_pricevol`: `LastClose/Now/Volume/Zaf`
- `get_market_data`（日线）: `Open/High/Low/Close/Volume/Amount/VolInStock/ForwardFactor/Date/Time`
- `get_exday_data`: `Amo[4][4]` 四档价位资金分布 + `BCancel/SCancel`
- `get_zdt_data`: `ZDTStatusNow/Ori` `VolZT` `OpenTimesZT/DT` `FirstTimeZT/DT`
  `LastTimeZT/DT` `LastOpenTimeZT/DT` `FDVolMaxZT/DT`
- `get_divid_factors`: `Type/Bonus/ShareBonus/Allotment/AllotPrice`
- `get_gb_info`: `Date/Zgb/Ltgb`
- `get_relation`: `BlockCode/BlockName/BlockType/GPNume`（所属板块）

### THS `quotes_ext`（10 个具名字段）
`price` `amount`(元) `chg_pct` `auction_chg_pct` `auction_amount`
`speed_4m` `main_inflow`(元) `dde_main`(亿) `market_cap`(元) `code`

### THS `quote`（原生 dt 码）
`dt6`=昨收、`dt7`=最低、`dt10`=最高、`dt13`=成交量(**股**)、`dt48`=涨跌幅；
`dt17` `dt49` `dt66` `dt87_a` `dt87_b` 含义待补字典。

### THS `depth`
`buy/sell`: `[{level(买一…), price, qty(**股**), amount}]`
`seal_amount` `seal_type` `fields`（原生 dt13/dt24–dt35 五档…）

### THS `timeline`（169 点）
`minute_index` `dt10` `dt13` `dt14` `dt15` `dt19` `dt22` `dt23` `dt54`
`dt201`–`dt210` `buy_force` `sell_force` `net_force`

### THS 板块成分 `board/{id}/constituents`
`code` `chg_pct` `dt6` `dt7` `dt8` `dt9` `dt10` `dt13`(**股**) `dt19`
`dt45` `dt48` `dt66` `dt223`–`dt226`

### TdxAiData 独有
- `get_minute_data`: `Time/Price/Average/Volume`（当日 169 根）
- `get_tick_data`: `Time/Price/Volume/BSFlag/TotalNum`（分页）
- `get_call_auction`: `Time/Price/Volume/LeaveQty`（3 秒切片）
- `get_domain_sorthq`: `Code/Name/Now/Close/ZAF/Volume`

## 3. 单位（最容易错的地方）

| 数据 | 原始单位 | 统一后 |
|---|---|---|
| TDX `Amount` | 万元 | 元（×10000，四舍五入到分） |
| TDX `Volume` | 手 | 手 |
| TDX `Buyp/Buyv` | 手 | 手 |
| THS `quotes_ext.volume` | 手 | 手 |
| THS `depth.qty` / `board dt13` | **股** | 手（÷100） |
| 所有价格 | 元 | 元 |

## 4. 数据源能力边界（standard 非 L2 账号实测）

| 项 | 状态 |
|---|---|
| THS 10 档盘口 | 403 `l2_snapshot_push` |
| THS `stock-ready` | 403 `l2_market_access` |
| THS `order-queues` | 403 `l2_timeline` |
| THS 竞价/历史分时/热榜/板块报价 | 上游超时 → 30s 504 + 自愈 |
| TDX 分钟线 1m/5m | 0 行 |
| TDX pro-data GP/BK/SC/GO | `json has no table_list`（缺数据包） |
| aidata `get_pricevol` | DLL 无实现 |
| THS `market_snapshot` | `[]` |

## 5. 对账口径

- **盘中**：600519 三源最新价差 0.013%、成交额差 0.007%、成交量一致。
- **收盘后**：TDX 与 THS 成交额可差 0.02%–6%（收盘集合竞价纳入不同），
  此时只对比价格，不拿成交量/额做硬判据。
- 统一行情是**逐字段合并**：同一响应里不同字段可能来自不同源，看 `source`。
