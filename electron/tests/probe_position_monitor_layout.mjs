/**
 * 持仓监控重设计探针（2026-10-23）：首屏可见行数 + 两档截图 + 分析抽屉展开。
 *
 * 用法：node electron/tests/probe_position_monitor_layout.mjs
 * 依赖本机 3080 已部署最新构建、admin/admin123 可登录。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

page.on('response', async (r) => {
  if (/\/simulation\/account(\?|$)/.test(r.url())) {
    try {
      const body = await r.json();
      const d = body?.data || body;
      console.log(
        'SIM_ACCOUNT',
        JSON.stringify({
          cash: d?.cash,
          available_cash: d?.available_cash,
          total_asset: d?.total_asset,
          market_value: d?.market_value,
          positions: Array.isArray(d?.positions)
            ? d.positions.length
            : d?.positions && typeof d.positions === 'object'
              ? Object.keys(d.positions).length
              : null,
        }),
      );
    } catch {
      /* 非 JSON 忽略 */
    }
  }
});

const click = async (re, scope = 'button') => {
  const items = page.locator(scope);
  for (let i = 0, n = await items.count(); i < n; i++) {
    const t = ((await items.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
    if (re.test(t)) {
      await items.nth(i).click({ timeout: 5000 }).catch(() => {});
      return true;
    }
  }
  return false;
};

const dismiss = async () => {
  for (let r = 0; r < 6; r++) {
    const n = await page.evaluate(
      () =>
        Array.from(document.querySelectorAll('.ant-modal-wrap')).filter(
          (m) => getComputedStyle(m).display !== 'none',
        ).length,
    );
    if (!n) return;
    if (!(await click(/稍后再答|同意|确认|我知道了|已阅读|知道了|跳过/, '.ant-modal:visible button'))) break;
    await page.waitForTimeout(700);
  }
};

const rowCount = () => page.locator('[data-testid="position-row"]').count();

const report = async (label) => {
  await page.waitForTimeout(1200);
  const count = await rowCount();
  const kpi = await page
    .locator('[data-testid="position-kpi-bar"]')
    .innerText()
    .catch(() => '');
  const header = await page
    .locator('[data-testid="position-table-header"]')
    .innerText()
    .catch(() => '');
  const insightsOpen = await page
    .locator('[data-testid="position-insights"]')
    .getAttribute('data-open')
    .catch(() => null);
  const cashPill = await page.evaluate(() => {
    const spans = Array.from(
      document.querySelectorAll('[data-testid="position-kpi-bar"] span'),
    );
    const el = spans.find((s) => (s.textContent || '').includes('现金占比'));
    return el ? { text: el.textContent, title: el.getAttribute('title') } : null;
  });
  const kpiSummary = await page.evaluate(() => {
    const el = document.querySelector('[data-testid="position-kpi-bar"]');
    if (!el) return null;
    const fk = Object.keys(el).find((k) => k.startsWith('__reactFiber$'));
    let fiber = fk ? el[fk] : null;
    while (fiber && !(fiber.memoizedProps && fiber.memoizedProps.summary)) {
      fiber = fiber.return;
    }
    return fiber ? fiber.memoizedProps.summary : null;
  });
  console.log(
    JSON.stringify({ label, rows: count, insightsOpen, cashPill, kpiSummary, kpi: kpi.replace(/\n/g, ' | '), header: header.replace(/\n/g, ' | ') }),
  );
  return count;
};

await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(4000);
const inputs = page.locator('input:visible');
if ((await inputs.count()) >= 2) {
  await inputs.nth(0).fill('admin');
  await inputs.nth(1).fill('admin123');
  await click(/登录/);
  await page.waitForTimeout(6000);
}
await dismiss();

await page.goto(`${BASE}/#/trading`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(7000);
await dismiss();
await click(/持仓监控/);
await page.waitForTimeout(4000);
await dismiss();

const at1440 = await report('1440x900');
await page.screenshot({ path: '/tmp/position_monitor_1440.png', fullPage: false });

await page.setViewportSize({ width: 1920, height: 1080 });
const at1920 = await report('1920x1080');
await page.screenshot({ path: '/tmp/position_monitor_1920.png', fullPage: false });

// 展开分析抽屉
await page.locator('[data-testid="position-insights"] button').first().click().catch(() => {});
await page.waitForTimeout(1500);
await report('1920x1080 + insights');
await page.screenshot({ path: '/tmp/position_monitor_1920_insights.png', fullPage: false });

console.log(JSON.stringify({ at1440, at1920, delta: at1920 - at1440 }));
await browser.close();
