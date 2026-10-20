#!/usr/bin/env bash
# alpha_library 夜间刷新（Alpha101 + GTJA191 + Alpha158 → 6_ml_datasets/alpha_library）
#
# 为什么有这个脚本：alpha_library_factors.py 一直是手动批处理，从未进 cron →
# 分区滞后日K 5 天（2026-09-09 实测 alpha dt=20260903 vs kline dt=20260908），
# alert.sh 2c 每 30 分钟报一次。
#
# 增量模式（2026-10-20 起默认）：alpha 已有分区时只补「最新分区之后」的缺口，
# 预热窗精确到日（--start-date = 首个缺口日 -500 天 ≈ 330 个交易日 > 最长 250 日
# 回看）。实测 674 日窗峰值 RSS 6.7GB、432 日窗全程 457s；330 日窗按因子字典
# 规模外推约 4GB、5-8 分钟（50 只 × 332 日全链路冒烟峰值 1.7GB，其中含全量
# daily_unadjusted 读取这一常量项）。内存闸门随之降到 8GB，凌晨 10-12GB 可用
# 内存即可自愈。只有 alpha 一个分区都没有时才走全量（2026-09-09 实测 ~80 分钟、
# 峰值 RSS ~24GB）。
#
# ⚠️ 分区属主：合并阶段按日期顺序重写最终分区，遇到非本用户所有的目录会
# PermissionError 中止（2026-09-09 实录：dt=20260831..20260903 四个分区归 root，
# 合并跑到 20260831 即失败，之后的 20260904/07/08 再没写入——白跑 80 分钟）。
# 故下面加了写权限闸门；修法：sudo chown -R zbox:zbox <alpha_library>。
#
# 闸门：
#   1) flock 防重入（上一次没跑完就跳过）；
#   2) 已最新则空转（cron 早于 quantdb 夜间同步时不会白跑 80 分钟）；
#   3) 最终分区存在非本用户可写的目录 → 跳过（合并必失败，别白跑）；
#   4) 可用内存 < MIN_MEM_GB 跳过——增量峰值 ~4GB / 全量 ~24GB，本机 swap 已用
#      15/15，不能和盘中/夜间任务抢内存（宁可滞后一天，也不要 OOM 拖垮实盘）。
#
# cron（JST = 北京+1h）：30 2 * * 2-6  ← 北京 01:30，接在 quantdb 夜间同步
# （实测 dt 分区 01:55 JST 落盘）之后。
set -uo pipefail

QM_ROOT=/home/zbox/projects/quantmind
PY=/usr/bin/python3
SCRIPT="$QM_ROOT/backend/scripts/alpha_library_factors.py"
ALPHA_ROOT="$QM_ROOT/data/quantdb/6_ml_datasets/alpha_library"
KLINE_ROOT="$QM_ROOT/data/quantdb/1_kline_data/daily_forward"
LOG="$ALPHA_ROOT/cron_run.log"
LOCK=/tmp/.qm_alpha_refresh.lock

ts() { date '+%F %T'; }
log_line() { echo "$(ts) $*" >> "$LOG"; }

mkdir -p "$ALPHA_ROOT"
touch "$LOG" 2>/dev/null

# 1) 防重入
exec 9>"$LOCK"
if ! flock -n 9; then
    log_line "跳过：上一次刷新仍在运行"
    exit 0
fi

# 1b) 清理上次崩溃残留的 partial（成功路径会 rmtree）：write_partitions 见到同名
#     日期会跳过（沿用旧预热窗结果），merge 只按日期过滤，不会自愈。
PARTIAL_DIR="$(dirname "$ALPHA_ROOT")"
rm -rf "$PARTIAL_DIR/_partial_a158" "$PARTIAL_DIR/_partial_a101" "$PARTIAL_DIR/_partial_gtja"

# 2) 已最新则空转（比较 8 位日期串，同为整数比较）
#    只认带 data.parquet 的分区：写入方先建目录后写文件，SIGKILL 会留下空目录；
#    把空目录当基线会让 start-dt 越过该日，永久留洞。
newest_dt() {
    find "$1" -maxdepth 2 -name data.parquet -path '*/dt=*' 2>/dev/null \
        | grep -oE 'dt=[0-9]{8}' | grep -oE '[0-9]{8}' | sort | tail -1
}
KL_DT=$(newest_dt "$KLINE_ROOT")
AL_DT=$(newest_dt "$ALPHA_ROOT")
if [ -z "$KL_DT" ]; then
    log_line "跳过：日K 分区目录为空（$KLINE_ROOT）"
    exit 0
