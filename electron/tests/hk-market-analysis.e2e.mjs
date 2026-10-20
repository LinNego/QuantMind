/** 港股市场分析页 E2E —— 专业化升级实机验证
 *
 * 覆盖：
 * 1) 八 Tab 内容断言（恒指脉搏 / 机会雷达 / CCASS 席位 / 南向资金 / 估值主题 / 轮动 & AH / 机构持仓 / 聪明钱）
 * 2) 行业热力图两层下钻：L1 色块 → L2 成分股（面包屑往返）→ L2 色块 → 个股终端浮窗
 * 3) 机会雷达个股行点击 → 个股终端浮窗（关闭后消失）
 * 4) 接口面：/market-analysis-hk/opportunity/* 与 /heatmap/drill 全部 200
 * 5) AH 溢价停更横幅动态判据（有则标注日期，无则说明数据新鲜）
 *
 * 依赖：本地部署（deploy_frontend.sh → quantmind-web:3080）+ 后端 /market-analysis-hk 可用。
 * 断言取向：内容关键字命中（确定性等待 + 轮询，不用固定 sleep 兜底硬断言）。
 */
import { chromium } from 'playwright';

const BASE = process.env.QM_BASE || 'http://localhost:3080';
const OUT = '/tmp';

const TAB_EXPECT = {
  恒指脉搏: ['港股市场温度计与赚钱效应', '个股综合赚钱效应 Top 10', '恒生行业热力图', '面积=当日成交额', '机会分'],
  机会雷达: [
    '机会雷达 · 六柱因子复合评分',
    '研究评分卡（非模型预测、非交易指令）',
    '候选池',
    '机会个股榜（综合分降序）',
    '六柱分位',
    '机会行业榜（成分中位分）',
    '风险警示榜（弱势 + 波动 + 撤离线索）',
  ],
  'CCASS 席位': ['全市场集中度榜', '个股席位穿透', '席位新进', '席位退出'],
  南向资金: ['南向资金 · 港股通持股总览', '南向增持榜', '南向减持榜', '南向板块配置'],
  估值主题: ['港股估值主题选股（股息 / 估值双维）', 'AH 溢价 · 两地比价', '派息日历（未来 60 天除息）'],
  '轮动 & AH': ['恒生行业轮动（1/5/20日）', '行业估值温度计（PE 中位 / 股息率）', 'AH 对应股联动'],
  机构持仓: ['市场机构持仓结构', '个股机构查询', '机构增持榜'],
  聪明钱: [
    '聪明钱 · 席位行为统计与类型效力',
    '研究统计（非模型预测、非交易指令）',
    '类型净流概览',
    '类型效力矩阵',
    '席位榜',
    '共识榜',
    '证据台 · 裁定与标定',
  ],
};

const apiCalls = [];
const errors = [];
const hardFails = [];
const softFails = [];
const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', args: ['--no-sandbox'] });
const ctx = await b.newContext({ viewport: { width: 1720, height: 1150 } });
const p = await ctx.newPage();
p.on('console', (m) => { if (m.type() === 'error') errors.push('[console] ' + m.text().slice(0, 200)); });
p.on('pageerror', (e) => errors.push('[PAGEERROR] ' + (e.message || '').slice(0, 200)));
p.on('response', (r) => {
  const u = r.url();
  if (/market-analysis-hk/.test(u)) {
    apiCalls.push(r.status() + ' ' + u.split('market-analysis-hk')[1].slice(0, 60));
    if (r.status() >= 400) errors.push('[API ' + r.status() + '] ' + u.slice(0, 140));
  } else if (r.status() >= 400 && !/favicon/.test(u)) {
    // 含 URL 的 4xx 记录：console 的 "Failed to load resource" 不带地址，无法定位
    errors.push('[API ' + r.status() + '] ' + u.slice(0, 160));
  }
});

const flat = (s) => s.replace(/\s+/g, ' ');
const bodyText = async () => flat(await p.locator('body').innerText().catch(() => ''));

function hard(ok, label) {
  console.log(`${ok ? '✓' : '✗'} ${label}`);
  if (!ok) hardFails.push(label);
}
function soft(ok, label) {
  console.log(`${ok ? '✓' : '✗(软)'} ${label}`);
  if (!ok) softFails.push(label);
}

async function dismissModal() {
  const wrap = p.locator('.ant-modal-wrap').first();
  if (await wrap.isVisible().catch(() => false)) {
    const close = p.locator('.ant-modal-close').first();
    if (await close.count()) {
      await close.click({ timeout: 3000 }).catch(() => undefined);
    } else {
      await p.keyboard.press('Escape').catch(() => undefined);
    }
    await p.waitForTimeout(800);
  }
}

