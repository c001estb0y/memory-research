"""彩色冰块配对游戏模拟器

规则概要:
- 箱子: 6种颜色 × 100颗 = 600颗冰块
- 玩家选定1种许愿色, 全程不放回抽取
- 初始抽取24块 → 按许愿色数量迭代追加 → 配对收纳/补冰交替循环
- 最终总和 = 收纳配对冰块总数 + 桌面剩余冰块数
"""

import numpy as np
from typing import Optional


class IceBlockGame:

    def __init__(
        self,
        num_colors: int = 6,
        blocks_per_color: int = 100,
        initial_draw: int = 24,
        seed: Optional[int] = None,
    ):
        self.num_colors = num_colors
        self.blocks_per_color = blocks_per_color
        self.total_blocks = num_colors * blocks_per_color
        self.initial_draw = initial_draw
        self.rng = np.random.default_rng(seed)

    def _draw(self, box: np.ndarray, n: int) -> np.ndarray:
        """从箱子中不放回抽取 n 块冰块, 就地修改 box, 返回各色抽取数量."""
        available = int(np.sum(box))
        n = min(n, available)
        if n == 0:
            return np.zeros(self.num_colors, dtype=int)
        drawn = self.rng.multivariate_hypergeometric(box, n)
        box -= drawn
        return drawn

    def _remove_pairs(self, table: np.ndarray) -> int:
        """移除桌面所有同色对子, 就地修改 table, 返回对子总数."""
        pairs_per_color = table // 2
        total_pairs = int(np.sum(pairs_per_color))
        table -= pairs_per_color * 2
        return total_pairs

    def _do_iteration_draws(
        self, box: np.ndarray, table: np.ndarray,
        wish_color: int, initial_wish_count: int,
    ) -> None:
        """迭代抽取: 按许愿色数量不断追加抽取, 直至本次抽取无许愿色或箱空."""
        wish_count = initial_wish_count
        while wish_count > 0 and int(np.sum(box)) > 0:
            drawn = self._draw(box, wish_count)
            table += drawn
            wish_count = int(drawn[wish_color])

    def simulate_once(self) -> dict:
        """执行一次完整游戏, 返回结果字典."""
        box = np.full(self.num_colors, self.blocks_per_color, dtype=int)
        table = np.zeros(self.num_colors, dtype=int)
        wish_color = 0  # 由对称性, 选哪种颜色不影响最终分布
        total_collected = 0

        # ── 第一阶段: 初始抽取 + 迭代追加 ──
        drawn = self._draw(box, self.initial_draw)
        table += drawn
        self._do_iteration_draws(box, table, wish_color, int(drawn[wish_color]))

        # ── 第二阶段: 配对补冰交替循环 ──
        while True:
            pairs = self._remove_pairs(table)
            if pairs == 0:
                break

            total_collected += pairs * 2

            if int(np.sum(box)) == 0:
                break

            drawn = self._draw(box, pairs)
            table += drawn
            self._do_iteration_draws(box, table, wish_color, int(drawn[wish_color]))

        remaining = int(np.sum(table))
        return {
            "final_sum": total_collected + remaining,
            "total_collected": total_collected,
            "remaining_on_table": remaining,
            "remaining_in_box": int(np.sum(box)),
        }

    def run_simulations(self, n: int) -> np.ndarray:
        """运行 n 次模拟, 返回每次的最终总和."""
        results = np.empty(n, dtype=int)
        for i in range(n):
            results[i] = self.simulate_once()["final_sum"]
        return results


# ────────────────────── 分析与报告 ──────────────────────


def analyze_results(results: np.ndarray) -> dict:
    """统计分析模拟结果."""
    stats = {
        "count": len(results),
        "mean": float(np.mean(results)),
        "median": float(np.median(results)),
        "std": float(np.std(results)),
        "min": int(np.min(results)),
        "max": int(np.max(results)),
        "percentiles": {
            "5%": float(np.percentile(results, 5)),
            "25%": float(np.percentile(results, 25)),
            "50%": float(np.percentile(results, 50)),
            "75%": float(np.percentile(results, 75)),
            "95%": float(np.percentile(results, 95)),
        },
    }

    # 逐值频率
    values, counts = np.unique(results, return_counts=True)
    stats["value_distribution"] = list(
        zip(values.tolist(), (counts / len(results)).tolist())
    )

    # 区间概率
    min_val, max_val = int(np.min(results)), int(np.max(results))
    range_size = max_val - min_val
    step = 2 if range_size <= 50 else (5 if range_size <= 200 else 10)
    start = (min_val // step) * step
    end = ((max_val // step) + 1) * step + step

    intervals = []
    for lo in range(start, end, step):
        hi = lo + step
        prob = float(np.sum((results >= lo) & (results < hi))) / len(results)
        if prob > 0:
            intervals.append((lo, hi, prob))
    stats["intervals"] = intervals

    return stats


def print_report(stats: dict):
    """打印分析报告."""
    print("=" * 62)
    print("        彩色冰块配对游戏 · 蒙特卡洛模拟分析报告")
    print("=" * 62)
    print(f"\n模拟次数: {stats['count']:,}")
    print(f"\n{'─'*20} 基本统计 {'─'*20}")
    print(f"  均值 (Mean):   {stats['mean']:.2f}")
    print(f"  中位数 (P50):  {stats['median']:.1f}")
    print(f"  标准差 (Std):  {stats['std']:.2f}")
    print(f"  最小值 (Min):  {stats['min']}")
    print(f"  最大值 (Max):  {stats['max']}")

    print(f"\n{'─'*20} 分位数 {'─'*22}")
    for k, v in stats["percentiles"].items():
        print(f"  {k:>5}: {v:.1f}")

    print(f"\n{'─'*20} 区间概率 {'─'*20}")
    print(f"  {'区间':<15}{'概率':>10}{'累计':>10}  分布")
    print(f"  {'─'*50}")
    cumulative = 0.0
    for lo, hi, prob in stats["intervals"]:
        cumulative += prob
        bar = "█" * int(prob * 200)
        print(f"  [{lo:>3}, {hi:>3})  {prob:>9.4f} {cumulative:>9.4f}  {bar}")

    print(f"\n{'─'*20} 精确值概率 (>0.5%) {'─'*10}")
    print(f"  {'最终总和':<12}{'概率':>10}")
    print(f"  {'─'*22}")
    for val, prob in stats["value_distribution"]:
        if prob > 0.005:
            print(f"  {val:<12}{prob:>9.4f}")


if __name__ == "__main__":
    import time

    NUM_SIMULATIONS = 500_000

    print(f"正在运行 {NUM_SIMULATIONS:,} 次模拟 …")
    t0 = time.time()

    game = IceBlockGame(seed=42)
    results = game.run_simulations(NUM_SIMULATIONS)

    elapsed = time.time() - t0
    print(f"完成, 用时 {elapsed:.1f} 秒\n")

    stats = analyze_results(results)
    print_report(stats)
