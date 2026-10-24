import React, { useEffect, useMemo, useState } from 'react';
import { Button, Card, Empty, Select, Spin, Tag, Typography, Collapse } from 'antd';
import { TrendingUp, Info } from 'lucide-react';
import ReactECharts from 'echarts-for-react';
import { modelTrainingService } from '../services/modelTrainingService';
import {
  FALLBACK_INDEX_OPTIONS,
  INDEX_COLORS,
  rebaseIndexPoints,
  type RegimeIndexOption,
  type RegimeIndexSeries,
} from './marketRegimeModel';

const { Text } = Typography;

export const MarketRegimePanel: React.FC<{ modelId: string }> = ({ modelId }) => {
  const [loading, setLoading] = useState(true);
  const [data, setData] = useState<any>(null);
  // 默认沪深300；用户可多选（含「全部叠加」）
  const [selectedIndices, setSelectedIndices] = useState<string[]>(['000300.SH']);
  // 均分是信号日 T 的分数，指数默认错开 T+1（次日可交易）；可切 T+N / 不错开
  const [indexOffset, setIndexOffset] = useState(1);

  useEffect(() => {
    if (!modelId) return;
    setLoading(true);
    modelTrainingService
      .getMarketRegime(modelId, 90, indexOffset)
      .then(setData)
      .catch(() => setData({ series: [], indices: [] }))
      .finally(() => setLoading(false));
  }, [modelId, indexOffset]);

  const series: any[] = data?.series ?? [];
  const current = data?.current ?? null;
  const indexOptions: RegimeIndexOption[] =
    (data?.index_options?.length ?? 0) > 0 ? data.index_options : FALLBACK_INDEX_OPTIONS;
  const availableIndices: RegimeIndexSeries[] = data?.indices ?? [];

  // 模型切换后若原选择不在新数据里，收敛到首个可用指数（不静默画空线）
  useEffect(() => {
    if (!availableIndices.length) return;
    const available = availableIndices.map((x) => x.code);
    const kept = selectedIndices.filter((code) => available.includes(code));
    if (kept.length === selectedIndices.length) return;
    setSelectedIndices(kept.length > 0 ? kept : [available[0]]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  const selectedSeries = availableIndices.filter((x) => selectedIndices.includes(x.code));

  const offsetOptions = useMemo(() => {
    const horizon = Number(data?.horizon_days) || 1;
    const options = [
      { value: 0, label: '不错开（T+0）' },
      { value: 1, label: '错开 T+1（次日）' },
    ];
    if (horizon > 1) {
      options.push({ value: horizon, label: `错开 T+${horizon}（模型周期）` });
    }
    return options;
  }, [data?.horizon_days]);

  const option = useMemo(() => {
    if (!series.length) return null;
    const dates = series.map((s: any) => s.trade_date);
    const vals = series.map((s: any) => s.avg_score);
    const hasIndex = selectedSeries.length > 0;
    const indexLines = selectedSeries.map((ix) => ({
      name: ix.name,
      type: 'line' as const,
      yAxisIndex: 1,
      data: rebaseIndexPoints(ix.points, dates),
      connectNulls: true,
      showSymbol: false,
      smooth: false,
      lineStyle: { width: 1.5, color: INDEX_COLORS[ix.code] ?? '#64748b' },
      itemStyle: { color: INDEX_COLORS[ix.code] ?? '#64748b' },
      emphasis: { focus: 'series' as const },
    }));
    return {
      color: ['#3b82f6', ...selectedSeries.map((ix) => INDEX_COLORS[ix.code] ?? '#64748b')],
      tooltip: {
        trigger: 'axis',
        formatter: (ps: any) => {
          const list = Array.isArray(ps) ? ps : [ps];
          const idx = list[0]?.dataIndex ?? 0;
          const row = series[idx];
          const lines = [
            `<b>${row.trade_date}</b>`,
            `均分 ${row.avg_score} · 中位 ${row.median_score} · 样本 ${row.count}`,
          ];
          for (const p of list) {
            if (p.seriesName === '均分') continue;
            const ix = availableIndices.find((x) => x.name === p.seriesName);
            const point = ix?.points.find(
              (pt) => String(pt.trade_date).slice(0, 10) === row.trade_date,
            );
            const indexDay =
              point?.index_date && point.index_date !== row.trade_date
                ? ` · 指数日 ${point.index_date}`
                : '';
            lines.push(
              `${p.marker ?? ''}${p.seriesName}: ${
                p.value == null ? '—' : Number(p.value).toFixed(2)
              }${point?.close != null ? `（收盘 ${point.close}${indexDay}）` : ''}`,
            );
          }
          return lines.join('<br/>');
        },
      },
      legend: {
        top: 0,
        itemWidth: 14,
        itemHeight: 8,
        textStyle: { fontSize: 10 },
        data: ['均分', ...selectedSeries.map((x) => x.name)],
      },
      grid: { left: 52, right: hasIndex ? 56 : 16, top: 30, bottom: 28 },
      xAxis: {
        type: 'category',
        data: dates,
        boundaryGap: false,
        axisLabel: { fontSize: 10, color: '#64748b' },
      },
      yAxis: hasIndex
        ? [
            {
              type: 'value',
              name: '均分',
              nameTextStyle: { fontSize: 10, color: '#64748b' },
              axisLabel: { fontSize: 10, color: '#64748b', formatter: (v: number) => v.toFixed(3) },
              splitLine: { lineStyle: { color: '#f1f5f9' } },
            },
            {
              type: 'value',
              name: '指数(首日=100)',
              nameTextStyle: { fontSize: 10, color: '#64748b' },
              position: 'right',
              scale: true,
              axisLabel: { fontSize: 10, color: '#64748b', formatter: (v: number) => v.toFixed(1) },
              splitLine: { show: false },
            },
          ]
        : [
            {
              type: 'value',
              axisLabel: { fontSize: 10, color: '#64748b', formatter: (v: number) => v.toFixed(3) },
              splitLine: { lineStyle: { color: '#f1f5f9' } },
            },
          ],
      series: [
        {
          name: '均分',
          type: 'line',
          yAxisIndex: 0,
          data: vals,
          smooth: true,
          symbol: 'circle',
          symbolSize: 3,
          lineStyle: { width: 2, color: '#3b82f6' },
          itemStyle: { color: '#3b82f6' },
          areaStyle: { color: 'rgba(59,130,246,0.08)' },
        },
        ...indexLines,
      ],
    };
  }, [series, selectedSeries, availableIndices]);

  if (loading) return <div className="flex justify-center py-12"><Spin /></div>;
  if (!series.length) return <Empty description="暂无推理均分时序" />;

  return (
    <div className="space-y-4">
      <Card size="small" className="rounded-2xl">
        <Collapse
          ghost
          items={[{
            key: 'explain',
            label: <span className="text-xs font-black text-slate-700 flex items-center gap-1.5"><Info size={12} className="text-blue-500"/>口径说明</span>,
            children: <div className="text-xs text-slate-600 leading-relaxed space-y-1">
              <div>均分 = 当日全市场 `pred / fusion_score` 均值（与个股终端一致，回退读 `pred.parquet`）。</div>
              <div>均分日期 = **信号日 T**（用 T 日及之前数据算出）；指数默认取 **T+1（次日可交易）** 对齐，可切换 T+N / 不错开。</div>
              <div>指数叠加用于**走势对比**：按窗口首个有效日归一为 100，与均分（左轴）分轴显示，不改变均分口径。</div>
              <div>可多选沪深300 / 中证500 / 中证1000 / 上证综指 / 深证成指；「全部叠加」一次画全。</div>
            </div>,
          }]}
        />
      </Card>

      <div className="grid grid-cols-2 gap-3">
        <Card size="small" className="rounded-2xl text-center">
          <div className="text-[11px] text-slate-400 font-bold">最新均分</div>
          <div className="text-lg font-mono font-black text-blue-600">{Number(current?.avg_score ?? 0).toFixed(4)}</div>
          <div className="text-[11px] text-slate-500 mt-1">{current?.trade_date} · 全市场 {current?.count} 只</div>
        </Card>
        <Card size="small" className="rounded-2xl text-center">
          <div className="text-[11px] text-slate-400 font-bold">样本</div>
          <div className="text-lg font-mono font-black text-slate-800">{series.length} 日</div>
          <div className="text-[11px] text-slate-400">近 {series[0]?.trade_date} ~ {series[series.length-1]?.trade_date}</div>
        </Card>
      </div>

      <Card className="rounded-2xl" size="small">
        <div className="flex flex-wrap items-center gap-2 mb-2">
          <TrendingUp size={14} className="text-blue-500" />
          <Text className="text-xs font-black text-slate-700">均分时序 × 大盘指数</Text>
          <Select
            mode="multiple"
            size="small"
            allowClear
            value={selectedIndices}
            onChange={(v) => setSelectedIndices(v as string[])}
            options={indexOptions.map((o) => ({ value: o.code, label: o.name }))}
            placeholder="选择对比指数"
            maxTagCount="responsive"
            style={{ minWidth: 240, maxWidth: 420 }}
            className="ml-2"
          />
          <Select
            size="small"
            value={indexOffset}
            onChange={(v) => setIndexOffset(Number(v))}
            options={offsetOptions}
            style={{ width: 190 }}
            title="均分日期=信号日 T；指数默认取 T+1（次日可交易）"
          />
          <Button
            size="small"
            onClick={() => setSelectedIndices(indexOptions.map((o) => o.code))}
          >
            全部叠加
          </Button>
          <Button size="small" onClick={() => setSelectedIndices([])}>
            仅均分
          </Button>
          {selectedSeries.length > 0 && (
            <span className="ml-auto text-[10px] text-slate-400">
              指数按窗口首日=100 归一 · 右轴 · 信号日 T → 指数 T+{indexOffset}
            </span>
          )}
          {data?.indices_error && (
            <Tag color="warning" className="text-[10px]">
              指数读取失败：{String(data.indices_error).slice(0, 80)}
            </Tag>
          )}
        </div>
        {option && <ReactECharts option={option} style={{ height: 340 }} notMerge />}
      </Card>
    </div>
  );
};

export default MarketRegimePanel;
