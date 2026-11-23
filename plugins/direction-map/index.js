/**
 * 方向地图（Direction Map）Host 半。
 *
 * 两条只读数据通道，都是把「参数 → JSON」交给同一个 Python 内核：
 *   /direction-map/query?symbol=601012.SH   单公司方向地图
 *   /direction-map/chain                    光伏产业链全景
 *
 * 财务计算一律在 Python 内核里（scripts/direction_map/），本文件只做参数校验与转发。
 */
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

const run = promisify(execFile)

export const inject = ['webServer']

const SYMBOL_RE = /^\d{6}\.(SH|SZ|BJ)$/

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
  })
}

export function apply(ctx, config = {}) {
  const dir = config.scriptDir || '/root/QuantMind/scripts/direction_map'
  const python = config.python || 'python3'
  const timeoutMs = Number(config.timeoutMs || 120000)

  async function execJson(script, args) {
    const { stdout } = await run(python, [`${dir}/${script}`, ...args], {
      timeout: timeoutMs,
      maxBuffer: 16 * 1024 * 1024,
    })
    return JSON.parse(stdout)
  }

  function failure(error) {
    const message = error && error.message ? error.message : String(error)
    return json({ error: `分析内核执行失败：${message}` }, 500)
  }

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: '/direction-map/query',
    resource: async (request) => {
      const symbol = (new URL(request.url).searchParams.get('symbol') || '').trim().toUpperCase()
      if (!SYMBOL_RE.test(symbol)) {
        return json({ error: '股票代码格式应为 600000.SH / 000001.SZ / 830799.BJ' }, 400)
      }
      try {
        return json(await execJson('direction_map.py', ['--symbol', symbol, '--json']))
      } catch (error) {
        return failure(error)
      }
    },
  }), 'direction-map query route')

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: '/direction-map/chain',
    resource: async () => {
      try {
        return json(await execJson('chain.py', ['--json']))
      } catch (error) {
        return failure(error)
      }
    },
  }), 'direction-map chain route')
}
