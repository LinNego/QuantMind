// 组合选股机构级统计探针（2026-10-21 细案 §7.8）
// 覆盖：数据端口徽标 / 已实现与浮动拆分 / 基准与超额 / 月度基准列 /
//       每列大盘（沪深300 当日 + 20 日线位置）/ 每格当日涨跌与当日盈亏与名单统计 /
//       每日批次与逐日净值明细 / 合规字眼（无买入卖出）。
// 只读：不确认调仓计划、不改本金、不触发任何委托。
// 凭据从环境变量读取，不落盘。
import { chromium } from 'playwright';

const BASE = process.env.QM_PROBE_BASE || 'http://localhost:3080';
const USER = process.env.QM_PROBE_USER;
const PASS = process.env.QM_PROBE_PASS;
if (!USER || !PASS) {
  console.error('[FATAL] 需要 QM_PROBE_USER / QM_PROBE_PASS');
  process.exit(2);
}

const results = [];
const errs = [];
const ok = (name, detail = '') => results.push({ name, pass: true, detail });
const bad = (name, detail = '') => results.push({ name, pass: false, detail });

const BENIGN_CONSOLE = [/ResizeObserver loop/i, /\[antd: /];

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 980 } });
page.on('pageerror', (e) => errs.push('[PAGEERROR] ' + (e.message || '').slice(0, 200)));
page.on('console', (m) => {
  if (m.type() !== 'error') return;
  const text = m.text();
  if (BENIGN_CONSOLE.some((re) => re.test(text))) return;
  errs.push('[console.error] ' + text.slice(0, 200));
});

const textOf = async (locator) =>
  ((await locator.innerText().catch(() => '')) || '').replace(/\n+/g, ' | ');

const waitForText = async (locator, pattern, timeout = 60000) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const text = await textOf(locator);
    if (pattern.test(text)) return text;
    await page.waitForTimeout(500);
  }
  return await textOf(locator);
};

