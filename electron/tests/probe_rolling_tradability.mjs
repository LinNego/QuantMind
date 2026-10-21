/**
 * 滚动建仓 · 统计折叠 + 可交易性 探针（2026-10-21）。
 *
 * 覆盖：
 *  1) 顶部统计条可折叠：按钮文案/aria-expanded/折叠摘要/localStorage/再次展开；
 *  2) 「执行」段：建仓覆盖 / 容量约束 / ST 过滤 / 跳过明细；
 *  3) 卡片建仓状态标签（建仓 x% / 次日建仓 / 未建仓 · 原因），不用红绿色块；
 *  4) 批次明细「建仓」列；
 *  5) 合规：页面上没有买入/卖出/BUY/SELL。
 *
 * 只读：不改本金、不确认计划、不触发任何委托。
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_PROBE_BASE || 'http://localhost:3080';
const USER = process.env.QM_PROBE_USER || 'admin';
const PASS = process.env.QM_PROBE_PASS || 'admin123';
const KEY = 'qm:combo:rolling:metrics-collapsed';

const results = [];
const ok = (name, detail = '') => results.push({ name, pass: true, detail });
const bad = (name, detail = '') => results.push({ name, pass: false, detail });
const errs = [];

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 980 } });
page.on('pageerror', (e) => errs.push('[PAGEERROR] ' + (e.message || '').slice(0, 200)));
page.on('console', (m) => {
  if (m.type() !== 'error') return;
  const text = m.text();
  if (/ResizeObserver loop|\[antd: /.test(text)) return;
  errs.push('[console.error] ' + text.slice(0, 200));
});

const textOf = async (locator) =>
  ((await locator.innerText().catch(() => '')) || '').replace(/\n+/g, ' | ').trim();

try {
  // 登录
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

  const dismiss = async () => {
    const later = page.locator('button', { hasText: '稍后再答' }).first();
    if (await later.count()) {
      await later.click().catch(() => {});
      await page.waitForTimeout(500);
    }
  };

  // 推理中心 → 组合选股 → 已保存组合 → 滚动建仓
  await page.goto(`${BASE}/#/inference-center`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);
  await dismiss();
  await page
    .locator('button:has-text("组合选股"), [role="tab"]:has-text("组合选股"), .ant-tabs-tab:has-text("组合选股")')
    .first()
    .click()
    .catch(() => {});
  await page.locator('[data-testid="combo-workspace"]').waitFor({ timeout: 30000 });
  await page.waitForTimeout(800);

  const blendItems = page.locator('[data-testid^="combo-item-blend-"]');
  for (let i = 0; i < 40 && (await blendItems.count()) === 0; i += 1) {
    await page.waitForTimeout(500);
  }
  if ((await blendItems.count()) === 0) {
    bad('已保存组合', '列表为空');
    throw new Error('no blend');
  }
  await blendItems.nth(0).click();
  await page.waitForTimeout(2000);

  await page
    .locator('[role="tab"]:has-text("滚动建仓"), .ant-tabs-tab:has-text("滚动建仓")')
    .first()
    .click()
    .catch(() => {});
  await page.locator('[data-testid="combo-rolling-root"]').waitFor({ timeout: 30000 });
  await page.locator('[data-testid="combo-rolling-performance"]').waitFor({ timeout: 180000 });
  await page.waitForTimeout(800);
  ok('滚动建仓指标条渲染');

  // ── 1) 折叠 / 展开 ─────────────────────────────────────────────
  const toggle = page.locator('[data-testid="combo-rolling-performance-toggle"]');
  if ((await toggle.count()) === 0) {
    bad('统计条有折叠按钮');
  } else {
    ok('统计条有折叠按钮', await textOf(toggle));
    const expandedText = await textOf(toggle);
    const ariaBefore = await toggle.getAttribute('aria-expanded');
    await toggle.click();
    await page.waitForTimeout(400);
    const collapsedText = await textOf(toggle);
    const ariaAfter = await toggle.getAttribute('aria-expanded');
    const totalVisible = await page.locator('[data-testid="combo-rolling-total-return"]').count();
    const header = await textOf(page.locator('[data-testid="combo-rolling-performance-header"]'));
    const stored = await page.evaluate((k) => window.localStorage.getItem(k), KEY);
    if (
      expandedText.includes('收起统计') &&
      collapsedText.includes('展开统计') &&
      ariaBefore === 'true' &&
      ariaAfter === 'false' &&
      totalVisible === 0 &&
      /窗口收益/.test(header) &&
      /收益金额/.test(header) &&
      /期末现金/.test(header) &&
      stored === '1' &&
      !/undefined/.test(header)
    ) {
      ok('折叠：隐藏指标行 + 一行摘要 + localStorage', header.slice(0, 160));
    } else {
      bad(
        '折叠：隐藏指标行 + 一行摘要 + localStorage',
        JSON.stringify({ expandedText, collapsedText, ariaBefore, ariaAfter, totalVisible, header, stored }),
      );
    }
    await toggle.click();
    await page.waitForTimeout(400);
    const restored = await page.locator('[data-testid="combo-rolling-total-return"]').count();
    const storedAfter = await page.evaluate((k) => window.localStorage.getItem(k), KEY);
    if (restored === 1 && storedAfter === null) ok('再次展开恢复全部指标');
    else bad('再次展开恢复全部指标', JSON.stringify({ restored, storedAfter }));
  }

  // ── 2) 执行段 ─────────────────────────────────────────────────
  const coverage = await textOf(page.locator('[data-testid="combo-rolling-coverage"]'));
  const capacity = await textOf(page.locator('[data-testid="combo-rolling-capacity"]'));
  const stFilter = await textOf(page.locator('[data-testid="combo-rolling-st-filter"]'));
  const skips = await textOf(page.locator('[data-testid="combo-rolling-skips"]'));
  const execRow = await textOf(page.locator('[data-testid="combo-rolling-performance-tradability"]'));
  if (/\/\s*\d+（\d+%）/.test(coverage) || coverage === '—') ok('建仓覆盖', coverage || '(缺失)');
  else bad('建仓覆盖', coverage || '(缺失)');
  if (/参与率/.test(capacity) || capacity === '未启用') ok('容量约束', capacity);
  else bad('容量约束', capacity || '(缺失)');
  if (stFilter === '已启用' || stFilter === '未启用') ok('ST 过滤', stFilter);
  else bad('ST 过滤', stFilter || '(缺失)');
  if (/\d+ 只/.test(skips)) ok('跳过明细', skips);
  else bad('跳过明细', skips || '(缺失)');
  if (/建仓覆盖/.test(execRow) && /容量约束/.test(execRow) && /ST 过滤/.test(execRow)) {
    ok('「执行」段四指标同排');
  } else {
    bad('「执行」段四指标同排', execRow.slice(0, 200));
  }

  // ── 3) 卡片建仓状态 ───────────────────────────────────────────
  const tags = await page.evaluate(() =>
    Array.from(document.querySelectorAll('[data-testid^="combo-rolling-pick-buy-"]')).map((el) => ({
      text: (el.innerText || '').trim(),
      html: el.innerHTML,
    })),
  );
  const counts = {};
  for (const tag of tags) counts[tag.text] = (counts[tag.text] || 0) + 1;
  const kinds = tags.map((t) => t.text);
  const hasBought = kinds.some((t) => /^建仓 \d+(\.\d+)?%$/.test(t));
  const hasPlanned = kinds.some((t) => t === '次日建仓');
  const hasSkip = kinds.some((t) => t.startsWith('未建仓 · '));
  if (tags.length > 0 && hasSkip) {
    ok('卡片建仓状态标签', JSON.stringify(counts).slice(0, 220));
  } else {
    bad('卡片建仓状态标签', JSON.stringify(counts).slice(0, 220));
  }
  if (hasBought || hasPlanned) ok('建仓/次日候选标签存在', `${hasBought ? '建仓 ' : ''}${hasPlanned ? '次日建仓' : ''}`);
  else bad('建仓/次日候选标签存在', '未出现建仓或次日建仓标签');
  const badTone = tags.filter((t) => /red|green|rose|emerald/.test(t.html));
  if (badTone.length === 0) ok('建仓状态不用红绿');
  else bad('建仓状态不用红绿', JSON.stringify(badTone.slice(0, 3)));

  // ── 4) 批次明细「建仓」列 ─────────────────────────────────────
  const batchHeader = page.locator('[data-testid="combo-rolling-details"]');
  const detailsText = await textOf(batchHeader);
  const headerMatch = /每日批次（(\d+) 批/.exec(detailsText);
  if (headerMatch) {
    await page.locator('text=/每日批次（/').first().click();
    await page.locator('[data-testid="combo-rolling-batches"]').waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);
    const boughtCells = await page.evaluate(() =>
      Array.from(document.querySelectorAll('[data-testid="combo-rolling-batch-bought"]'))
        .slice(0, 4)
        .map((el) => (el.innerText || '').trim()),
    );
    if (boughtCells.length > 0 && boughtCells.every((v) => /^\d+$|—$/.test(v))) {
      ok('批次明细建仓列', boughtCells.join(','));
    } else {
      bad('批次明细建仓列', JSON.stringify(boughtCells));
    }
  } else {
    bad('批次明细存在');
  }

  // ── 5) 合规字眼 ───────────────────────────────────────────────
  const bodyText = (await page.locator('[data-testid="combo-rolling-root"]').innerText()) || '';
  if (/买入|卖出|BUY|SELL/.test(bodyText)) bad('合规：无买卖字眼', (bodyText.match(/.{0,20}(买入|卖出|BUY|SELL).{0,20}/) || [''])[0]);
  else ok('合规：无买卖字眼');
  if (/undefined|NaN/.test(bodyText)) bad('无 undefined/NaN', (bodyText.match(/.{0,20}(undefined|NaN).{0,20}/) || [''])[0]);
  else ok('无 undefined/NaN');

  await page.screenshot({ path: '/tmp/rolling_tradability.png', fullPage: false });
} catch (e) {
  bad('探针异常', String(e && e.message ? e.message : e).slice(0, 300));
} finally {
  await browser.close();
}

const failed = results.filter((r) => !r.pass);
for (const r of results) console.log(`${r.pass ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? ' — ' + r.detail : ''}`);
if (errs.length) {
  console.log('\n浏览器错误：');
  for (const e of errs.slice(0, 10)) console.log('  ' + e);
}
console.log(`\n${results.length - failed.length}/${results.length} 通过`);
process.exit(failed.length > 0 || errs.length > 0 ? 1 : 0);
