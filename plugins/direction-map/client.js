/**
 * 方向地图（Direction Map）Client 半。
 *
 * 注册在设置页里的一整页应用：输入股票代码 → 直接看到三层证据
 * （钱从哪来 / 钱投向哪 / 它自己说）和方向信号。
 * 只使用 --dsw-alias-* 主题变量，跟随 Harness 明暗主题。
 */
window.__ModuleLoader__.load({
  id: '@local/direction-map',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const { useState, useEffect } = React

    const toneColor = {
      ok: 'var(--dsw-alias-state-success-primary)',
      warn: 'var(--dsw-alias-state-warn-primary)',
      bad: 'var(--dsw-alias-state-error-primary)',
      info: 'var(--dsw-alias-brand-primary)',
    }

    function yi(v, digits = 1) {
      if (v === null || v === undefined || Number.isNaN(v)) return '—'
      const n = v / 1e8
      return Math.abs(n) >= 10000 ? `${(n / 10000).toFixed(2)}万亿` : `${n.toFixed(digits)}亿`
    }
    function pct(v, digits = 1) {
      if (v === null || v === undefined || Number.isNaN(v)) return '—'
      return `${(v * 100).toFixed(digits)}%`
    }
    function signed(v, digits = 1) {
      if (v === null || v === undefined || Number.isNaN(v)) return '—'
      return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
    }

    const card = {
      border: '1px solid var(--dsw-alias-border-l1)',
      borderRadius: 10,
      padding: '14px 16px',
      background: 'var(--dsw-alias-bg-layer-1)',
      marginBottom: 12,
    }
    const label = { color: 'var(--dsw-alias-label-secondary)', fontSize: 12, marginBottom: 6 }
    const big = { fontSize: 22, fontWeight: 600, color: 'var(--dsw-alias-label-primary)' }
    const row = { display: 'flex', gap: 24, flexWrap: 'wrap' }
    const kv = { minWidth: 120, marginBottom: 10 }
    const small = { fontSize: 12, color: 'var(--dsw-alias-label-secondary)' }

    function Stat({ name, value, note }) {
      return h('div', { style: kv },
        h('div', { style: small }, name),
        h('div', { style: { fontSize: 15, fontWeight: 500, color: 'var(--dsw-alias-label-primary)' } }, value),
        note ? h('div', { style: small }, note) : null)
    }

    function DirectionMap() {
      const [symbol, setSymbol] = useState('601012.SH')
      const [data, setData] = useState(null)
      const [error, setError] = useState('')
      const [loading, setLoading] = useState(false)

      async function load(code) {
        const target = (code || symbol).trim().toUpperCase()
        if (!target) return
        setLoading(true)
        setError('')
        try {
          const res = await fetch(`/direction-map/query?symbol=${encodeURIComponent(target)}`)
          const body = await res.json()
          if (body.error) { setError(body.error); setData(null) } else { setData(body) }
        } catch (err) {
          setError(`请求失败：${err && err.message ? err.message : err}`)
        } finally {
          setLoading(false)
        }
      }

      useEffect(() => { load('601012.SH') }, [])

      const rev = data && data.revenue
      const cap = data && data.capital
      const pm = data && data.product_mix

      return h('div', { style: { padding: '20px 24px', maxWidth: 1020, overflowY: 'auto', height: '100%' } },
        h('div', { style: { marginBottom: 16 } },
          h('div', { style: { fontSize: 18, fontWeight: 600, color: 'var(--dsw-alias-label-primary)' } }, '公司方向地图'),
          h('div', { style: small }, '把钱、产能和管理层的话三层对齐，看这家公司正在变成什么')),

        h('div', { style: { display: 'flex', gap: 8, marginBottom: 16 } },
          h('input', {
            value: symbol,
            onChange: (e) => setSymbol(e.target.value),
            onKeyDown: (e) => { if (e.key === 'Enter') load() },
            placeholder: '600000.SH',
            style: {
              flex: '0 0 180px', padding: '8px 10px', borderRadius: 8, fontSize: 14,
              border: '1px solid var(--dsw-alias-border-l2)',
              background: 'var(--dsw-alias-bg-base)', color: 'var(--dsw-alias-label-primary)',
            },
          }),
          h('button', {
            onClick: () => load(),
            disabled: loading,
            style: {
              padding: '8px 16px', borderRadius: 8, fontSize: 14, cursor: 'pointer',
              border: 'none', background: 'var(--dsw-alias-brand-primary)', color: '#fff',
              opacity: loading ? 0.6 : 1,
            },
          }, loading ? '分析中…' : '生成方向地图')),

        error ? h('div', { style: { ...card, color: 'var(--dsw-alias-state-error-primary)' } }, error) : null,

        data && rev ? h('div', {},
          h('div', { style: { marginBottom: 10, fontSize: 15, fontWeight: 600, color: 'var(--dsw-alias-label-primary)' } },
            `${data.name} ${data.symbol}`,
            h('span', { style: { ...small, marginLeft: 8 } }, `${data.industry || ''} · 最新报告期 ${rev.latest_period || '—'}`)),

          h('div', { style: card },
            h('div', { style: label }, '① 钱从哪来（TTM）'),
            h('div', { style: row },
              h('div', {},
                h('div', { style: big }, yi(rev.revenue_ttm)),
                h('div', { style: small }, '营业收入')),
              h('div', {},
                h('div', { style: big }, yi(rev.net_profit_ttm)),
                h('div', { style: small }, '归母净利润')),
              h('div', {},
                h('div', { style: big }, yi(rev.operating_cash_flow_ttm)),
                h('div', { style: small }, '经营现金流'))),
            h('div', { style: { ...row, marginTop: 12 } },
              h(Stat, { name: '单季营收同比', value: signed(rev.revenue_yoy) }),
              h(Stat, { name: '单季营收环比', value: signed(rev.revenue_qoq) }),
              h(Stat, { name: '毛利率(YTD)', value: pct(rev.gross_margin_latest), note: `同比 ${(rev.gross_margin_yoy_pp || 0).toFixed(1)}pp` }),
              h(Stat, { name: '净利率(YTD)', value: pct(rev.net_margin_latest) }),
              h(Stat, { name: '研发强度', value: pct(rev.research_intensity_ttm) }))),

          h('div', { style: card },
            h('div', { style: label }, '② 钱投向哪'),
            h('div', { style: row },
              h(Stat, { name: '在建工程', value: yi(cap.construction_in_progress), note: `同比 ${signed(cap.construction_yoy)}` }),
              h(Stat, { name: '固定资产', value: yi(cap.fixed_assets), note: `同比 ${signed(cap.fixed_assets_yoy)}` }),
              h(Stat, { name: '存货', value: yi(cap.inventory), note: `同比 ${signed(cap.inventory_yoy)} · 周转 ${cap.inventory_days ? cap.inventory_days.toFixed(0) : '—'} 天` }),
              h(Stat, { name: '应收账款', value: yi(cap.receivables), note: `同比 ${signed(cap.receivables_yoy)}` })),
            h('div', { style: { ...row, marginTop: 12 } },
              h(Stat, { name: '货币资金', value: yi(cap.cash) }),
              h(Stat, { name: '有息负债', value: yi(cap.interest_bearing_debt) }),
              h(Stat, { name: '净现金', value: yi(cap.net_cash) }),
              h(Stat, { name: '资产负债率', value: pct(cap.debt_ratio) }),
              h(Stat, { name: '利息保障', value: cap.interest_coverage ? cap.interest_coverage.toFixed(1) : '—' }))),

          h('div', { style: card },
            h('div', { style: label }, '③ 它自己说（年报分产品收入）'),
            pm && pm.product_mix && pm.product_mix.length
              ? h('div', {},
                h('div', { style: { ...small, marginBottom: 8 } }, `${pm.report_year} 年年报 · ${pm.source}`),
                pm.product_mix.map((p) => h('div', {
                  key: p.name,
                  style: { display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 },
                },
                  h('div', { style: { width: 120, fontSize: 13, color: 'var(--dsw-alias-label-primary)' } }, p.name),
                  h('div', { style: { flex: 1, height: 8, background: 'var(--dsw-alias-bg-layer-2)', borderRadius: 4 } },
                    h('div', {
                      style: {
                        width: `${Math.max(1, (p.share || 0) * 100)}%`, height: '100%', borderRadius: 4,
                        background: 'var(--dsw-alias-brand-primary)',
                      },
                    })),
                  h('div', { style: { width: 150, textAlign: 'right', fontSize: 12, color: 'var(--dsw-alias-label-secondary)' } },
                    `${yi(p.revenue)} · ${pct(p.share, 1)} · 毛利 ${p.gross_margin.toFixed(1)}%`))))
              : h('div', { style: small }, '年报未抓取。运行 pdf_facts.py 后此处显示分产品收入结构。')),

          h('div', { style: card },
            h('div', { style: label }, '方向信号'),
            (data.signals || []).map((s) => h('div', {
              key: s.key,
              style: { display: 'flex', gap: 10, alignItems: 'baseline', marginBottom: 6 },
            },
              h('span', {
                style: {
                  fontSize: 12, padding: '1px 8px', borderRadius: 999,
                  color: toneColor[s.tone] || 'var(--dsw-alias-label-secondary)',
                  border: `1px solid ${toneColor[s.tone] || 'var(--dsw-alias-border-l1)'}`,
                },
              }, s.key),
              h('span', { style: { fontSize: 13, color: 'var(--dsw-alias-label-primary)' } }, s.detail))))) : null)
    }

    function PvChain() {
      const [data, setData] = useState(null)
      const [error, setError] = useState('')
      const [loading, setLoading] = useState(false)

      async function load() {
        setLoading(true)
        setError('')
        try {
          const res = await fetch('/direction-map/chain')
          const body = await res.json()
          if (body.error) { setError(body.error); setData(null) } else { setData(body) }
        } catch (err) {
          setError(`请求失败：${err && err.message ? err.message : err}`)
        } finally {
          setLoading(false)
        }
      }
      useEffect(() => { load() }, [])

      function tone(gm) {
        if (gm === null || gm === undefined) return 'var(--dsw-alias-label-secondary)'
        if (gm < 0) return 'var(--dsw-alias-state-error-primary)'
        if (gm < 0.05) return 'var(--dsw-alias-state-warn-primary)'
        if (gm >= 0.2) return 'var(--dsw-alias-state-success-primary)'
        return 'var(--dsw-alias-label-primary)'
      }

      const th = { fontSize: 12, color: 'var(--dsw-alias-label-secondary)', textAlign: 'right', padding: '6px 8px' }
      const td = { fontSize: 13, textAlign: 'right', padding: '6px 8px', color: 'var(--dsw-alias-label-primary)' }

      const COLS = '1.1fr 1.5fr 1fr 1fr 1fr 1fr 1fr'
      const border = '1px solid var(--dsw-alias-border-l1)'

      const titleRow = h('div', { style: { display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 4 } },
        h('div', { style: { fontSize: 18, fontWeight: 600, color: 'var(--dsw-alias-label-primary)' } }, '光伏产业链全景'),
        h('div', { style: small }, loading ? '加载中…' : `${data ? data.rows.length : 0} 家 · 按主营收入构成分层`),
        h('button', {
          onClick: load,
          style: {
            marginLeft: 'auto', padding: '4px 12px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
            border: '1px solid var(--dsw-alias-border-l2)', background: 'transparent',
            color: 'var(--dsw-alias-label-primary)',
          },
        }, '刷新'))

      const noteRow = h('div', { style: { ...small, marginBottom: 14 } },
        '分层依据写在每行 title 里；隆基已按年报「组件及电池占 85.2%」归入组件（一体化），不再按行业标签归硅片。')

      const errorBox = error
        ? h('div', { style: { ...card, color: 'var(--dsw-alias-state-error-primary)' } }, error)
        : null

      const layerCards = !data ? null : h('div', { style: { display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 14 } },
        data.layers.map((lay) => h('div', {
          key: lay.layer,
          style: { ...card, flex: '1 1 150px', marginBottom: 0, padding: '10px 12px' },
        },
          h('div', { style: { fontSize: 12, color: 'var(--dsw-alias-label-secondary)' } }, lay.layer),
          h('div', { style: { fontSize: 16, fontWeight: 600, color: 'var(--dsw-alias-label-primary)' } },
            yi(lay.revenue_ttm, 0)),
          h('div', { style: { fontSize: 11, color: 'var(--dsw-alias-label-secondary)', marginTop: 2 } },
            `${lay.count} 家 · 毛利中位 ${pct(lay.median_gross_margin)} · 亏损 ${lay.losing_count}`))))

      const headerCells = ['公司', '分层', 'TTM营收', '毛利率', '营收同比', '固资同比', '经营现金流'].map((t, i) =>
        h('div', { key: t, style: { ...th, textAlign: i < 2 ? 'left' : 'right' } }, t))

      const tableHeader = h('div', {
        style: { display: 'grid', gridTemplateColumns: COLS, borderBottom: border },
      }, headerCells)

      const tableRows = !data ? [] : data.rows.map((r) => h('div', {
        key: r.symbol,
        title: `${r.basis || ''}${r.top_product ? `｜最大分产品：${r.top_product} ${pct(r.top_product_share)}` : ''}`,
        style: { display: 'grid', gridTemplateColumns: COLS, borderBottom: border },
      },
        h('div', { style: { ...td, textAlign: 'left' } },
          h('div', {}, r.name,
            h('span', { style: { ...small, marginLeft: 4 } }, r.symbol.slice(0, 6))),
          r.top_product
            ? h('div', { style: { fontSize: 11, color: 'var(--dsw-alias-label-secondary)', marginTop: 1 } },
              `${r.top_product} ${pct(r.top_product_share)}`)
            : h('div', { style: { fontSize: 11, color: 'var(--dsw-alias-state-idle-primary)', marginTop: 1 } }, '年报未解析')),
        h('div', { style: { ...td, textAlign: 'left', color: 'var(--dsw-alias-label-secondary)' } }, r.layer),
        h('div', { style: td }, yi(r.revenue_ttm, 0)),
        h('div', { style: { ...td, color: tone(r.gross_margin), fontWeight: 500 } }, pct(r.gross_margin)),
        h('div', { style: { ...td, color: r.revenue_yoy < 0 ? 'var(--dsw-alias-state-warn-primary)' : 'var(--dsw-alias-label-primary)' } },
          signed(r.revenue_yoy)),
        h('div', { style: { ...td, color: 'var(--dsw-alias-label-secondary)' } }, signed(r.fixed_assets_yoy)),
        h('div', { style: { ...td, color: r.operating_cash_flow_ttm < 0 ? 'var(--dsw-alias-state-error-primary)' : 'var(--dsw-alias-label-primary)' } },
          yi(r.operating_cash_flow_ttm, 0))))

      const table = !data ? null : h('div', { style: { ...card, padding: '4px 8px' } }, tableHeader, tableRows)

      return h('div', { style: { padding: '20px 24px', maxWidth: 1080, overflowY: 'auto', height: '100%' } },
        titleRow,
        noteRow,
        errorBox,
        data ? h('div', {}, layerCards, table) : null)
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        // 会话区顶部页签（挨着「对话」「轨迹」）：主入口，最容易被找到
        ctx.slots.inject('conversation.view', () => ctx.slots.register({
          name: 'conversation.view',
          id: 'direction-map',
          order: 20,
          label: () => '方向地图',
        }, DirectionMap))

        ctx.slots.inject('conversation.view', () => ctx.slots.register({
          name: 'conversation.view',
          id: 'direction-map-chain',
          order: 21,
          label: () => '光伏产业链',
        }, PvChain))

        // 设置页里也留一个整页入口，方便单独打开
        ctx.slots.inject('settings.section', () => ctx.slots.register({
          name: 'settings.section',
          id: 'direction-map',
          order: 40,
          label: () => '方向地图',
        }, DirectionMap))
      },
    }
  },
})
