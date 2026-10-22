/**
 * 「已归档 → 恢复使用」按钮探针。
 *
 * 只验证 UI 可达性与位置（右侧操作区），**不点击恢复**——恢复会改真实模型状态。
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const ctx = await browser.newContext({ viewport: { width: 1720, height: 1150 } });
const page = await ctx.newPage();

const failures = [];
const check = (name, ok, extra = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${extra ? ` — ${extra}` : ''}`);
  if (!ok) failures.push(name);
};

await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await page.waitForTimeout(2500);
if ((await page.locator('input[type=password]').count()) > 0) {
  await page.locator('input').nth(0).fill('admin');
  await page.locator('input[type=password]').first().fill('admin123');
  const buttons = page.locator('button');
  for (let i = 0; i < (await buttons.count()); i++) {
    const text = (await buttons.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
    if (/登录|登\s*录/.test(text)) {
      await buttons.nth(i).click();
      break;
    }
  }
  await page.waitForTimeout(7000);
}

await page.goto(`${BASE}/#/model-registry`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await page.waitForTimeout(8000);

// 关掉可能遮挡的弹窗（登录提示/公告等）
const modals = page.locator('.ant-modal-wrap');
for (let i = 0; i < (await modals.count()); i++) {
  const modal = modals.nth(i);
  if (!(await modal.isVisible().catch(() => false))) continue;
  console.log('dismiss modal:', (await modal.innerText().catch(() => '')).replace(/\s+/g, ' ').slice(0, 160));
  const close = modal.locator('.ant-modal-close');
  if (await close.count()) await close.first().click().catch(() => {});
  else await page.keyboard.press('Escape').catch(() => {});
  await page.waitForTimeout(600);
}

check('模型资产库页面可达', (await page.locator('body').innerText()).includes('模型资产库'));

const archivedTab = page.locator('button', { hasText: /已归档/ }).first();
check('已归档 Tab 存在', (await archivedTab.count()) > 0);
if ((await archivedTab.count()) > 0) {
  await archivedTab.click();
  await page.waitForTimeout(2500);
}

const firstArchivedCard = page
  .locator('div.cursor-pointer', { hasText: /mdl_[a-z0-9_]+/ })
  .first();
const cardCount = await firstArchivedCard.count();
check('归档列表有模型卡片', cardCount > 0);
if (cardCount > 0) {
  await firstArchivedCard.click();
  await page.waitForTimeout(1500);
}

const restoreBtn = page.getByRole('button', { name: '恢复使用' });
const restoreCount = await restoreBtn.count();
check('右侧出现「恢复使用」按钮', restoreCount > 0);

if (restoreCount > 0) {
  const box = await restoreBtn.first().boundingBox();
  check(
    '按钮位于主区右侧',
    Boolean(box) && box.x > 1720 * 0.55,
    box ? `x=${Math.round(box.x)}` : 'no box',
  );
  const archiveBtn = page.getByRole('button', { name: /^归档$/ });
  check('归档模型不再显示「归档」按钮', (await archiveBtn.count()) === 0);
}

await page.screenshot({ path: '/tmp/model_restore_button.png', fullPage: false });
console.log('screenshot: /tmp/model_restore_button.png');
await browser.close();
process.exit(failures.length ? 1 : 0);