fi
if [ -n "$AL_DT" ] && [ "$AL_DT" -ge "$KL_DT" ]; then
    log_line "跳过：已最新（alpha=$AL_DT kline=$KL_DT）"
    exit 0
fi

# 2b) 模式选择：有基线 → 尾部增量（预热窗 500 天，覆盖最长 250 日回看）；无基线 → 全量。
START_DT=""
WARM_START=""
RUN_ARGS=()
if [ -n "$AL_DT" ]; then
    START_DT=$(find "$KLINE_ROOT" -maxdepth 2 -name data.parquet -path '*/dt=*' 2>/dev/null \
        | grep -oE 'dt=[0-9]{8}' | grep -oE '[0-9]{8}' | awk -v a="$AL_DT" '$1 > a' | sort | head -1)
fi
if [ -n "$START_DT" ]; then
    WARM_START=$(date -d "${START_DT:0:4}-${START_DT:4:2}-${START_DT:6:2} -500 days" +%Y%m%d)
    MODE=incremental
    MIN_MEM_GB=${INCR_MIN_MEM_GB:-8}
    RUN_ARGS=(--start-date "$WARM_START" --start-dt "$START_DT")
else
    MODE=full
    MIN_MEM_GB=${MIN_MEM_GB:-30}
fi

# 3) 写权限闸门：合并阶段要重写最终分区（**dt=* 目录**），非本用户可写的 dt=* 会
#    PermissionError 中止（白跑一整轮）。只检查 dt=* —— report/ 等旁路目录由其他
#    工具维护，合并不写它们（2026-09-18 实录：report/ 属 root 触发本闸门连续
#    4 夜误跳（09-15~09-18），而合并根本不碰 report/；正确修法是把检查面收窄到
#    合并真正重写的 dt=*，同时 chown 掉旁路目录保持树整洁）。
NOWRITABLE=$(find "$ALPHA_ROOT" -maxdepth 1 -type d -name 'dt=*' ! -writable 2>/dev/null | head -3)
if [ -n "$NOWRITABLE" ]; then
    log_line "跳过：最终分区有 $(find "$ALPHA_ROOT" -maxdepth 1 -type d -name 'dt=*' ! -writable 2>/dev/null | wc -l) 个 dt=* 目录不可写（合并会 PermissionError）→ sudo chown -R $(id -un):$(id -gn) $ALPHA_ROOT；示例：$(echo "$NOWRITABLE" | tr '\n' ' ')"
    exit 0
fi

# 4) 内存闸门：不足先**有界等待**，超时仍不足才跳过。
#    2026-10-13 事故：02:30 那一刻可用 14GB < 30GB → 直接 exit 0，alpha 停在
#    dt=20261009 而日K 已 20261012；下游 CUSTOM 重建 fail-closed 拒写 → 模型撞
#    维度门禁 → 当日无批量信号。整条链的**唯一可动点**就是这里。此前凌晨常有
#    其它任务（备份/训练残留）短暂吃内存，等一个窗口往往就够——直接放弃太早。
MEM_WAIT_SECONDS=${MEM_WAIT_SECONDS:-1800}
MEM_WAIT_SLEEP=120
waited=0
while :; do
    AVAIL_GB=$(awk '/^MemAvailable:/ {print int($2/1024/1024)}' /proc/meminfo)
    if [ -n "$AVAIL_GB" ] && [ "$AVAIL_GB" -ge "$MIN_MEM_GB" ]; then
        break
    fi
    if [ "$waited" -ge "$MEM_WAIT_SECONDS" ]; then
        log_line "跳过：可用内存 ${AVAIL_GB:-?}GB < ${MIN_MEM_GB}GB，已等待 ${waited}s（alpha=$AL_DT kline=$KL_DT）→ 由 alpha_refresh_retry 择机重试"
        exit 0
    fi
    [ "$waited" -eq 0 ] && log_line "内存不足（可用 ${AVAIL_GB:-?}GB < ${MIN_MEM_GB}GB），等待内存释放（上限 ${MEM_WAIT_SECONDS}s）…"
    sleep "$MEM_WAIT_SLEEP"
    waited=$((waited + MEM_WAIT_SLEEP))
done

log_line "开始刷新（mode=$MODE alpha=${AL_DT:-无} kline=$KL_DT start-dt=${START_DT:-无} warm-start=${WARM_START:-无} 可用内存=${AVAIL_GB}GB 等待=${waited}s）"
nice -n 10 "$PY" "$SCRIPT" "${RUN_ARGS[@]}" >> "$LOG" 2>&1
rc=$?
AL_NEW=$(newest_dt "$ALPHA_ROOT")
log_line "结束 rc=$rc（alpha=${AL_NEW:-无}）"
exit "$rc"
