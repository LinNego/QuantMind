/**
 * 候选信号 · 推理模型下拉完整性验收（2026-10-20）。
 *
 * 用户复报「选择模型显示不全」：下拉原先只列近 90 天有信号的模型，注册表里
 * 尚未跑推理的新模型整段消失。本探针锁定修复后的用户可见事实：
 *   1. `/stock-terminal/list` 的 `models` 覆盖注册表（远多于 15 个），且包含
 *      `has_signals_90d=false` 的注册表模型；
 *   2. 下拉按「近 90 天有信号 / 无信号」分组，组标题带数量；
 *   3. 无信号模型仍可搜索并选中，列表空态明说「这款模型当日没有打分」。
 *
 * 用法：QM_BASE=http://localhost:3080 node tests/probe_candidate_model_options.mjs
 * 硬规则：深链必须带完整 `#/`；等待确定性信号（响应/可见文案），不拿 sleep 当断言。
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';

const b = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();

let pass = 0;
let total = 0;
const check = (ok, label) => {
  total++;
  if (ok) pass++;
  console.log(`${ok ? '✓' : '✗'} ${label}`);
};

const pageErrors = [];
p.on('pageerror', (e) => pageErrors.push(String(e.message || e).slice(0, 200)));

/** 等待 /list 响应并返回其 models（可带模型过滤条件）。 */
function waitListModels(predicate = () => true) {
  return p.waitForResponse(
    (r) => /stock-terminal\/list/.test(r.url()) && predicate(r.url()),
    { timeout: 30000 },
  ).then(async (r) => {
    const j = await r.json();
    return (j.data ?? j).models ?? [];
  });
}

await p.goto(`${BASE}/#/trading?tab=signals`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await p.waitForTimeout(4000);
if ((await p.locator('input[type=password]').count()) > 0) {
  await p.locator('input').nth(0).fill('admin');
  await p.locator('input[type=password]').first().fill('admin123');
  const btns = p.locator('button');
  for (let i = 0; i < (await btns.count()); i++) {
    const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
    if (/登录/.test(t)) { await btns.nth(i).click(); break; }
  }
  await p.waitForTimeout(7000);
  await p.goto(`${BASE}/#/trading?tab=signals`, { waitUntil: 'domcontentloaded', timeout: 40000 });
}

// 首启适当性问卷会挡住筛选面板：点「稍后再答」，等遮罩消失
const later = p.locator('button', { hasText: '稍后再答' }).first();
if (await later.count()) {
  await later.click().catch(() => {});
  await p.locator('.ant-modal-wrap:visible').first()
    .waitFor({ state: 'hidden', timeout: 10000 }).catch(() => {});
}

const models = await waitListModels();
check(models.length > 15, `下拉选项覆盖注册表（${models.length} > 15）`);
const registryOnly = models.filter((m) => m.has_signals_90d === false);
check(registryOnly.length > 0, `包含近 90 天无信号的注册表模型（${registryOnly.length} 个）`);

const sel = p.locator('.ant-select').filter({ hasText: '推理模型' }).first();
await sel.locator('input').first().click();
const dd = p.locator('.ant-select-dropdown:visible').first();
await dd.waitFor({ state: 'visible', timeout: 10000 });
await dd.locator('.ant-select-item-group').first().waitFor({ state: 'visible', timeout: 10000 });
// 注意：antd 分组头用 innerText 取会得到空串（虚拟列表 + title 属性），
// 必须取 textContent，否则探针会假阴性。
const headers = await dd
  .locator('.ant-select-item-group')
  .evaluateAll((els) => els.map((e) => e.textContent || ''));
check(
  headers.some((h) => /近 90 天有信号（\d+）/.test(h)),
  `分组标题：有信号（${headers.join(' / ')}）`,
);
check(
  headers.some((h) => /近 90 天无信号（\d+）/.test(h)),
  '分组标题：无信号（无信号模型仍可选）',
);

// 取一个有显示名的注册表模型，搜索并选中；断言服务端确实按该模型收窄且当日 0 行
const target = registryOnly.find((m) => m.display_name) ?? registryOnly[0];
const query = (target.display_name || target.model_id).slice(0, 24);
await sel.locator('input').first().fill(query);
const opt = dd.locator('.ant-select-item-option', { hasText: query }).first();
await opt.waitFor({ state: 'visible', timeout: 10000 });
const listResp = waitListModels((u) => u.includes(encodeURIComponent(target.model_id)) || u.includes(target.model_id));
await opt.click();
const narrowed = await listResp;
check(
  narrowed.length > 15 && narrowed.some((m) => m.model_id === target.model_id),
  '选中无信号模型后，服务端 models 字段仍完整（不随 model= 塌陷）',
);

const hint = p.locator('text=这款模型当日没有打分').first();
await hint.waitFor({ state: 'visible', timeout: 15000 });
check(true, '列表空态明说「这款模型当日没有打分」');
check(pageErrors.length === 0, `无页面异常（${pageErrors.join(' | ') || '0'}）`);

console.log(`\n${pass}/${total} 通过`);
await b.close();
process.exit(pass === total ? 0 : 1);
