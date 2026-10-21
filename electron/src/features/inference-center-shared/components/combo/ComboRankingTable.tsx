/**
 * 组合选股 · 当日榜单。
 *
 * 合规边界：表头与单元格**只出现位置词**（靠前/居中/靠后），不出现任何方向性字眼。
 * 分数颜色只按中性色显示——「较上日」的红涨绿跌不在这里用，因为榜单没有可比的上日分数。
 */

import React, { useMemo } from 'react';
import { Alert, Button, Table, Tag, Tooltip } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Download, RefreshCw } from 'lucide-react';
import type {
  BlendRankingMember,
  BlendRankingResponse,
  BlendRankingRow,
} from '../../../../services/blendTypes';
import { formatResearchScore } from '../../../shared/researchScore';
import { positionTagClass } from '../../positionLabels';
import {
  buildRankingCsv,
  coverageLabel,
  dispersionLabel,
  historyLabel,
  positionLabel,
  rankingCsvFilename,
  scoreLabel,
} from './comboRankingModel';

const POSITION_HINT =
  '位置仅描述该标的在本组合当日综合分排序中的相对位置，不构成任何交易建议。';

export interface ComboRankingTableProps {
  ranking: BlendRankingResponse | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  blendName: string;
  /** 目标持有日（历史标注的分母）；未知时不显示分母 */
  holdDays?: number | null;
  /** 未保存模板：没有纸面台账历史，历史列如实显示 — */
  isTemplate?: boolean;
}

function medianScore(rows: readonly BlendRankingRow[]): number | null {
  const values = rows
    .map((row) => row.research_score)
    .filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
    .sort((a, b) => a - b);
  if (values.length === 0) return null;
  const mid = Math.floor(values.length / 2);
  return values.length % 2 === 0 ? (values[mid - 1] + values[mid]) / 2 : values[mid];
}

