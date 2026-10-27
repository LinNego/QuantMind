-- QuantDB 数据视图（DuckDB 2.x）
-- 用法：  duckdb -init research/qdb.sql
-- 进去之后：  .tables   /   .schema   /   SELECT * FROM feat LIMIT 5;
-- 每条视图都是懒加载，建视图不读数据，查了才读。

-- 1 kline data
CREATE OR REPLACE VIEW daily_backward AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/1_kline_data/daily_backward/**/*.parquet');
CREATE OR REPLACE VIEW daily_forward AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/1_kline_data/daily_forward/**/*.parquet');
CREATE OR REPLACE VIEW daily_unadjusted AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/1_kline_data/daily_unadjusted/**/*.parquet');
CREATE OR REPLACE VIEW index_daily AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/1_kline_data/index_daily/**/*.parquet');

-- 2 base sector
CREATE OR REPLACE VIEW index_weights AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/2_base_sector/index_weights/**/*.parquet');
CREATE OR REPLACE VIEW instrument_detail AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/2_base_sector/instrument_detail/**/*.parquet');
CREATE OR REPLACE VIEW margin_trading AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/2_base_sector/margin_trading/**/*.parquet');
CREATE OR REPLACE VIEW sector_concept AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/2_base_sector/sector_concept/**/*.parquet');
CREATE OR REPLACE VIEW trading_calendar AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/2_base_sector/trading_calendar/**/*.parquet');

-- 3 financial data
CREATE OR REPLACE VIEW balance AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/3_financial_data/balance/**/*.parquet');
CREATE OR REPLACE VIEW capital AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/3_financial_data/capital/**/*.parquet');
CREATE OR REPLACE VIEW cashflow AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/3_financial_data/cashflow/**/*.parquet');
CREATE OR REPLACE VIEW dividend_factors AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/3_financial_data/dividend_factors/**/*.parquet');
CREATE OR REPLACE VIEW holder_num AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/3_financial_data/holder_num/**/*.parquet');
CREATE OR REPLACE VIEW income AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/3_financial_data/income/**/*.parquet');
CREATE OR REPLACE VIEW per_share AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/3_financial_data/per_share/**/*.parquet');
CREATE OR REPLACE VIEW pershare_index AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/3_financial_data/pershare_index/**/*.parquet');

-- 4 bond etf
CREATE OR REPLACE VIEW convertible_bond AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/4_bond_etf/convertible_bond/**/*.parquet');
CREATE OR REPLACE VIEW etf_pcf AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/4_bond_etf/etf_pcf/**/*.parquet');

-- 5 technical derived
CREATE OR REPLACE VIEW market_sentiment AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/5_technical_derived/market_sentiment/**/*.parquet');
CREATE OR REPLACE VIEW technical_indicators AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/5_technical_derived/technical_indicators/**/*.parquet');
CREATE OR REPLACE VIEW valuation AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/5_technical_derived/valuation/**/*.parquet');

-- 6 ml datasets
CREATE OR REPLACE VIEW features_daily AS SELECT * FROM read_parquet('/opt/quantmind/data/quantdb/6_ml_datasets/features_daily/**/*.parquet');

-- ===== 短别名（少打字）=====
CREATE OR REPLACE VIEW feat AS SELECT * FROM features_daily;
CREATE OR REPLACE VIEW kf   AS SELECT * FROM daily_forward;
CREATE OR REPLACE VIEW ku   AS SELECT * FROM daily_unadjusted;
CREATE OR REPLACE VIEW kb   AS SELECT * FROM daily_backward;
CREATE OR REPLACE VIEW val  AS SELECT * FROM valuation;
CREATE OR REPLACE VIEW ti   AS SELECT * FROM technical_indicators;
CREATE OR REPLACE VIEW idx  AS SELECT * FROM index_daily;
