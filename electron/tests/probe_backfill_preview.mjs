/**
 * 复现：模型治理 → 一键推理至最新 → 预览为空 / [object Object]。
 *
 * 用法：node electron/tests/probe_backfill_preview.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });

page.on('response', async (r) => {
  if (r.url().includes('/backfill-jobs/preview')) {
    console.log('PREVIEW_STATUS', r.status());
    console.log('PREVIEW_REQ', r.request().postData()?.slice(0, 800));
    try {
      console.log('PREVIEW_RES', (await r.text()).slice(0, 1600));
    } catch {
      /* ignore */
    }
  }
});
page.on('requestfailed', (r) => {
  if (r.url().includes('backfill')) console.log('PREVIEW_FAILED', r.url(), r.failure()?.errorText);
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

await page.goto(`${BASE}/#/inference-center`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(8000);
await dismiss();
await click(/模型治理/, 'button');
await page.waitForTimeout(4000);
await dismiss();

const headerCheck = page.locator('.ant-table-thead .ant-checkbox-input').first();
await headerCheck.click({ timeout: 5000 }).catch(() => {});
await page.waitForTimeout(500);
const selectionBar = await page
  .locator('[data-testid="governance-selection-bar"]')
  .innerText()
  .catch(() => '');
console.log('SELECTION_BAR', JSON.stringify(selectionBar));
const buttonState = await page
  .locator('button:has-text("一键推理至最新")')
  .first()
  .isEnabled()
  .catch(() => null);
console.log('BUTTON_ENABLED', buttonState);

const opened = await click(/一键推理至最新/, 'button');
console.log('OPENED', opened);
await page.waitForTimeout(4000);

const modalCount = await page.locator('.ant-modal-wrap:visible').count();
console.log('VISIBLE_MODALS', modalCount);
const modalText = await page.locator('.ant-modal-content:visible').first().innerText().catch(() => '');
console.log('MODAL_TEXT', JSON.stringify(modalText.slice(0, 1500)));
await page.screenshot({ path: '/tmp/backfill_preview.png', fullPage: false });

const bodyHtml = await page
  .locator('[data-testid="backfill-job-body"]')
  .innerText()
  .catch(() => '');
console.log('BODY_TEXT', JSON.stringify(bodyHtml.slice(0, 1200)));

await browser.close();
