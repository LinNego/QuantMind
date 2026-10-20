/**
 * 「智能体台账只统计通达信实盘」验收探针（2026-10-20）。
 *
 * 断的是用户当轮反馈的四件事：
 *   1) 模型卡收益 = PG 账本区间收益率（period_return），不是「净值 ÷ 10 万」的
 *      迁移缺口口径（flash 曾显示 -14% 一类假亏损）；
 *   2) 决策日志「全部轮次」拿到**全量**历史回合（flash ≥ 330 轮），不再被 80/300 条截断；
 *   3) 四档筛选互斥不漏：全部 = 有成交 + 无成交 + 盘后复盘；复盘轮只进「盘后复盘」；
 *   4) 模型详情页出现「通达信桥实盘」「区间收益率」，持仓/成交表无模拟盘残留。
 *
 * 硬规矩（沿用 probe_live_arena_tabs / probe_eval_center_interactions）：
 *   - 深链必须写全 `#/`（HashRouter；缺了静默落首页）；
 *   - 点栏必须验激活（`bg-blue-50`），只看「页面里有字」是假通过；
 *   - 等待一律用 locator / 轮询，不拿固定 waitForTimeout 当判据。
 *
 * 前置：quantmind-web（3080）在跑、后端可达、admin/admin123 可登录。
 * 用法：node electron/tests/probe_agent_ledger_live.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.PROBE_BASE || 'http://localhost:3080';
const USER = process.env.PROBE_USER || 'admin';
const PASS = process.env.PROBE_PASS || 'admin123';

const results = [];
const check = (name, pass, detail = '') => {
  results.push({ name, pass, detail });
  console.log(`${pass ? '✅' : '❌'} ${name}${detail ? `  — ${detail}` : ''}`);
};

const browser = await chromium.launch({
  executablePath: '/usr/bin/google-chrome-stable',
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });
const errs = [];
const bad = [];
page.on('pageerror', (e) => errs.push('[PAGEERROR] ' + (e.message || '').slice(0, 200)));
page.on('response', (r) => {
  const u = r.url();
  if (/\/api\/v1\/agent-arena\//.test(u) && r.status() >= 500) bad.push(`${r.status()} ${u.replace(BASE, '')}`);
});

const clickByText = async (re, scope = 'button') => {
  const items = page.locator(scope);
  for (let i = 0, n = await items.count(); i < n; i++) {
    const t = ((await items.nth(i).innerText().catch(() => '')) || '').replace(/\s/g, '');
    if (re.test(t)) {
      await items.nth(i).click({ timeout: 4000 });
      return true;
    }
  }
  return false;
};

const dismissModals = async () => {
  for (let round = 0; round < 8; round++) {
    const blocking = await page.evaluate(
      () => Array.from(document.querySelectorAll('.ant-modal-wrap'))
        .filter((m) => getComputedStyle(m).display !== 'none').length,
    );
    if (!blocking) return true;
    if (await clickByText(/稍后再答|同意|确认|我知道了|已阅读|知道了|跳过/, '.ant-modal:visible button')) {
      await page.waitForTimeout(800);
      continue;
    }
    await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(600);
  }
  return false;
};

const apiJson = (path) =>
  page.evaluate(async (p) => {
    const token = localStorage.getItem('access_token') || '';
    const res = await fetch(`/api/v1${p}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (!res.ok) return { __status: res.status };
    return res.json();
  }, path);

/** 反复读直到 predicate 成立或超时（确定性等待，不靠固定 sleep） */
const waitUntil = async (fn, timeoutMs = 15000, stepMs = 250) => {
  const t0 = Date.now();
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() - t0 > timeoutMs) return null;
    await page.waitForTimeout(stepMs);
  }
};

const cardCount = () => page.locator('.mc-card').count();
const clickFilter = async (label) => {
  const ok = await clickByText(new RegExp(`^${label}$`), '.mc-filter');
  if (!ok) return false;
  await page.waitForTimeout(300);
  return true;
};

console.log(`\n=== 智能体台账（通达信实盘口径）验收 @ ${BASE} ===\n`);

