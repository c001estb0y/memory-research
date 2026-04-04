"""彩色冰块配对游戏 · 单元测试"""

import numpy as np
import pytest
from simulation import IceBlockGame, analyze_results


# ═══════════════════════════════════════════════════════════
#  抽取机制
# ═══════════════════════════════════════════════════════════

class TestDraw:

    def test_correct_count(self):
        game = IceBlockGame(seed=42)
        box = np.full(6, 100, dtype=int)
        drawn = game._draw(box, 24)
        assert np.sum(drawn) == 24

    def test_modifies_box(self):
        game = IceBlockGame(seed=42)
        box = np.full(6, 100, dtype=int)
        box_before = box.copy()
        drawn = game._draw(box, 24)
        np.testing.assert_array_equal(box + drawn, box_before)
        assert np.sum(box) == 576

    def test_non_negative(self):
        game = IceBlockGame(seed=42)
        box = np.full(6, 100, dtype=int)
        drawn = game._draw(box, 50)
        assert np.all(drawn >= 0)
        assert np.all(box >= 0)

    def test_draw_zero(self):
        game = IceBlockGame(seed=42)
        box = np.full(6, 100, dtype=int)
        drawn = game._draw(box, 0)
        assert np.sum(drawn) == 0
        assert np.sum(box) == 600

    def test_empty_box(self):
        game = IceBlockGame(seed=42)
        box = np.zeros(6, dtype=int)
        drawn = game._draw(box, 10)
        assert np.sum(drawn) == 0

    def test_draw_more_than_available(self):
        game = IceBlockGame(seed=42)
        box = np.array([3, 2, 1, 0, 0, 0], dtype=int)
        drawn = game._draw(box, 20)
        assert np.sum(drawn) == 6
        assert np.sum(box) == 0

    def test_single_color_box(self):
        game = IceBlockGame(num_colors=6, seed=42)
        box = np.array([5, 0, 0, 0, 0, 0], dtype=int)
        drawn = game._draw(box, 3)
        assert drawn[0] == 3
        assert np.sum(drawn) == 3


# ═══════════════════════════════════════════════════════════
#  配对机制
# ═══════════════════════════════════════════════════════════

class TestPairing:

    def test_mixed(self):
        game = IceBlockGame()
        table = np.array([4, 3, 2, 1, 0, 5], dtype=int)
        pairs = game._remove_pairs(table)
        assert pairs == 6  # 2+1+1+0+0+2
        np.testing.assert_array_equal(table, [0, 1, 0, 1, 0, 1])

    def test_all_even(self):
        game = IceBlockGame()
        table = np.array([4, 2, 6, 0, 2, 8], dtype=int)
        pairs = game._remove_pairs(table)
        assert pairs == 11
        np.testing.assert_array_equal(table, np.zeros(6, dtype=int))

    def test_all_odd(self):
        game = IceBlockGame()
        table = np.array([1, 3, 5, 7, 1, 1], dtype=int)
        pairs = game._remove_pairs(table)
        assert pairs == 6  # 0+1+2+3+0+0
        np.testing.assert_array_equal(table, [1, 1, 1, 1, 1, 1])

    def test_no_pairs(self):
        game = IceBlockGame()
        table = np.array([1, 0, 1, 0, 1, 0], dtype=int)
        pairs = game._remove_pairs(table)
        assert pairs == 0
        np.testing.assert_array_equal(table, [1, 0, 1, 0, 1, 0])

    def test_empty_table(self):
        game = IceBlockGame()
        table = np.zeros(6, dtype=int)
        pairs = game._remove_pairs(table)
        assert pairs == 0


# ═══════════════════════════════════════════════════════════
#  迭代抽取
# ═══════════════════════════════════════════════════════════

class TestIterationDraw:

    def test_zero_wish_no_change(self):
        game = IceBlockGame(seed=42)
        box = np.full(6, 100, dtype=int)
        table = np.zeros(6, dtype=int)
        game._do_iteration_draws(box, table, wish_color=0, initial_wish_count=0)
        assert np.sum(box) == 600
        assert np.sum(table) == 0

    def test_terminates(self):
        game = IceBlockGame(seed=42)
        box = np.full(6, 100, dtype=int)
        table = np.zeros(6, dtype=int)
        game._do_iteration_draws(box, table, wish_color=0, initial_wish_count=10)
        assert np.sum(table) > 0

    def test_conservation(self):
        game = IceBlockGame(seed=42)
        box = np.full(6, 100, dtype=int)
        table = np.zeros(6, dtype=int)
        game._do_iteration_draws(box, table, wish_color=0, initial_wish_count=8)
        assert np.sum(box) + np.sum(table) == 600

    def test_stops_when_box_empty(self):
        game = IceBlockGame(num_colors=6, seed=42)
        box = np.array([2, 0, 0, 0, 0, 0], dtype=int)
        table = np.zeros(6, dtype=int)
        game._do_iteration_draws(box, table, wish_color=0, initial_wish_count=100)
        assert np.sum(box) == 0
        assert np.sum(table) == 2


# ═══════════════════════════════════════════════════════════
#  完整模拟 · 核心不变量
# ═══════════════════════════════════════════════════════════

