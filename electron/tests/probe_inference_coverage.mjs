/**
 * 探针：模型管理 → 推理管理（InferenceCoveragePanel）日历实际显示范围。
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const MODEL_NAME = process.env.PROBE_MODEL || 'T+10 跨库300 LightGBM 长窗2026验证';

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });

page.on('response', async (r) => {
  if (r.url().includes('/inference/coverage')) {
    try {
      const d = await r.json();
      console.log(
        'COVERAGE',
        JSON.stringify({
          min_date: d.min_date,
          max_date: d.max_date,
          latest_trade_date: d.latest_trade_date,
          gaps: (d.gap_dates || []).length,
          estimated: d.estimated,
        }),
      );
    } catch {
      /* ignore */
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

// 选中目标模型（点名称所在行/卡）
const matches = await page.evaluate((needle) => {
  const out = [];
  for (const el of Array.from(document.querySelectorAll('div,span,button'))) {
    const t = (el.textContent || '').trim();
    if (t.includes(needle) && t.length < 120) {
      out.push({ tag: el.tagName, cls: (el.className || '').toString().slice(0, 80), text: t.slice(0, 80) });
    }
  }
  return out.slice(0, 10);
}, MODEL_NAME);
console.log('MATCHES', JSON.stringify(matches));
const selected = await page.evaluate((needle) => {
  const els = Array.from(document.querySelectorAll('div,span,button'));
  const hit = els.find((el) => (el.textContent || '').trim() === needle)
    || els.find((el) => (el.textContent || '').includes(needle) && (el.textContent || '').length < 120);
  if (!hit) return false;
  hit.scrollIntoView({ block: 'center' });
  hit.click();
  return true;
}, MODEL_NAME);
console.log('MODEL_SELECTED', selected);
await page.waitForTimeout(2000);

const tabLocator = page.locator('.ant-tabs-tab', { hasText: '推理管理' }).first();
await tabLocator.click({ timeout: 5000 }).catch(() => {});
console.log('TAB_CLICKED', await tabLocator.count());
await page.waitForTimeout(5000);
await dismiss();

const months = await page.evaluate(() =>
  Array.from(document.querySelectorAll('.text-xs.font-black.text-slate-700.text-center.mb-1')).map(
    (el) => (el.textContent || '').trim(),
  ),
);
const cells = await page.evaluate(() => {
  const out = { green: 0, amber: 0, gray: 0, future: 0 };
  const nodes = Array.from(document.querySelectorAll('.h-7.flex.items-center.justify-center.rounded-lg.border'));
  for (const n of nodes) {
    const cls = n.className || '';
    if (cls.includes('bg-emerald')) out.green++;
    else if (cls.includes('bg-amber')) out.amber++;
    else if (cls.includes('bg-slate-100')) out.future++;
    else out.gray++;
  }
  return out;
});
console.log('MONTHS', JSON.stringify(months));
console.log('CELLS', JSON.stringify(cells));
await page.screenshot({ path: '/tmp/inference_coverage.png', fullPage: false });

await browser.close();