// ── 登录 + 进入实盘栏 ─────────────────────────────────────────────────────
await page.goto(`${BASE}/#/auth/login`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(4000);
const inputs = page.locator('input:visible');
if ((await inputs.count()) >= 2) {
  await inputs.nth(0).fill(USER);
  await inputs.nth(1).fill(PASS);
  await clickByText(/登录/);
  await page.waitForTimeout(6000);
}
await dismissModals();

await page.goto(`${BASE}/#/live`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(7000);
await dismissModals();

// ── 1. 智能体台账栏激活 ────────────────────────────────────────────────────
{
  const btn = page.locator('button').filter({ hasText: /^智能体台账$/ }).first();
  await btn.click({ timeout: 8000 }).catch(async () => {
    await dismissModals();
    await btn.click({ force: true });
  });
  const active = await waitUntil(() =>
    page.evaluate(() => {
      const b = Array.from(document.querySelectorAll('button')).find((x) => (x.textContent || '').trim() === '智能体台账');
      return !!b && /bg-blue-50/.test(b.className);
    }),
  );
  check('智能体台账栏已激活', !!active);
  await dismissModals();
}

// ── 2. 模型卡收益 = PG 区间收益率 ─────────────────────────────────────────
let flashCardText = '';
{
  const overview = await apiJson('/agent-arena/overview');
  const rows = overview?.data?.markets?.cn ?? [];
  const want = new Map();
  for (const r of rows) want.set(r.name, r.summary?.period_return ?? null);

  // 反「空通过」：老进程的 /overview 是文件口径，summary 里根本没有 period_return，
  // 下面的逐卡断言会全部 continue 掉、一条都不打印却显示 PASS。先断数据面就位。
  const withSummary = rows.filter((r) => r.summary != null);
  const withPeriod = withSummary.filter((r) => r.summary.period_return != null);
  check(
    'A股 overview 为 PG 实盘口径（每行带 period_return）',
    withSummary.length > 0 && withPeriod.length === withSummary.length,
    `${withPeriod.length}/${withSummary.length} 行（共 ${rows.length} 行）`,
  );

  const cards = await waitUntil(async () => {
    const n = await page.locator('.model-card-mini').count();
    return n ? n : null;
  }, 20000);
  check('模型卡已渲染', !!cards && cards >= 1, `count=${cards ?? 0}`);

  const cardTexts = await page.locator('.model-card-mini').allInnerTexts();
  for (const r of rows) {
    const period = want.get(r.name);
    if (period == null) continue;
    const pct = `${period > 0 ? '+' : ''}${(period * 100).toFixed(2)}%`;
    // 卡片按 displayName 展示；用内容包含百分比字符串判定（同 fmtPct 口径）
    const hit = cardTexts.find((t) => t.includes(pct));
    if (r.name.includes('flash')) flashCardText = hit ?? '';
    check(`模型卡 ${r.name} 显示区间收益 ${pct}`, !!hit, hit ? '' : '未找到');
  }
  check('模型卡不再出现 -14%/-15% 迁移缺口假亏损', !cardTexts.some((t) => /-(1[3-9])\.\d+%/.test(t)));
  await page.screenshot({ path: '/tmp/probe_ledger_cards.png', fullPage: false });
}

// ── 3. 下钻 flash → 决策日志全量 + 四档筛选 ────────────────────────────────
{
  // 点 flash 卡（按卡内文本定位；点错卡也能跑，sub 会读回真实 agent）
  let clicked = false;
  const cards = page.locator('.model-card-mini');
  for (let i = 0, n = await cards.count(); i < n; i++) {
    const t = (await cards.nth(i).innerText().catch(() => '')) || '';
    if (/flash/i.test(t)) {
      await cards.nth(i).click({ timeout: 5000 });
      clicked = true;
      break;
    }
  }
  if (!clicked && (await cards.count())) await cards.nth(0).click({ timeout: 5000 });

  const agent = await waitUntil(async () => {
    const el = page.locator('.mdp-head-sub').first();
    if (!(await el.count())) return null;
    const t = ((await el.innerText().catch(() => '')) || '').trim();
    return t || null;
  }, 15000);
  check('下钻到模型详情', !!agent, agent ?? '');

  const liveChip = await page.locator('.mdp-live').count();
  check('详情页标注「通达信桥实盘」', liveChip > 0);
  const heroText = ((await page.locator('.mdp-hero').innerText().catch(() => '')) || '').replace(/\s/g, '');
  check('详情页用「区间收益率」口径', /区间收益率/.test(heroText));
  check('详情页不出现模拟盘标识', !/模拟盘/.test(((await page.locator('.mdp-head').innerText().catch(() => '')) || '')));

  const tabOk = await clickByText(/^决策日志$/, 'button.tab');
  check('决策日志页签可点开', tabOk);

  const loaded = await waitUntil(async () => ((await cardCount()) > 0 ? true : null), 20000);
  check('决策日志已渲染', !!loaded);

  const toolbar = ((await page.locator('.mc-toolbar-stat').innerText().catch(() => '')) || '').replace(/\s/g, '');
  const m = toolbar.match(/本页(\d+)轮·有成交(\d+)轮·复盘(\d+)轮/);
  check('工具栏三轮统计可解析', !!m, toolbar);
  const allN = m ? Number(m[1]) : 0;
  const tradeN = m ? Number(m[2]) : 0;
  const reviewN = m ? Number(m[3]) : 0;

  check('全部轮次未被截断（≥300 轮）', allN >= 300, `本页 ${allN} 轮`);
  check('复盘轮已识别（≥20 轮）', reviewN >= 20, `复盘 ${reviewN} 轮`);

  // 四档筛选：逐档点击后数卡片，断互斥不漏
  await clickFilter('全部轮次');
  const domAll = await waitUntil(async () => {
    const n = await cardCount();
    return n === allN ? n : null;
  }, 10000);
  check('「全部轮次」卡片数 = 工具栏统计', domAll === allN, `DOM ${domAll} / 统计 ${allN}`);

  await clickFilter('有成交');
  const domTrade = await waitUntil(async () => {
    const n = await cardCount();
    return n === tradeN ? n : null;
  }, 10000);
  check('「有成交」卡片数 = 工具栏统计', domTrade === tradeN, `DOM ${domTrade} / 统计 ${tradeN}`);

  await clickFilter('盘后复盘');
  const domReview = await waitUntil(async () => {
    const n = await cardCount();
    return n === reviewN ? n : null;
  }, 10000);
  check('「盘后复盘」卡片数 = 工具栏统计', domReview === reviewN, `DOM ${domReview} / 统计 ${reviewN}`);

  await clickFilter('无成交');
  const domIdle = await waitUntil(async () => {
    const n = await cardCount();
    return n > 0 && n !== domTrade && n !== domReview ? n : null;
  }, 10000);
  check('「无成交」有数据且与其余两档不同', !!domIdle && domIdle > 0, `DOM ${domIdle ?? 0}`);

  const sum = (domTrade ?? 0) + (domIdle ?? 0) + (domReview ?? 0);
  check('四档互斥不漏：有成交 + 无成交 + 复盘 = 全部', sum === allN, `${domTrade ?? 0} + ${domIdle ?? 0} + ${domReview ?? 0} = ${sum} vs ${allN}`);

  // 复盘轮卡片应带「复盘轮」标记
  await clickFilter('盘后复盘');
  const reviewMarks = await page.locator('.mc-kind.review').count();
  check('复盘卡片带「复盘轮」标记', reviewMarks >= Math.min(reviewN, 1), `标记 ${reviewMarks} / ${reviewN}`);

  // 与后端日志直接对账（review 行数）
  const logResp = await apiJson(`/agent-arena/agents/${encodeURIComponent(agent || '')}/logs?market=cn&limit=0`);
  const lines = logResp?.data ?? [];
  const reviewLines = lines.filter((l) => l.kind === 'review' || l.mode === 'review').length;
  check('复盘轮数与后端日志一致', reviewLines === reviewN, `API ${reviewLines} / UI ${reviewN}`);
  check('全量日志行数 ≥ UI 轮数（单行=单回合）', lines.length >= allN, `API ${lines.length} 行 / UI ${allN} 轮`);

  await page.screenshot({ path: '/tmp/probe_ledger_logs.png', fullPage: false });
}

// ── 4. 全链路零 pageerror / 零 5xx ────────────────────────────────────────
check('无 pageerror', errs.length === 0, errs.join(' | ').slice(0, 400));
check('arena 端点无 5xx', bad.length === 0, bad.slice(0, 5).join(' | '));

await browser.close();

const failed = results.filter((r) => !r.pass);
console.log(`\n${failed.length === 0 ? 'PASS' : 'FAIL'} — ${results.length - failed.length}/${results.length}\n`);
process.exit(failed.length === 0 ? 0 : 1);