class TestSimulationInvariants:
    """对默认参数 (6 色 ×100, 初始 24) 跑 300 次, 验证不变量."""

    ROUNDS = 300

    @pytest.fixture(autouse=True)
    def _run_games(self):
        game = IceBlockGame(seed=42)
        self.results = [game.simulate_once() for _ in range(self.ROUNDS)]

    def test_conservation(self):
        for r in self.results:
            assert r["final_sum"] + r["remaining_in_box"] == 600

    def test_components(self):
        for r in self.results:
            assert r["final_sum"] == r["total_collected"] + r["remaining_on_table"]

    def test_collected_is_even(self):
        for r in self.results:
            assert r["total_collected"] % 2 == 0

    def test_remaining_bounded(self):
        for r in self.results:
            assert 0 <= r["remaining_on_table"] <= 6

    def test_in_valid_range(self):
        for r in self.results:
            assert 24 <= r["final_sum"] <= 600

    def test_box_non_negative(self):
        for r in self.results:
            assert r["remaining_in_box"] >= 0


# ═══════════════════════════════════════════════════════════
#  确定性 & 可复现性
# ═══════════════════════════════════════════════════════════

class TestDeterminism:

    def test_same_seed_same_result(self):
        r1 = IceBlockGame(seed=12345).simulate_once()
        r2 = IceBlockGame(seed=12345).simulate_once()
        assert r1 == r2

    def test_different_seeds_vary(self):
        base = IceBlockGame(seed=1).simulate_once()["final_sum"]
        found_different = any(
            IceBlockGame(seed=s).simulate_once()["final_sum"] != base
            for s in range(2, 50)
        )
        assert found_different


# ═══════════════════════════════════════════════════════════
#  完全确定性场景 (可精确预测结果)
# ═══════════════════════════════════════════════════════════

class TestDeterministicScenarios:

    def test_single_color_drain_box(self):
        """单色 100 块, 初始抽 10: 迭代抽完所有, 50 对全收, 最终 = 100."""
        game = IceBlockGame(num_colors=1, blocks_per_color=100, initial_draw=10, seed=0)
        r = game.simulate_once()
        assert r["final_sum"] == 100
        assert r["remaining_in_box"] == 0
        assert r["remaining_on_table"] == 0
        assert r["total_collected"] == 100

    def test_single_color_odd(self):
        """单色 11 块, 初始抽 5: 全部抽完, 剩 1 块无法配对."""
        game = IceBlockGame(num_colors=1, blocks_per_color=11, initial_draw=5, seed=0)
        r = game.simulate_once()
        assert r["final_sum"] == 11
        assert r["remaining_in_box"] == 0
        assert r["remaining_on_table"] == 1
        assert r["total_collected"] == 10

    def test_all_drawn_exact(self):
        """2 色各 2 块, 初始抽 4 = 全部抽完, 2 对全收."""
        game = IceBlockGame(num_colors=2, blocks_per_color=2, initial_draw=4, seed=0)
        r = game.simulate_once()
        assert r["final_sum"] == 4
        assert r["remaining_in_box"] == 0

    def test_single_color_large_initial(self):
        """单色 20, 初始抽 20: 一次抽完, 10 对全收."""
        game = IceBlockGame(num_colors=1, blocks_per_color=20, initial_draw=20, seed=0)
        r = game.simulate_once()
        assert r["final_sum"] == 20
        assert r["total_collected"] == 20
        assert r["remaining_on_table"] == 0


# ═══════════════════════════════════════════════════════════
#  自定义参数
# ═══════════════════════════════════════════════════════════

class TestCustomParameters:

    def test_small_game_conservation(self):
        game = IceBlockGame(num_colors=3, blocks_per_color=10, initial_draw=6, seed=42)
        for _ in range(100):
            r = game.simulate_once()
            assert r["final_sum"] + r["remaining_in_box"] == 30

    def test_two_colors(self):
        game = IceBlockGame(num_colors=2, blocks_per_color=50, initial_draw=10, seed=42)
        for _ in range(100):
            r = game.simulate_once()
            assert r["final_sum"] + r["remaining_in_box"] == 100
            assert r["remaining_on_table"] <= 2
            assert r["total_collected"] % 2 == 0


# ═══════════════════════════════════════════════════════════
#  批量模拟
# ═══════════════════════════════════════════════════════════

class TestBatchRun:

    def test_correct_length(self):
        results = IceBlockGame(seed=42).run_simulations(200)
        assert len(results) == 200

    def test_all_in_range(self):
        results = IceBlockGame(seed=42).run_simulations(200)
        assert np.all(results >= 24)
        assert np.all(results <= 600)

    def test_statistical_stability(self):
        """两组独立大样本的均值偏差 < 2%."""
        r1 = IceBlockGame(seed=100).run_simulations(10_000)
        r2 = IceBlockGame(seed=200).run_simulations(10_000)
        rel_diff = abs(np.mean(r1) - np.mean(r2)) / np.mean(r1)
        assert rel_diff < 0.02


# ═══════════════════════════════════════════════════════════
#  分析函数
# ═══════════════════════════════════════════════════════════

class TestAnalysis:

    @pytest.fixture()
    def stats(self):
        results = IceBlockGame(seed=42).run_simulations(2000)
        return analyze_results(results)

    def test_keys(self, stats):
        for key in ("mean", "median", "std", "min", "max", "percentiles", "intervals"):
            assert key in stats

    def test_interval_probs_sum_to_one(self, stats):
        total = sum(p for _, _, p in stats["intervals"])
        assert abs(total - 1.0) < 1e-9

    def test_percentile_ordering(self, stats):
        p = stats["percentiles"]
        assert p["5%"] <= p["25%"] <= p["50%"] <= p["75%"] <= p["95%"]

    def test_min_max(self, stats):
        assert stats["min"] <= stats["mean"] <= stats["max"]
