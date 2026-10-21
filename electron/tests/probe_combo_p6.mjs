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
  // combo-list 一挂载就可见，此时清单还在骨架期：等到「有项 / 明确空态 / 读取失败」再计数，
  // 否则零项只是请求没回来，会把正常加载误报成失败。
  for (let i = 0; i < 40; i += 1) {
    if ((await items.count()) > 0) break;
    if (/暂无可用模板|暂无已保存组合|读取失败/.test(await bodyText())) break;
    await page.waitForTimeout(500);
  }
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
    if (/最低成交额|剔除 ST/.test(planText)) ok('计划参数含机构硬条件（最低成交额 / 剔除 ST）');
    else bad('计划参数含机构硬条件', planText.slice(0, 200));

    // 7.5) 计划表横向可滚（列多时不硬挤）
    const planTable = page.locator('[data-testid="combo-plan-table"]');
    if (await planTable.count()) {
      const body = planTable.locator('.ant-table-body, .ant-table-content').first();
      const m = await body.evaluate((el) => ({ sw: el.scrollWidth, cw: el.clientWidth })).catch(() => null);
      if (!m) bad('计划表横向滚动', '表格容器未找到');
      else if (m.sw > m.cw) ok('计划表可横向滚动', `${m.sw}>${m.cw}`);
      else ok('计划表列宽未溢出当前视口', `${m.sw}<=${m.cw}`);
    }

    // 7.6) 两档视口：滚动看板可横/纵滚动，且内容不被裁剪
    for (const vp of [{ width: 1366, height: 768 }, { width: 1680, height: 980 }]) {
      await page.setViewportSize(vp);
      await rollingTab.click().catch(() => {});
      await page.waitForTimeout(1800);
      const board = page.locator('[data-testid="combo-rolling-board"]');
      if (!(await board.count())) {
        bad(`滚动看板存在@${vp.width}`, '未渲染');
        continue;
      }
      const met = await board.evaluate((el) => {
        el.scrollLeft = el.scrollWidth;
        el.scrollTop = el.scrollHeight;
        return {
          sw: el.scrollWidth, cw: el.clientWidth, sh: el.scrollHeight, ch: el.clientHeight,
          left: el.scrollLeft, top: el.scrollTop,
        };
      });
      if (met.sw > met.cw && met.left > 0) ok(`滚动看板横向可滚@${vp.width}`, `${met.left}/${met.sw - met.cw}`);
      else bad(`滚动看板横向可滚@${vp.width}`, JSON.stringify(met));
      if (met.sh <= met.ch || met.top > 0) ok(`滚动看板纵向不裁剪@${vp.width}`, `${met.sh}/${met.ch}`);
      else bad(`滚动看板纵向不裁剪@${vp.width}`, JSON.stringify(met));
      await page.screenshot({ path: `/tmp/qm_probe_combo_rolling_${vp.width}.png` });
    }
    await page.setViewportSize({ width: 1680, height: 980 });

    // 7.7) K2：T+5 模板标注无推理成员，且预览无报错
    const t5Template = page.locator('[data-testid^="combo-item-template-"]', { hasText: 'T+5' }).first();
    if (await t5Template.count()) {
      const t5Text = ((await t5Template.innerText().catch(() => '')) || '').replace(/\n+/g, ' ');
      if (/\d+\s*只无推理/.test(t5Text)) ok('T+5 模板标注无推理成员', t5Text.slice(0, 120));
      else bad('T+5 模板标注无推理成员', t5Text.slice(0, 160));
      await t5Template.click();
      await page.waitForTimeout(4000);
      const t5Body = ((await ws.innerText().catch(() => '')) || '').replace(/\n+/g, ' | ');
      if (/读取失败|请求失败|Model not found|组合没有可用数据日/.test(t5Body)) {
        bad('T+5 模板预览', t5Body.slice(0, 240));
      } else {
        ok('T+5 模板预览', t5Body.slice(0, 140));
      }
    } else {
      bad('T+5 模板存在', '模板列表未找到 T+5');
    }
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