function downloadCsv(filename: string, content: string): void {
  const blob = new Blob([content], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function ContributionTable({
  row,
  members,
}: {
  row: BlendRankingRow;
  members: BlendRankingMember[];
}) {
  const nameOf = (modelId: string) =>
    members.find((m) => m.model_id === modelId)?.display_name || modelId;
  const entries = Object.entries(row.contributions ?? {}).sort((a, b) => b[1] - a[1]);
  return (
    <div className="px-6 py-2 space-y-2" data-testid="combo-contributions">
      <div className="flex items-center gap-3 text-[11px] text-slate-500">
        <span className="font-bold text-slate-600">成员贡献（分位口径）</span>
        <span>
          覆盖 {row.coverage}/{members.length}
        </span>
        <span>分歧 {dispersionLabel(row.dispersion_band)}</span>
      </div>
      <table className="text-[11px]">
        <thead>
          <tr className="text-slate-400">
            <th className="pr-6 text-left font-semibold">成员</th>
            <th className="text-right font-semibold">贡献</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([modelId, value]) => (
            <tr key={modelId}>
              <td className="pr-6 text-slate-700">{nameOf(modelId)}</td>
              <td className="text-right font-mono text-slate-600">{value.toFixed(3)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {row.missing_members?.length ? (
        <div className="flex items-center gap-1 flex-wrap">
          <span className="text-[11px] text-slate-400">缺失成员：</span>
          {row.missing_members.map((id) => (
            <Tag key={id} className="text-[10px]">
              {nameOf(id)}
            </Tag>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export const ComboRankingTable: React.FC<ComboRankingTableProps> = ({
  ranking,
  loading,
  error,
  onRetry,
  blendName,
  holdDays,
  isTemplate = false,
}) => {
  const rows = ranking?.ranking ?? [];
  const members = ranking?.members ?? [];
  const memberCount = members.length;
  const availableCount = members.filter((m) => m.available).length;
  const median = useMemo(() => medianScore(rows), [rows]);
  const dominantBand = useMemo(() => {
    const counts = ranking?.stats?.dispersion;
    if (!counts) return null;
    const entries = Object.entries(counts).filter(([band]) => band !== 'unknown');
    if (entries.length === 0) return null;
    return entries.sort((a, b) => Number(b[1]) - Number(a[1]))[0][0];
  }, [ranking?.stats?.dispersion]);

  const columns: ColumnsType<BlendRankingRow> = [
    {
      title: '排名',
      dataIndex: 'rank',
      width: 64,
      align: 'right',
      render: (v: number) => <span className="font-mono text-[11px] font-bold text-slate-500">{v}</span>,
    },
    {
      title: '代码',
      dataIndex: 'code',
      width: 104,
      render: (v: string, row) => (
        <span className="font-mono text-[11px] font-bold text-slate-800">{v || row.symbol}</span>
      ),
    },
    {
      title: '名称',
      dataIndex: 'name',
      width: 120,
      ellipsis: true,
      render: (v: string) => <span className="text-xs text-slate-700">{v || '—'}</span>,
    },
    {
      title: '综合分',
      dataIndex: 'research_score',
      width: 88,
      align: 'right',
      sorter: (a, b) => (a.research_score ?? -1) - (b.research_score ?? -1),
      render: (_v, row) => (
        <span className="font-mono text-xs font-black text-blue-700">{scoreLabel(row)}</span>
      ),
    },
    {
      title: (
        <Tooltip title={POSITION_HINT}>
          <span className="cursor-help">位置</span>
        </Tooltip>
      ),
      dataIndex: 'position',
      width: 80,
      align: 'center',
      render: (_v, row) => {
        const label = positionLabel(row);
        return (
          <span className={`inline-block rounded border px-1.5 py-0.5 text-[10px] font-bold ${positionTagClass(label)}`}>
            {label}
          </span>
        );
      },
    },
    {
      title: '覆盖',
      dataIndex: 'coverage',
      width: 72,
      align: 'center',
      render: (v: number) => (
        <span className="font-mono text-[11px] text-slate-600">{coverageLabel(v, memberCount)}</span>
      ),
    },
    {
      title: '分歧',
      dataIndex: 'dispersion_band',
      width: 64,
      align: 'center',
      render: (v: string) => <span className="text-[11px] text-slate-600">{dispersionLabel(v)}</span>,
    },
    {
      title: '历史标注',
      dataIndex: 'history',
      width: 220,
      render: (_v, row) => (
        <span className="text-[11px] text-slate-600">{historyLabel(row.history, holdDays)}</span>
      ),
    },
    {
      title: '行业',
      dataIndex: 'industry',
      width: 88,
      ellipsis: true,
      render: (v: string | null | undefined) => (
        <span className="text-[11px] text-slate-600">{v || '—'}</span>
      ),
    },
    {
      title: '市值(亿)',
      dataIndex: 'market_cap_yi',
      width: 84,
      align: 'right',
      render: (v: number | null | undefined) => (
        <span className="font-mono text-[11px] text-slate-600">
          {typeof v === 'number' && Number.isFinite(v) ? v.toFixed(1) : '—'}
        </span>
      ),
    },
  ];

  return (
    <div className="h-full min-h-0 min-w-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden">
      <div className="shrink-0 flex items-center gap-3 px-3 py-2 border-b border-slate-100">
        <div className="flex items-center gap-2 text-[11px] text-slate-500">
          <span>
            覆盖{' '}
            <strong className="font-mono text-slate-800">
              {availableCount}/{memberCount || '—'}
            </strong>
          </span>
          <span>
            分歧 <strong className="text-slate-800">{dispersionLabel(dominantBand)}</strong>
          </span>
          <span>
            评分中位{' '}
            <strong className="font-mono text-slate-800">{formatResearchScore(median)}</strong>
          </span>
          {ranking?.data_trade_date && (
            <span className="font-mono text-slate-400">数据日 {ranking.data_trade_date}</span>
          )}
          {isTemplate && <Tag color="blue">模板预览 · 保存后启用历史标注</Tag>}
        </div>
        <div className="ml-auto flex items-center gap-2" data-testid="combo-ranking-actions">
          <Button
            size="small"
            icon={<Download size={13} />}
            disabled={!ranking || rows.length === 0}
            onClick={() =>
              ranking &&
              downloadCsv(
                rankingCsvFilename(blendName, ranking.data_trade_date),
                buildRankingCsv(rows, memberCount),
              )
            }
          >
            导出名单
          </Button>
          <Button size="small" icon={<RefreshCw size={13} />} onClick={onRetry} loading={loading}>
            刷新
          </Button>
        </div>
      </div>

      {error ? (
        <div className="p-3" data-testid="combo-ranking-error">
          <Alert
            type="error"
            showIcon
            message={error}
            action={
              <Button size="small" onClick={onRetry}>
                重试
              </Button>
            }
          />
        </div>
      ) : null}

      <div className="flex-1 min-h-0" data-testid="combo-ranking-table">
        <Table<BlendRankingRow>
          rowKey={(row) => row.symbol || row.code}
          size="small"
          loading={loading}
          dataSource={rows}
          columns={columns}
          pagination={{ defaultPageSize: 50, pageSizeOptions: [50, 100, 200], showSizeChanger: true }}
          scroll={{ x: 980, y: 'calc(100vh - 300px)' }}
          expandable={{
            expandedRowRender: (row) => <ContributionTable row={row} members={members} />,
            rowExpandable: () => true,
          }}
          locale={{ emptyText: loading ? ' ' : error ? ' ' : '该组合当日没有可展示的标的' }}
        />
      </div>
    </div>
  );
};

export default ComboRankingTable;
