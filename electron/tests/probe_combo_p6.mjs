// P6 探针：推理中心「组合选股」Tab + 模型治理「一键串行补推」
// 不落真实委托：不确认计划、不启动补推任务；页面首启按产品设计可能自动建
// 默认组合（C0 全 13·T+10，仅本地纸面台账），探针不额外创建任何东西。
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

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 980 } });
page.on('pageerror', (e) => errs.push('[PAGEERROR] ' + (e.message || '').slice(0, 200)));
page.on('console', (m) => {
  if (m.type() === 'error') errs.push('[console.error] ' + m.text().slice(0, 200));
});

const bodyText = async () =>
  ((await page.locator('body').innerText().catch(() => '')) || '').replace(/\n+/g, ' | ');

try {
  // 1) 登录
  await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
  const inputs = page.locator('input');
  await inputs.nth(0).waitFor({ state: 'visible', timeout: 20000 });
  await inputs.nth(0).fill(USER);
  await inputs.nth(1).fill(PASS);
  const loginBtn = page.locator('button', { hasText: '登录' }).first();
  await loginBtn.click();
  await page.waitForURL(/#\/(?!auth)/, { timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(2500);
  // hash 路由判据看 URL，别用 '#/auth/login' 当选择器（CSS 下非法，会抛选择器异常）
  if (page.url().includes('/auth/login')) {
    const t = await bodyText();
    if (/登录/.test(t) && !/退出/.test(t)) bad('登录', '仍停在登录页');
    else ok('登录');
  } else {
    ok('登录');
  }

  // 2) 深链进入推理中心，切到组合选股
  await page.goto(`${BASE}/#/inference-center`, { waitUntil: 'domcontentloaded' });
  // 首启的「投资适当性评估」弹窗会盖住全页并拦截点击：先选择稍后再答
  const laterBtn = page.locator('button', { hasText: '稍后再答' }).first();
  if (await laterBtn.count()) {
    await laterBtn.click().catch(() => {});
    await page.waitForTimeout(600);
  }
  await page.locator('[data-testid="combo-workspace"]').waitFor({ timeout: 30000 }).catch(() => {});
  if (!(await page.locator('[data-testid="combo-workspace"]').count())) {
    // 顶栏是普通按钮（不是 antd Tabs）：按可见文本点击，兼容两种形态
    const tab = page
      .locator('button:has-text("组合选股"), [role="tab"]:has-text("组合选股"), .ant-tabs-tab:has-text("组合选股")')
      .first();
    await tab.click().catch(() => {});
    await page.locator('[data-testid="combo-workspace"]').waitFor({ timeout: 30000 }).catch(() => {});
  }
  const hasWorkspace = await page.locator('[data-testid="combo-workspace"]').count();
  if (hasWorkspace) ok('组合选股 Tab 渲染');
  else bad('组合选股 Tab 渲染', (await bodyText()).slice(0, 300));

  // 3) 左侧组合列表：模板或已保存组合
  await page.locator('[data-testid="combo-list"]').waitFor({ timeout: 20000 }).catch(() => {});
  const items = page.locator('[data-testid^="combo-item-"]');
  const nItems = await items.count();
  if (nItems > 0) ok('组合列表有可选项', `${nItems} 项`);
  else bad('组合列表有可选项', (await bodyText()).slice(0, 300));

  // 4) 选中第一个组合，等榜单
  if (nItems > 0) {
    await items.nth(0).click();
    await page.waitForTimeout(4000);
    const ws = page.locator('[data-testid="combo-workspace"]');
    const wsText = ((await ws.innerText().catch(() => '')) || '').replace(/\n+/g, ' | ');
    if (/当日榜单|靠前|居中|靠后|暂无|无数据|—/.test(wsText)) ok('榜单区域有内容/诚实空态', wsText.slice(0, 180));
    else bad('榜单区域有内容/诚实空态', wsText.slice(0, 240));

    // 5) 合规：组合工作区不出现买入/卖出
    if (/买入|卖出/.test(wsText)) bad('合规词表', '组合工作区出现买入/卖出');
    else ok('合规词表（无买入/卖出）');

    // 6) 滚动建仓
    const rollingTab = page.locator('[role="tab"]:has-text("滚动建仓"), .ant-tabs-tab:has-text("滚动建仓")').first();
    await rollingTab.click().catch(() => {});
    await page.waitForTimeout(3500);
    const rollText = ((await ws.innerText().catch(() => '')) || '').replace(/\n+/g, ' | ');
    if (/今日新进|滚动建仓|暂无|无数据|—/.test(rollText)) ok('滚动建仓区渲染', rollText.slice(0, 160));
    else bad('滚动建仓区渲染', rollText.slice(0, 240));

    // 7) 调仓计划
    const planTab = page.locator('[role="tab"]:has-text("调仓计划"), .ant-tabs-tab:has-text("调仓计划")').first();
    await planTab.click().catch(() => {});
    await page.waitForTimeout(3000);
    const planText = ((await ws.innerText().catch(() => '')) || '').replace(/\n+/g, ' | ');
    if (/调仓计划|纸面|暂无|生成|—/.test(planText)) ok('调仓计划区渲染', planText.slice(0, 160));
    else bad('调仓计划区渲染', planText.slice(0, 240));
  }

  // 8) 模型治理：多选 → 一键推理入口出现（只开预览，不起任务）
  const govTab = page
    .locator('button:has-text("模型治理"), [role="tab"]:has-text("模型治理"), .ant-tabs-tab:has-text("模型治理")')
    .first();
  await govTab.click().catch(() => {});
  await page.waitForTimeout(3000);
  // 补推工具条只在勾选 ≥2 个模型后出现；这里只改选择态，不点「一键推理至最新」
  const rowChecks = page.locator('.ant-checkbox-input');
  const nChecks = await rowChecks.count();
  if (nChecks >= 2) {
    await rowChecks.nth(0).click().catch(() => {});
    await rowChecks.nth(1).click().catch(() => {});
    await page.waitForTimeout(800);
  }
  const gov = ((await page.locator('body').innerText().catch(() => '')) || '').replace(/\n+/g, ' | ');
  const hasBackfillBtn = /一键推理至最新|串行补推/.test(gov);
  if (hasBackfillBtn) ok('模型治理补推入口可见（勾选 2 个模型后）');
  else bad('模型治理补推入口可见', gov.slice(0, 300));

  await page.screenshot({ path: '/tmp/qm_probe_combo.png', fullPage: false });
} catch (e) {
  bad('探针异常', String(e).slice(0, 300));
} finally {
  const failed = results.filter((r) => !r.pass);
  for (const r of results) console.log(`[${r.pass ? 'PASS' : 'FAIL'}] ${r.name}${r.detail ? ' :: ' + r.detail : ''}`);
  console.log('[ERRORS]', errs.length ? errs.slice(0, 5).join(' || ') : 'none');
  console.log(`[SUMMARY] ${results.length - failed.length}/${results.length} pass`);
  await browser.close();
  process.exit(failed.length ? 1 : 0);
}
