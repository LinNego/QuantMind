/**
 * 探针：模型管理 → 大盘分析（均分 × 指数叠加，T+1 错开）。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const MODEL_NAME = process.env.PROBE_MODEL || 'T+10 跨库300 LightGBM 长窗2026验证';

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });

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
    await page.waitForTimeout(600);
  }
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

await page.goto(`${BASE}/#/model-registry`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(8000);
await dismiss();

await page.evaluate((needle) => {
  const els = Array.from(document.querySelectorAll('div,span,button'));
  const hit =
    els.find((el) => (el.textContent || '').trim() === needle) ||
    els.find((el) => (el.textContent || '').includes(needle) && (el.textContent || '').length < 160);
  if (hit) {
    hit.scrollIntoView({ block: 'center' });
    hit.click();
  }
}, MODEL_NAME);
await page.waitForTimeout(2500);

await page.locator('.ant-tabs-tab', { hasText: '大盘分析' }).first().click({ timeout: 5000 }).catch(() => {});
await page.waitForTimeout(5000);
await dismiss();

const text = await page
  .locator('.ant-tabs-tabpane-active')
  .innerText()
  .catch(() => '');
console.log('PANEL_TEXT', JSON.stringify(text.slice(0, 600)));
await page.screenshot({ path: '/tmp/market_regime_overlay.png', fullPage: false });

// 全部叠加
await click(/全部叠加/);
await page.waitForTimeout(3000);
await page.screenshot({ path: '/tmp/market_regime_overlay_all.png', fullPage: false });
console.log('ALL_DONE', true);

await browser.close();