try {
  // 1) 登录
  await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
  const inputs = page.locator('input');
  await inputs.nth(0).waitFor({ state: 'visible', timeout: 20000 });
  await inputs.nth(0).fill(USER);
  await inputs.nth(1).fill(PASS);
  await page.locator('button', { hasText: '登录' }).first().click();
  await page.waitForURL(/#\/(?!auth)/, { timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(2000);
  if (page.url().includes('/auth/login')) bad('登录', '仍停在登录页');
  else ok('登录');

  // 2) 进入推理中心 → 组合选股
  await page.goto(`${BASE}/#/inference-center`, { waitUntil: 'domcontentloaded' });
  const laterBtn = page.locator('button', { hasText: '稍后再答' }).first();
  if (await laterBtn.count()) {
    await laterBtn.click().catch(() => {});
    await page.waitForTimeout(600);
  }
  if (!(await page.locator('[data-testid="combo-workspace"]').count())) {
    await page
      .locator('button:has-text("组合选股"), [role="tab"]:has-text("组合选股"), .ant-tabs-tab:has-text("组合选股")')
      .first()
      .click()
      .catch(() => {});
    await page.locator('[data-testid="combo-workspace"]').waitFor({ timeout: 30000 }).catch(() => {});
  }
  if (await page.locator('[data-testid="combo-workspace"]').count()) ok('组合选股 Tab 渲染');
  else bad('组合选股 Tab 渲染');
  const ws = page.locator('[data-testid="combo-workspace"]');

  // 3) 选一个已保存组合（滚动建仓/周期收益只对已保存组合开口）
  const blendItems = page.locator('[data-testid^="combo-item-blend-"]');
  for (let i = 0; i < 40; i += 1) {
    if ((await blendItems.count()) > 0) break;
    if (/暂无已保存组合|读取失败/.test(await textOf(page.locator('body')))) break;
    await page.waitForTimeout(500);
  }
  const nBlends = await blendItems.count();
  if (nBlends === 0) {
    bad('存在已保存组合', '列表没有已保存组合，无法验证历史统计');
  } else {
    ok('存在已保存组合', `${nBlends} 个`);
    await blendItems.nth(0).click();
    await page.waitForTimeout(2500);
  }

  // 4) 周期收益 Tab
  if (nBlends > 0) {
    await page
      .locator('[role="tab"]:has-text("周期收益"), .ant-tabs-tab:has-text("周期收益")')
      .first()
      .click()
      .catch(() => {});
    await page.locator('[data-testid="combo-period-root"]').waitFor({ timeout: 30000 }).catch(() => {});
    const summary = page.locator('[data-testid="combo-period-summary"]');
    await summary.waitFor({ timeout: 90000 }).catch(() => {});
    if (await summary.count()) ok('周期收益指标条渲染');
    else bad('周期收益指标条渲染', (await textOf(ws)).slice(0, 300));

    if (await summary.count()) {
      const sourceBadge = page.locator('[data-testid="combo-period-source"]');
      const badgeText = await textOf(sourceBadge);
      const tagText = await textOf(page.locator('[data-testid="combo-period-source-tag"]'));
      const spanText = await textOf(page.locator('[data-testid="combo-period-source-span"]'));
      if (/测试集|推理/.test(tagText) && /\d{4}-\d{2}-\d{2}/.test(spanText)) {
        ok('周期收益 · 数据端口徽标', badgeText.slice(0, 120));
      } else {
        bad('周期收益 · 数据端口徽标', badgeText.slice(0, 200));
      }
      const bench = await textOf(page.locator('[data-testid="combo-period-source-benchmark"]'));
      if (/沪深300/.test(bench)) ok('周期收益 · 基准徽标', bench);
      else bad('周期收益 · 基准徽标', bench || '(缺失)');

      const realized = await textOf(page.locator('[data-testid="combo-period-realized"]'));
      const unrealized = await textOf(page.locator('[data-testid="combo-period-unrealized"]'));
      const toneOk = (v) => /^[+\-]?\d/.test(v) || v === '—';
      if (toneOk(realized) && toneOk(unrealized)) {
        ok('周期收益 · 已实现/浮动分栏', `已实现 ${realized} · 浮动 ${unrealized}`);
      } else {
        bad('周期收益 · 已实现/浮动分栏', `已实现 ${realized} · 浮动 ${unrealized}`);
      }

      const benchPct = await textOf(page.locator('[data-testid="combo-period-benchmark"]'));
      const excessPct = await textOf(page.locator('[data-testid="combo-period-excess"]'));
      if (toneOk(benchPct) && toneOk(excessPct)) {
        ok('周期收益 · 基准/超额', `基准 ${benchPct} · 超额 ${excessPct}`);
      } else {
        bad('周期收益 · 基准/超额', `基准 ${benchPct} · 超额 ${excessPct}`);
      }

      const months = await textOf(page.locator('[data-testid="combo-period-months"]'));
      if (/基准/.test(months) && /超额/.test(months) && /已实现|浮动/.test(months)) {
        ok('周期收益 · 月度表含基准/超额/盈亏拆分');
      } else {
        bad('周期收益 · 月度表含基准/超额/盈亏拆分', months.slice(0, 240));
      }

      // 快捷区间「全部」：pred 源必须回看到 2025 年
      const rangeBefore = await textOf(page.locator('[data-testid="combo-period-range"]'));
      await page.locator('[data-testid="combo-period-quick-all"]').click().catch(() => {});
      const rangeAfter = await waitForText(
        page.locator('[data-testid="combo-period-range"]'),
        /^2025-|^2024-/,
        120000,
      );
      if (/^测试集/.test(tagText)) {
        if (/^(2025|2024)-\d{2}-\d{2}/.test(rangeAfter)) {
          ok('快捷区间「全部」回看到 2025', rangeAfter.slice(0, 80));
        } else {
          bad('快捷区间「全部」回看到 2025', `${rangeBefore} → ${rangeAfter.slice(0, 80)}`);
        }
      } else {
        ok('快捷区间「全部」（runs 降级源）', `${rangeBefore} → ${rangeAfter.slice(0, 80)}`);
      }

      const chart = await page.locator('[data-testid="combo-period-chart"]').innerHTML().catch(() => '');
      if (chart.includes('#f59e0b')) ok('周期收益 · 基准曲线与组合同图');
      else bad('周期收益 · 基准曲线与组合同图', '未找到基准折线颜色');
    }
  }

  // 5) 滚动建仓 Tab
  if (nBlends > 0) {
    await page
      .locator('[role="tab"]:has-text("滚动建仓"), .ant-tabs-tab:has-text("滚动建仓")')
      .first()
      .click()
      .catch(() => {});
    await page.locator('[data-testid="combo-rolling-root"]').waitFor({ timeout: 30000 }).catch(() => {});
    const perf = page.locator('[data-testid="combo-rolling-performance"]');
    await perf.waitFor({ timeout: 120000 }).catch(() => {});
    if (await perf.count()) ok('滚动建仓指标条渲染');
    else bad('滚动建仓指标条渲染', (await textOf(ws)).slice(0, 300));

    if (await perf.count()) {
      const badge = await textOf(page.locator('[data-testid="combo-rolling-source"]'));
      if (/测试集|推理/.test(badge) && /本金/.test(badge)) ok('滚动建仓 · 数据端口+本金徽标', badge.slice(0, 120));
      else bad('滚动建仓 · 数据端口+本金徽标', badge.slice(0, 200));

      const realized = await textOf(page.locator('[data-testid="combo-rolling-realized"]'));
      const unrealized = await textOf(page.locator('[data-testid="combo-rolling-unrealized"]'));
      if (realized && unrealized && /^[+\-]?\d|—/.test(realized) && /^[+\-]?\d|—/.test(unrealized)) {
        ok('滚动建仓 · 已实现/浮动分栏', `已实现 ${realized} · 浮动 ${unrealized}`);
      } else {
        bad('滚动建仓 · 已实现/浮动分栏', `已实现 ${realized} · 浮动 ${unrealized}`);
      }
      const benchPct = await textOf(page.locator('[data-testid="combo-rolling-benchmark"]'));
      const excessPct = await textOf(page.locator('[data-testid="combo-rolling-excess"]'));
      if (benchPct && excessPct) ok('滚动建仓 · 基准/超额', `基准 ${benchPct} · 超额 ${excessPct}`);
      else bad('滚动建仓 · 基准/超额', `基准 ${benchPct} · 超额 ${excessPct}`);

      const capitalInput = page.locator('[data-testid="combo-rolling-capital"]');
      if ((await capitalInput.count()) > 0) {
        const capitalValue = await capitalInput.inputValue().catch(() => '');
        ok('滚动建仓 · 本金输入', capitalValue);
      } else {
        bad('滚动建仓 · 本金输入', '未找到本金输入框');
      }

      // 每列大盘
      const market = page.locator('[data-testid^="combo-rolling-market-"]').first();
      if (await market.count()) {
        const marketText = await textOf(market);
        if (/大盘|—/.test(marketText) && /站上|跌破|—/.test(marketText)) {
          ok('滚动建仓 · 每列大盘（涨跌+20日线位置）', marketText.slice(0, 80));
        } else {
          bad('滚动建仓 · 每列大盘（涨跌+20日线位置）', marketText.slice(0, 160));
        }
      } else {
        bad('滚动建仓 · 每列大盘', '未找到列头大盘块');
      }

      // 每格当日涨跌 / 当日盈亏 / 名单统计
      const change = page.locator('[data-testid^="combo-rolling-pick-change-"]').first();
      if (await change.count()) {
        const changeText = await textOf(change);
        if (/当日/.test(changeText)) ok('滚动建仓 · 每格当日涨跌幅', changeText);
        else bad('滚动建仓 · 每格当日涨跌幅', changeText || '(空)');
      } else {
        bad('滚动建仓 · 每格当日涨跌幅', '未找到格子涨跌');
      }
      const stats = page.locator('[data-testid^="combo-rolling-pick-stats-"]').first();
      if (await stats.count()) {
        const statsText = await textOf(stats);
        if (/在榜/.test(statsText) && /首入/.test(statsText) && /次日/.test(statsText)) {
          ok('滚动建仓 · 每格名单统计', statsText.slice(0, 90));
        } else {
          bad('滚动建仓 · 每格名单统计', statsText.slice(0, 160));
        }
      } else {
        bad('滚动建仓 · 每格名单统计', '未找到名单统计行');
      }

      // 明细：每日批次 + 逐日净值
      const batchHeader = page
        .locator('[data-testid="combo-rolling-details"] .ant-collapse-header')
        .filter({ hasText: '每日批次' })
        .first();
      if (await batchHeader.count()) {
        await batchHeader.click().catch(() => {});
        const batches = page.locator('[data-testid="combo-rolling-batches"]');
        await batches.waitFor({ timeout: 15000 }).catch(() => {});
        const batchText = await textOf(batches);
        if (/已结算|浮动中/.test(batchText) && /批次收益|盈亏金额/.test(batchText)) {
          ok('滚动建仓 · 每日批次表', batchText.slice(0, 120));
        } else {
          bad('滚动建仓 · 每日批次表', batchText.slice(0, 240));
        }
      } else {
        bad('滚动建仓 · 每日批次明细', '未找到「每日批次」折叠头');
      }

      const dailyHeader = page
        .locator('[data-testid="combo-rolling-details"] .ant-collapse-header')
        .filter({ hasText: '逐日净值' })
        .first();
      if (await dailyHeader.count()) {
        await dailyHeader.click().catch(() => {});
        const daily = page.locator('[data-testid="combo-rolling-daily"]');
        await daily.waitFor({ timeout: 15000 }).catch(() => {});
        const dailyText = await textOf(daily);
        if (/基准/.test(dailyText) && /超额/.test(dailyText) && /权益/.test(dailyText)) {
          ok('滚动建仓 · 逐日净值表含基准/超额');
        } else {
          bad('滚动建仓 · 逐日净值表含基准/超额', dailyText.slice(0, 240));
        }
      } else {
        bad('滚动建仓 · 逐日净值明细', '未找到「逐日净值」折叠头');
      }
    }

    // 6) 合规：组合工作区不出现买入/卖出
    const wsText = await textOf(ws);
    if (/买入|卖出/.test(wsText)) bad('合规词表', wsText.match(/.{0,20}(买入|卖出).{0,20}/)?.[0] ?? '');
    else ok('合规词表（无买入/卖出）');
  }

  await page.screenshot({ path: '/tmp/qm_probe_combo_institutional.png', fullPage: false });
} catch (e) {
  bad('探针异常', String(e).slice(0, 300));
} finally {
  const failed = results.filter((r) => !r.pass);
  for (const r of results) console.log(`[${r.pass ? 'PASS' : 'FAIL'}] ${r.name}${r.detail ? ' :: ' + r.detail : ''}`);
  console.log('[ERRORS]', errs.length ? errs.slice(0, 5).join(' || ') : 'none');
  console.log(`[SUMMARY] ${results.length - failed.length}/${results.length} pass`);
  await browser.close();
  process.exit(failed.length || errs.length ? 1 : 0);
}
