/**
 * 探针：推理中心 → 截面选股 → 历史统计（单模型 Top50 测试集统计）。
 *
 * 校验点：真实渲染总收益/基准/超额、月度拆解、无买卖字眼、无控制台错误。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 1050 } });

const api = [];
const consoleErrors = [];
page.on('response', async (r) => {
  if (r.url().includes('selection-stats')) {
    api.push({ url: r.url(), status: r.status(), body: await r.text().catch(() => '') });
  }
});
page.on('console', (m) => {
  if (m.type() === 'error') consoleErrors.push(m.text());
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
    if (
      !(await click(
        /稍后再答|同意|确认|我知道了|已阅读|知道了|跳过/,
        '.ant-modal:visible button',
      ))
    )
      break;
    await page.waitForTimeout(700);
  }
};
const text = async (sel) =>
  ((await page.locator(sel).first().textContent().catch(() => '')) || '')
    .replace(/\s+/g, ' ')
    .trim();

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

await page.goto(`${BASE}/#/inference-center`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(7000);
await dismiss();

const entered = await click(/截面选股/);
console.log('workspace cross clicked:', entered);
await page.waitForTimeout(1500);

const historyClicked = await click(/历史统计/, '.ant-segmented-item-label');
console.log('history clicked:', historyClicked);

const ok = await page
  .waitForSelector('[data-testid="selection-stats-total-return"]', { timeout: 90000 })
  .then(() => true)
  .catch(() => false);
console.log('result rendered:', ok);
await page.waitForTimeout(800);

const model = await text('[data-testid="cross-section-model"]');
const summary = await text('[data-testid="selection-stats-summary"]');
const range = await text('[data-testid="selection-stats-range"]');
const warnings = await text('[data-testid="selection-stats-warnings"]');
const errorText = await text('[data-testid="selection-stats-error"]');
const unsupported = await text('[data-testid="selection-stats-unsupported"]');
const months = await page.evaluate(() => {
  const root = document.querySelector('[data-testid="selection-stats-months"]');
  if (!root) return [];
  return Array.from(root.querySelectorAll('tbody tr'))
    .slice(0, 3)
    .map((tr) => (tr.textContent || '').replace(/\s+/g, ' ').trim());
});
const bodyText = (await page.locator('body').innerText()).replace(/\s+/g, ' ');
const banned = ['买入', '卖出', '加仓', '减仓', '建仓', '清仓'].filter((w) =>
  bodyText.includes(w),
);
const chart = await page.locator('[data-testid="selection-stats-chart"]').count();
const quality = await text('[data-testid="selection-stats-quality"]');
const footer = await text('[data-testid="selection-stats-root"] > div:last-child');

console.log('MODEL:', model);
console.log('SUMMARY:', summary);
console.log('QUALITY:', quality);
console.log('RANGE:', range);
console.log('WARNINGS:', warnings);
console.log('ERROR PANEL:', errorText);
console.log('UNSUPPORTED:', unsupported);
console.log('CHART NODES:', chart);
console.log('MONTHS:', JSON.stringify(months));
console.log('BANNED WORDS:', JSON.stringify(banned));
console.log('FOOTER:', footer.slice(0, 260));
// 第二种换股节奏：每日滚动 T+1
const dailyClicked = await click(/每日滚动T\+1/, '.ant-segmented-item-label');
await page.waitForTimeout(300);
const calcClicked = await click(/计算/, '[data-testid="selection-stats-root"] button');
const dailyOk = await page
  .waitForFunction(
    () => {
      const el = document.querySelector('[data-testid="selection-stats-range"]');
      return Boolean(el && /持有 1 个交易日/.test(el.textContent || ''));
    },
    null,
    { timeout: 90000 },
  )
  .then(() => true)
  .catch(() => false);
console.log('daily mode:', dailyClicked, calcClicked, dailyOk);
console.log('DAILY SUMMARY:', await text('[data-testid="selection-stats-summary"]'));
console.log('DAILY RANGE:', await text('[data-testid="selection-stats-range"]'));

console.log('API:', JSON.stringify(api.map((a) => ({ status: a.status, url: a.url }))));
console.log('CONSOLE ERRORS:', JSON.stringify(consoleErrors.slice(0, 6)));

await page.screenshot({ path: '/tmp/selection_stats_probe.png', fullPage: false });
await browser.close();