/** 轮询等待 body 文本满足条件（确定性等待，避免固定 sleep 断言） */
async function waitForText(pred, timeoutMs = 12000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (pred(await bodyText())) return true;
    await p.waitForTimeout(500);
  }
  return false;
}

/** 「个股终端」浮窗是否打开（标题栏/悬浮条文本） */
async function terminalOpen() {
  return await p.locator('span', { hasText: '个股终端' }).count() > 0;
}
async function closeTerminal() {
  const btn = p.locator('button[title="关闭"]').last();
  if (await btn.count()) {
    await btn.click({ timeout: 3000 }).catch(() => undefined);
    await p.waitForTimeout(600);
  }
}

// ---- 登录 ----
await p.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await p.waitForTimeout(3500);
if ((await p.locator('input[type=password]').count()) > 0) {
  await p.locator('input').nth(0).fill('admin');
  await p.locator('input[type=password]').first().fill('admin123');
  const btns = p.locator('button');
  for (let i = 0; i < (await btns.count()); i++) {
    const t = (await btns.nth(i).innerText().catch(() => '')).replace(/\s/g, '');
    if (/登录|登\s*录/.test(t)) { await btns.nth(i).click(); break; }
  }
  await p.waitForTimeout(7000);
}
console.log('登录后 URL:', p.url());

// ---- 切换市场为港股 ----
await dismissModal();
const hkBtn = p.locator('button[role=radio]', { hasText: '港股' }).first();
console.log('找到「港股」切换按钮:', (await hkBtn.count()) > 0);
const hkClicked = await hkBtn.click({ timeout: 8000 }).then(() => true).catch(() => false);
if (!hkClicked) {
  await p.evaluate(() => localStorage.setItem('qm:current_market', 'HK'));
  await p.reload({ waitUntil: 'domcontentloaded' });
}
await p.waitForTimeout(2500);
console.log('切换后 localStorage 市场:', await p.evaluate(() => localStorage.getItem('qm:current_market')));

// ---- 深链进「市场分析」 ----
await p.goto(`${BASE}/#/market-analysis`, { waitUntil: 'domcontentloaded' });
await p.waitForTimeout(9000);
await dismissModal();
await p.waitForTimeout(500);

let f = await bodyText();
console.log('\nURL:', p.url());
hard(f.includes('港股市场多维分析与资金穿透'), '页面标题：港股市场多维分析与资金穿透');
hard(f.includes('QuantHK'), '数据引擎标识：QuantHK');
hard(f.includes('市场: 港股'), '市场标识 pill：市场: 港股');
hard(f.includes('恒生指数'), '恒生指数卡存在');
hard(!f.includes('上证指数'), '未误渲染 A 股页');
const NAV = ['恒指脉搏', '机会雷达', 'CCASS 席位', '南向资金', '估值主题', '轮动 & AH', '机构持仓'];
hard(NAV.every((t) => f.includes(t)), `七 Tab 导航齐全：${NAV.join(' / ')}`);

// ---- 逐 Tab 断言 ----
console.log('\n---- 各 Tab 内容断言 ----');
let pass = 0;
for (const [tab, keys] of Object.entries(TAB_EXPECT)) {
  const el = p.locator('button', { hasText: tab }).first();
  const clicked = await el.click({ timeout: 6000 }).then(() => true).catch(() => false);
  if (!clicked) { hard(false, `${tab}: Tab 按钮点击`); continue; }
  // 机会雷达数据量大（4 个接口并发），给足渲染时间
  const ok = await waitForText((t) => keys.every((k) => t.includes(k)), tab === '机会雷达' ? 20000 : 12000);
  const t = await bodyText();
  const hit = keys.filter((k) => t.includes(k));
  if (ok) pass++;
  else hardFails.push(`${tab} 内容断言`);
  console.log(`${ok ? '✓' : '✗'} ${tab}: ${hit.length}/${keys.length} [${hit.join(' / ')}]`);
  await p.screenshot({ path: `${OUT}/hk-ma-${tab.replace(/[ &]/g, '_')}.png` });
}
console.log(`\nTab 通过 ${pass}/${Object.keys(TAB_EXPECT).length}`);

// ---- 热力图下钻：L1 → L2 → 个股终端 ----
console.log('\n---- 行业热力图两层下钻 ----');
const pulseTabOk = await p
  .locator('button', { hasText: '恒指脉搏' })
  .first()
  .click({ timeout: 6000 })
  .then(() => true)
  .catch(() => false);
