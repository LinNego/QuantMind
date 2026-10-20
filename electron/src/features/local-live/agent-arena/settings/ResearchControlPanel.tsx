import { useCallback, useEffect, useState } from 'react';
import { BrainCircuit, CheckCircle2, Eye, EyeOff, Loader2, PlugZap, Save, XCircle } from 'lucide-react';
import { SERVICE_ENDPOINTS } from '../../../../config/services';

/**
 * 「设置 → 研究总控」：给市场研究 agent 单独配一套 LLM（端点 / 模型 / key）。
 *
 * 为什么单独一栏：研究总控要写长报告（五档复盘 + 次日 brief），供应商、token 预算、
 * 温度都和盘中决策模型不同。共用交易模型那套 key 会出现「换研究模型把交易模型一起换掉」。
 * 后端存在 `config/runtime.env`（`QM_RESEARCH_LLM_*`），保存后宿主 cron 下一档即生效，
 * 不需要重启服务；key 只写不回，这里只显示掩码。
 *
 * 调度不在这里改：五档由宿主 crontab 触发（北京时间见表内），本面板只展示时间与当日状态。
 */

interface SlotSchedule {
  time: string;
  cron: string;
  label: string;
}

interface ResearchConfig {
  model: string;
  provider: string;
  base_url: string;
  key_configured: boolean;
  key_mask: string;
  source: 'research' | 'fallback' | string;
  timeout: number;
  max_tokens: number;
  temperature: number;
  ready: boolean;
  error: string;
  schedule: Record<string, SlotSchedule>;
}

interface SlotStatus {
  slot: string;
  label: string;
  time: string;
  report: boolean;
  ts: string;
  bytes: number;
  state: { ok?: boolean; error?: string; updated_at?: string };
}

interface ResearchStatus {
  day: string;
  target: string;
  slots: SlotStatus[];
  brief_ready: boolean;
}

interface TestResult {
  ok: boolean;
  model: string;
  provider: string;
  latency_ms: number | null;
  reply: string;
  error: string;
  source: string;
}

const API_BASE = `${SERVICE_ENDPOINTS.API_GATEWAY}/admin/research-control`;
const PROVIDERS = [
  { value: 'glm', label: 'GLM（智谱）' },
  { value: 'deepseek', label: 'DeepSeek' },
  { value: 'openai', label: 'OpenAI 兼容' },
  { value: 'custom', label: '自定义' },
];

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${localStorage.getItem('access_token') || ''}`,
      ...(init?.headers || {}),
    },
  });
  const body = (await res.json().catch(() => ({}))) as { detail?: string; data?: T };
  if (!res.ok) throw new Error(body?.detail || `HTTP ${res.status}`);
  return body as T;
}

const fmtBytes = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k 字` : `${n} 字`);

function SlotState({ row }: { row: SlotStatus }) {
  if (row.report) {
    return (
      <span className="inline-flex items-center gap-1 text-emerald-600">
        <CheckCircle2 size={14} /> 已完成 · {fmtBytes(row.bytes)}
      </span>
    );
  }
  if (row.state?.error) {
    return (
      <span className="inline-flex items-center gap-1 text-rose-600" title={row.state.error}>
        <XCircle size={14} /> 失败：{row.state.error.slice(0, 40)}
      </span>
    );
  }
  return <span className="text-gray-400">未运行</span>;
}

