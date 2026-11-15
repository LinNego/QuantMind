#!/usr/bin/env python3
"""生成学习路线脑图（横向树状图）。

不依赖 graphviz，纯 matplotlib 画。
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

FONT = Path("/root/quant/QuantMind/docker/training/fonts/WQYZenHei.ttf")
font_manager.fontManager.addfont(str(FONT))
CN = font_manager.FontProperties(fname=str(FONT)).get_name()
plt.rcParams["font.family"] = CN
plt.rcParams["axes.unicode_minus"] = False

OUT = Path("/root/quant/QuantMind/docs/images/learning/00_mindmap.png")

# (id, parent, 文本, 颜色, 深度)  深度 0=根 1=阶段 2=模块 3=产出
NODES = [
    ("root", None, "AI Agent × 量化\n16 周", "#2B2B2B", 0),

    # ── 阶段一 ──────────────────────────────────────────
    ("s1", "root", "① 地基\n第 1–3 周", "#4C78A8", 1),
    ("s1a", "s1", "统计直觉\n特征 / 标签 / 训练", "#4C78A8", 2),
    ("s1b", "s1", "验证观\n样本内 vs 样本外", "#4C78A8", 2),
    ("s1c", "s1", "数据体检\n列类型 / 缺失 / 常数", "#4C78A8", 2),
    ("s1d", "s1", "产出：读懂 30 行示例\n说出「过拟合」是什么", "#7FA6C9", 3),

    # ── 阶段二 ──────────────────────────────────────────
    ("s2", "root", "② Agent 工程\n第 4–8 周", "#54A24B", 1),
    ("s2a", "s2", "工具设计\n32 个 SKILL.md 当教材", "#54A24B", 2),
    ("s2b", "s2", "上下文工程\n什么进上下文 / 何时压缩", "#54A24B", 2),
    ("s2c", "s2", "状态与恢复\n断点续跑 / 超时 / 降级", "#54A24B", 2),
    ("s2d", "s2", "产出：3 个 PR\n一次失败恢复的案例", "#8CC084", 3),

    # ── 阶段三 ──────────────────────────────────────────
    ("s3", "root", "③ 评测体系\n第 6–12 周", "#F58518", 1),
    ("s3a", "s3", "因子评测\nIC / ICIR / 衰减", "#F58518", 2),
    ("s3b", "s3", "防泄漏\n时序切分 / 安慰剂", "#F58518", 2),
    ("s3c", "s3", "Agent 评测\n30 条 eval + 基线", "#F58518", 2),
    ("s3d", "s3", "产出：一个评测引擎\n一组可复现的数字", "#F8B06B", 3),

    # ── 阶段四 ──────────────────────────────────────────
    ("s4", "root", "④ 融合项目\n第 9–14 周", "#B279A2", 1),
    ("s4a", "s4", "特征一致性\n训练 / 推理 / 回测", "#B279A2", 2),
    ("s4b", "s4", "因子质检\n去重 / 相关 / 样本外", "#B279A2", 2),
    ("s4c", "s4", "证据链\n每个结论可溯源", "#B279A2", 2),
    ("s4d", "s4", "产出：一条可跑通的链路\n一张溯源 demo", "#CBA3C0", 3),

    # ── 阶段五 ──────────────────────────────────────────
    ("s5", "root", "⑤ 作品与求职\n第 13–16 周", "#E45756", 1),
    ("s5a", "s5", "Case study\n中英各一篇", "#E45756", 2),
    ("s5b", "s5", "开源化\n可 clone 能跑", "#E45756", 2),
    ("s5c", "s5", "简历三条线\n每条带数字", "#E45756", 2),
    ("s5d", "s5", "产出：作品集 + 面试\n目标 2 个 offer", "#EE8B8A", 3),

    # ── 终点 ────────────────────────────────────────────
    ("goal", "root", "目标岗位\n垂直 Agent / 量化平台", "#1B5E20", 1),
]


def main() -> None:
    depth_x = {0: 0.0, 1: 3.4, 2: 6.8, 3: 10.2}
    children: dict[str, list[str]] = {}
    by_id = {}
    for nid, parent, label, color, depth in NODES:
        by_id[nid] = (parent, label, color, depth)
        if parent:
            children.setdefault(parent, []).append(nid)

    # 叶子按出现顺序排 y，父节点取子节点 y 的均值
    pos: dict[str, float] = {}
    counter = [0.0]

    def place(nid: str) -> float:
        kids = children.get(nid, [])
        if not kids:
            counter[0] += 1.0
            pos[nid] = -counter[0]
            return pos[nid]
        ys = [place(k) for k in kids]
        pos[nid] = sum(ys) / len(ys)
        return pos[nid]

    place("root")

    fig, ax = plt.subplots(figsize=(17, 11))
    ax.set_xlim(-1.2, 13.5)
    ax.set_ylim(min(pos.values()) - 1.2, max(pos.values()) + 1.2)
    ax.axis("off")

    for nid, (parent, label, color, depth) in by_id.items():
        if parent is None:
            continue
        x0, y0 = depth_x[by_id[parent][3]], pos[parent]
        x1, y1 = depth_x[depth], pos[nid]
        ax.annotate(
            "",
            xy=(x1 - 0.05, y1),
            xytext=(x0 + 0.05, y0),
            arrowprops=dict(
                arrowstyle="-",
                color=color,
                lw=2.4 if depth <= 1 else 1.4,
                alpha=0.85 if depth <= 1 else 0.55,
                connectionstyle="arc3,rad=0.18",
            ),
        )

    for nid, (parent, label, color, depth) in by_id.items():
        x, y = depth_x[depth], pos[nid]
        size = {0: 15, 1: 11.5, 2: 9.5, 3: 8.8}[depth]
        weight = "bold" if depth <= 1 else "normal"
        face = color if depth <= 1 else "white"
        textcolor = "white" if depth <= 1 else "#333333"
        pad = {0: 0.55, 1: 0.34, 2: 0.26, 3: 0.22}[depth]
        ax.text(
            x, y, label,
            fontsize=size, fontweight=weight, ha="left", va="center",
            color=textcolor, linespacing=1.45, zorder=3,
            bbox=dict(
                boxstyle=f"round,pad={pad}",
                facecolor=face,
                edgecolor=color,
                linewidth=1.6,
                alpha=0.95,
            ),
        )

    ax.text(
        5.2, max(pos.values()) + 0.95,
        "从后端到「AI Agent × 量化」：16 周路线图",
        fontsize=17, fontweight="bold", ha="center", color="#1B1B1B",
    )

    fig.tight_layout()
    fig.savefig(OUT, dpi=115, bbox_inches="tight", facecolor="white")
    print(f"已写入 {OUT}")


if __name__ == "__main__":
    main()