hard(pulseTabOk, '恒指脉搏 Tab 点击');
await waitForText((t) => t.includes('恒生行业热力图'), 12000);
await p.waitForTimeout(1500); // ECharts canvas 渲染余量（图无文本信号可等）

// 取最大 canvas（treemap 560px 高，全页最大）
async function biggestCanvasBox() {
  const boxes = [];
  for (const c of await p.locator('canvas').all()) {
    const bb = await c.boundingBox().catch(() => null);
    if (bb) boxes.push(bb);
  }
  boxes.sort((a, b2) => b2.width * b2.height - a.width * a.height);
  return boxes[0] || null;
}

let box = await biggestCanvasBox();
if (!box) {
  hard(false, 'treemap canvas 未找到');
} else {
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  // L1 点击 → L2
  await p.mouse.click(cx, cy);
  const inL2 = await waitForText((t) => t.includes('成分股 · 面积=成交额'), 10000);
  soft(inL2, 'L1 色块点击 → 下钻 L2（面包屑「成分股 · 面积=成交额」）');
  if (inL2) {
    // 面包屑随点击立即出现，但摘要行/成分图要等 drill 数据落地 → 确定性等待
    const l2Ready = await waitForText((t) => t.includes('只 · 合计'), 12000);
    soft(l2Ready, 'L2 摘要行（只数 / 合计成交额）');
    await p.screenshot({ path: `${OUT}/hk-ma-drill-L2.png` });

    // L2 点击 → 个股终端（等 treemap 布局动画稳定；中心点瞬时落空时按象限重试）
    await p.waitForTimeout(1200);
    box = await biggestCanvasBox();
    if (!box) {
      soft(false, 'L2 treemap canvas 未渲染（drill 数据未落地）');
    } else {
      const spots = [
        [0.5, 0.5],
        [0.3, 0.35],
        [0.7, 0.35],
        [0.3, 0.65],
        [0.7, 0.65],
      ];
      let opened = false;
      for (const [rx, ry] of spots) {
        await p.mouse.move(box.x + box.width * rx, box.y + box.height * ry);
        await p.waitForTimeout(150);
        await p.mouse.click(box.x + box.width * rx, box.y + box.height * ry);
        // 浮窗打开是异步的（含终端懒加载），轮询而非固定等待
        const t0 = Date.now();
        while (Date.now() - t0 < 2500) {
          if (await terminalOpen()) {
            opened = true;
            break;
          }
          await p.waitForTimeout(300);
        }
        if (opened) break;
      }
      soft(opened, 'L2 个股色块点击 → 个股终端浮窗弹出');
      if (opened) await p.screenshot({ path: `${OUT}/hk-ma-drill-terminal.png` });
      await closeTerminal();
    }

    // 面包屑返回 L1
    const back = p.locator('button', { hasText: /^恒生行业$/ }).first();
    if (await back.count()) {
      await back.click().catch(() => undefined);
      const backL1 = await waitForText((t) => t.includes('平均涨幅 / 成交额 / 领涨龙头') && !t.includes('成分股 · 面积=成交额'), 8000);
      soft(backL1, '面包屑「恒生行业」返回 L1');
    } else {
      soft(false, '面包屑返回按钮未找到');
    }
  }
}

// ---- 机会雷达：个股行点击 → 个股终端 ----
console.log('\n---- 机会雷达个股点击弹窗 ----');
const oppTabOk = await p
  .locator('button', { hasText: '机会雷达' })
  .first()
  .click({ timeout: 6000 })
  .then(() => true)
  .catch(() => false);
hard(oppTabOk, '机会雷达 Tab 点击');
// 「六柱分位」列注释仅在有数据行时渲染 → 数据就位的确定性判据
const oppReady = await waitForText(
  (t) => t.includes('机会个股榜（综合分降序）') && t.includes('六柱分位'),
  25000,
);
hard(oppReady, '机会雷达面板数据渲染（个股榜 + 六柱分位）');
const firstRow = p.locator('[title$="个股终端"]').first();
if (await firstRow.count()) {
  const rowTitle = await firstRow.getAttribute('title').catch(() => '');
  await firstRow.click().catch(() => undefined);
  let opened = false;
  const t0 = Date.now();
  while (Date.now() - t0 < 4000) {
    if (await terminalOpen()) {
      opened = true;
      break;
    }
    await p.waitForTimeout(300);
  }
  hard(opened, `机会雷达个股行「${rowTitle || '?'}」点击 → 个股终端弹出`);
  if (opened) {
    await p.screenshot({ path: `${OUT}/hk-ma-opportunity-terminal.png` });
    // 终端懒加载 → 联想选中 → K线卡渲染；「前复权」控件仅选中后出现，
    // 可区分「自动选中成功」与空态欢迎页（空态文本也含「个股」，不能拿它当判据）
    const termReady = await waitForText(
      (t) => t.includes('前复权') && !t.includes('在上方搜索框输入代码'),
      15000,
    );
    soft(termReady, '终端主体自动选中标的并渲染 K 线区（非空态）');
    if (termReady) await p.screenshot({ path: `${OUT}/hk-ma-opportunity-terminal-loaded.png` });
  }
  await closeTerminal();
  await p.waitForTimeout(800);
  const closedOk = !(await terminalOpen());
  soft(closedOk, '关闭后浮窗消失');
} else {
  hard(false, '机会雷达个股行（title=打开 … 个股终端）未找到');
}