export default function ResearchControlPanel() {
  const [config, setConfig] = useState<ResearchConfig | null>(null);
  const [status, setStatus] = useState<ResearchStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [message, setMessage] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [showKey, setShowKey] = useState(false);

  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [provider, setProvider] = useState('glm');
  const [apiKey, setApiKey] = useState('');

  const applyConfig = useCallback((data: ResearchConfig) => {
    setConfig(data);
    setBaseUrl(data.base_url || '');
    setModel(data.model || '');
    setProvider(data.provider || 'glm');
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [cfg, st] = await Promise.all([
        request<{ data: ResearchConfig }>('/config'),
        request<{ data: ResearchStatus }>('/status').catch(() => null),
      ]);
      applyConfig(cfg.data);
      if (st) setStatus(st.data);
    } catch (error: unknown) {
      setMessage({ kind: 'err', text: error instanceof Error ? error.message : '读取配置失败' });
    } finally {
      setLoading(false);
    }
  }, [applyConfig]);

  useEffect(() => {
    void load();
  }, [load]);

  const body = () => ({
    base_url: baseUrl.trim(),
    model: model.trim(),
    provider,
    // 留空 = 沿用已保存的 key（后端只写不回，前端拿不到明文）
    ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
  });

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    setMessage(null);
    try {
      const res = await request<{ success: boolean; data: TestResult }>('/config/test', {
        method: 'POST',
        body: JSON.stringify(body()),
      });
      setTestResult(res.data);
    } catch (error: unknown) {
      setMessage({ kind: 'err', text: error instanceof Error ? error.message : '测试失败' });
    } finally {
      setTesting(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setMessage(null);
    try {
      const res = await request<{ data: ResearchConfig }>('/config', {
        method: 'PUT',
        body: JSON.stringify(body()),
      });
      applyConfig(res.data);
      setApiKey('');
      setMessage({ kind: 'ok', text: '已保存到 config/runtime.env，宿主 cron 下一档生效（无需重启）' });
      void load();
    } catch (error: unknown) {
      setMessage({ kind: 'err', text: error instanceof Error ? error.message : '保存失败' });
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    if (!window.confirm('清空研究总控的独立 key / 端点，回落到 GLM_*/OPENAI_* 那套交易模型配置？')) return;
    setSaving(true);
    setMessage(null);
    try {
      const res = await request<{ data: ResearchConfig }>('/config/reset', { method: 'POST' });
      applyConfig(res.data);
      setApiKey('');
      setMessage({ kind: 'ok', text: '已清空独立配置，当前回落到交易模型那套供应商变量' });
      void load();
    } catch (error: unknown) {
      setMessage({ kind: 'err', text: error instanceof Error ? error.message : '清空失败' });
    } finally {
      setSaving(false);
    }
  };

  const sourceBadge = () => {
    if (!config) return null;
    if (config.source === 'research') {
      return (
        <span className="rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200">
          独立 key（QM_RESEARCH_LLM_*）
        </span>
      );
    }
    return (
      <span className="rounded-full bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-700 ring-1 ring-amber-200">
        回落交易模型 key
      </span>
    );
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-gray-500">
        <Loader2 size={16} className="animate-spin" /> 读取研究总控配置…
      </div>
    );
  }

  return (
    <div className="space-y-6 p-1">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-gray-200 pb-4">
        <div>
          <h2 className="flex items-center gap-2 text-lg font-semibold text-gray-900">
            <BrainCircuit size={20} className="text-indigo-600" />
            研究总控
          </h2>
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-gray-500">
            站在所有交易 / 新闻 / 对话 agent 之上的复盘大脑。每天五档（午间、收盘、推理、新闻、夜间综合）
            自动生成报告与次日 brief，第二天其他 agent 直接读取。这里单独配置它自己的模型与 key。
          </p>
        </div>
        <div className="flex flex-col items-end gap-2">
          {sourceBadge()}
          {config && (
            <span className={`text-xs ${config.ready ? 'text-emerald-600' : 'text-rose-600'}`}>
              {config.ready ? '配置可用' : config.error || '配置不可用'}
            </span>
          )}
        </div>
      </header>

      {message && (
        <div
          className={`rounded-lg px-3 py-2 text-sm ${
            message.kind === 'ok' ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-50 text-rose-700'
          }`}
        >
          {message.text}
        </div>
      )}

      <section className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-gray-900">模型与密钥</h3>
          {config && (
            <span className="text-xs text-gray-500">
              已保存 key：{config.key_configured ? config.key_mask || '已配置' : '未配置独立 key'}
            </span>
          )}
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <label className="block text-sm">
            <span className="mb-1 block font-medium text-gray-700">API 端点（含 /v1）</span>
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://open.bigmodel.cn/api/paas/v4"
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
            />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block font-medium text-gray-700">模型</span>
            <input
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="glm-5.3-flash"
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
            />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block font-medium text-gray-700">供应商</span>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
            >
              {PROVIDERS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <label className="block text-sm">
            <span className="mb-1 block font-medium text-gray-700">
              API Key <span className="font-normal text-gray-400">（留空 = 沿用已保存）</span>
            </span>
            <div className="relative">
              <input
                type={showKey ? 'text' : 'password'}
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={config?.key_configured ? '••••••••（已保存，留空不改）' : '粘贴独立 key'}
                autoComplete="new-password"
                className="w-full rounded-lg border border-gray-300 px-3 py-2 pr-10 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
              />
              <button
                type="button"
                onClick={() => setShowKey((v) => !v)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                aria-label={showKey ? '隐藏 key' : '显示 key'}
              >
                {showKey ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </label>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={handleTest}
            disabled={testing || saving}
            className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-sm font-medium text-indigo-700 hover:bg-indigo-100 disabled:opacity-50"
          >
            {testing ? <Loader2 size={15} className="animate-spin" /> : <PlugZap size={15} />}
            测试连接
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={saving || testing}
            className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            {saving ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}
            保存
          </button>
          <button
            type="button"
            onClick={handleReset}
            disabled={saving || testing}
            className="ml-auto text-xs text-gray-400 underline-offset-2 hover:text-rose-600 hover:underline disabled:opacity-50"
          >
            清空独立配置（回落交易模型 key）
          </button>
        </div>

        {testResult && (
          <div
            className={`mt-4 rounded-lg px-3 py-2 text-sm ${
              testResult.ok ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-50 text-rose-700'
            }`}
          >
            {testResult.ok ? (
              <>
                连接正常 · {testResult.model} · {testResult.latency_ms}ms · 回复「{testResult.reply}」
              </>
            ) : (
              <>连接失败：{testResult.error}</>
            )}
          </div>
        )}
        <p className="mt-3 text-xs text-gray-400">
          参数默认：timeout {config?.timeout ?? '—'}s · max_tokens {config?.max_tokens ?? '—'} · temperature{' '}
          {config?.temperature ?? '—'}。key 只写不回；保存后宿主 cron 下一档读取同一份
          config/runtime.env，无需重启服务。
        </p>
      </section>

      <section className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-sm font-semibold text-gray-900">今日五档复盘</h3>
          {status && (
            <span className="text-xs text-gray-500">
              复盘日 {status.day} · 目标交易日 {status.target} · 次日 brief{' '}
              {status.brief_ready ? '已就绪' : '尚未生成'}
            </span>
          )}
        </div>
        {status ? (
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-gray-100 text-xs text-gray-500">
                <th className="py-2 font-medium">档位</th>
                <th className="py-2 font-medium">北京时间</th>
                <th className="py-2 font-medium">状态</th>
                <th className="py-2 font-medium">完成时间</th>
              </tr>
            </thead>
            <tbody>
              {status.slots.map((row) => (
                <tr key={row.slot} className="border-b border-gray-50 last:border-0">
                  <td className="py-2 font-medium text-gray-800">{row.label}</td>
                  <td className="py-2 text-gray-500">{row.time}</td>
                  <td className="py-2">
                    <SlotState row={row} />
                  </td>
                  <td className="py-2 text-xs text-gray-500">{row.ts ? row.ts.replace('T', ' ').slice(0, 16) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="text-sm text-gray-400">状态暂不可读</p>
        )}
        <p className="mt-3 text-xs text-gray-400">
          触发时间不在这里改：五档由宿主 crontab 驱动（JST = 北京 + 1）。报告落 data/research_control/reports/，
          次日 brief 落 data/research_control/brief/，其他 agent 按目标交易日直接读取。
        </p>
      </section>
    </div>
  );
}