// ---- 行业榜过滤个股榜 ----
const sectorRow = p.locator('[title^="只看「"]').first();
if (await sectorRow.count()) {
  await sectorRow.click().catch(() => undefined);
  await p.waitForTimeout(2500);
  const filtered = await p.locator('[title="清除行业过滤"]').count() > 0;
  soft(filtered, '机会行业榜点行 → 个股榜行业过滤生效（出现「清除行业过滤」）');
  await p.screenshot({ path: `${OUT}/hk-ma-opportunity-filtered.png` });
} else {
  soft(false, '机会行业榜行未找到（可能无达标行业）');
}

// ---- AH 溢价停更横幅（动态判据） ----
const valTabOk = await p
  .locator('button', { hasText: '估值主题' })
  .first()
  .click({ timeout: 6000 })
  .then(() => true)
  .catch(() => false);
hard(valTabOk, '估值主题 Tab 点击');
hard(await waitForText((t) => t.includes('AH 溢价 · 两地比价'), 12000), '估值主题面板渲染（AH 比价）');
f = await bodyText();
if (f.includes('该数据源已停止更新')) {
  const m = f.match(/最新数据 (\d{4}-\d{2}-\d{2})/);
  console.log(`· AH 溢价：停更横幅可见（最新数据 ${m ? m[1] : '?'}）—— 动态判据生效，源侧恢复后自动消失`);
} else {
  console.log('· AH 溢价：无停更横幅（数据新鲜）');
}
await p.screenshot({ path: `${OUT}/hk-ma-ah-premium.png` });

// ---- 第二面板行点击弹窗（南向增持榜） ----
const southTabOk = await p
  .locator('button', { hasText: '南向资金' })
  .first()
  .click({ timeout: 6000 })
  .then(() => true)
  .catch(() => false);
hard(southTabOk, '南向资金 Tab 点击');
// 面板标题先于行数据渲染 → 以可点击行出现为数据就位判据
let southRowReady = false;
for (let i = 0; i < 40; i++) {
  if ((await p.locator('[title$="个股终端"]').count()) > 0) {
    southRowReady = true;
    break;
  }
  await p.waitForTimeout(500);
}
hard(southRowReady, '南向面板数据渲染（含可点击行）');
const southRow = p.locator('[title$="个股终端"]').first();
if (await southRow.count()) {
  await southRow.click().catch(() => undefined);
  let sOpened = false;
  const ts = Date.now();
  while (Date.now() - ts < 4000) {
    if (await terminalOpen()) {
      sOpened = true;
      break;
    }
    await p.waitForTimeout(300);
  }
  soft(sOpened, '南向增持榜行点击 → 个股终端弹出');
  await closeTerminal();
} else {
  soft(false, '南向榜行未找到');
}

// ---- 接口面 ----
console.log('\n---- /market-analysis-hk 接口 ----');
const uniq = [...new Set(apiCalls)];
console.log(uniq.length ? uniq.join('\n') : '(无调用)');
for (const ep of ['/opportunity/stocks', '/opportunity/sectors', '/opportunity/risk', '/opportunity/summary', '/heatmap/drill']) {
  hard(uniq.some((c) => c.startsWith('200') && c.includes(ep)), `接口 200：${ep}`);
}

console.log('\n---- 错误 ----');
console.log(errors.length ? [...new Set(errors)].slice(0, 10).join('\n') : '(无)');

console.log(`\n==== 硬断言失败 ${hardFails.length} / 软断言失败 ${softFails.length} ====`);
if (hardFails.length) console.log('硬失败：\n- ' + hardFails.join('\n- '));
if (softFails.length) console.log('软失败：\n- ' + softFails.join('\n- '));
await b.close();
process.exitCode = hardFails.length ? 1 : 0;
