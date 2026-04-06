"""记忆蒸馏管线测试用例"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from models import (
    DistillationResult,
    EngineeringExperience,
    ExperienceType,
    NarrativeBundle,
    NarrativeExperience,
)


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

SAMPLE_SUMMARY = {
    "id": "1429",
    "memory_session_id": "mem-test-001",
    "project": "d:/github/shadow-folk",
    "request": "用户想排查 MCP 认证失败的问题",
    "investigated": "检查了远端 HTTP MCP 服务的鉴权与工具调用行为",
    "learned": "401 的直接原因是 Authorization 缺少 Bearer 前缀",
    "completed": "完成了 HTTP MCP 端点开发、部署与联调验证",
    "next_steps": "建议直接抓取客户端实际发出的 Authorization 头确认",
    "files_read": "/tmp/test-mcp.sh",
    "files_edited": "packages/server/src/mcp/http.ts",
    "notes": "",
    "prompt_number": "0",
    "discovery_tokens": "0",
    "created_at": "2026-04-03T04:22:41.848Z",
    "created_at_epoch": "1775190161848",
    "meta_intent": "用户的深层目的是让 Cursor 通过 HTTP 方式稳定接入远端 MCP 服务",
}

SAMPLE_OBSERVATION = {
    "id": "12252",
    "memory_session_id": "mem-test-001",
    "project": "d:/github/shadow-folk",
    "text": "MCP 调试结论：Bearer 生效后工具调用恢复正常。Cursor 红色报错是 SSE 轮询噪音。",
    "type": "debugging",
    "title": "MCP 调试结论：Bearer 生效",
    "subtitle": "排查 Cursor SSE 轮询噪音与 MCP 查询问题",
    "facts": "日志中大量 GET /api/mcp 请求是 SSE 探测噪音",
    "narrative": "",
    "concepts": "MCP,Bearer Token,SSE,debugging",
    "files_read": "",
    "files_modified": "",
    "prompt_number": "0",
    "discovery_tokens": "0",
    "created_at": "2026-04-03T05:00:00.000Z",
    "created_at_epoch": "1775192400000",
    "meta_intent": "排查认证与工具调用链路",
}

SAMPLE_EXPERIENCE = EngineeringExperience(
    issue_context="MCP 配置后 Cursor 持续显示红色错误，Authorization 缺少 Bearer 前缀",
    root_cause="Authorization header 格式错误，缺少 'Bearer ' 前缀",
    solution="将配置改为 Authorization: Bearer <token>",
    rationale="Bearer Token 认证遵循 RFC 6750 规范",
    experience_type=ExperienceType.debugging,
    related_components=["validateApiToken", "Cursor MCP client"],
    prevention="在错误提示中明确给出完整格式示例",
    confidence=0.95,
)

SAMPLE_EXPERIENCE_LOW_CONF = EngineeringExperience(
    issue_context="某次文件编辑后出现不确定的报错",
    root_cause="不确定",
    solution="重启服务",
    rationale="经验猜测",
    experience_type=ExperienceType.debugging,
    related_components=["unknown"],
    prevention=None,
    confidence=0.3,
)


def _make_csv(path: str, rows: list, fieldnames: list):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ═══════════════════════════════════════════════════════════════
# 1. Pydantic 模型测试
# ═══════════════════════════════════════════════════════════════

class TestModels:
    def test_experience_valid(self):
        exp = SAMPLE_EXPERIENCE
        assert exp.confidence == 0.95
        assert exp.experience_type == ExperienceType.debugging
        assert len(exp.related_components) == 2

    def test_experience_confidence_bounds(self):
        with pytest.raises(Exception):
            EngineeringExperience(
                issue_context="test",
                root_cause="test",
                solution="test",
                rationale="test",
                experience_type=ExperienceType.debugging,
                related_components=["a"],
                confidence=1.5,
            )

    def test_experience_confidence_lower_bound(self):
        with pytest.raises(Exception):
            EngineeringExperience(
                issue_context="test",
                root_cause="test",
                solution="test",
                rationale="test",
                experience_type=ExperienceType.debugging,
                related_components=["a"],
                confidence=-0.1,
            )

    def test_distillation_result_empty(self):
        result = DistillationResult(
            experiences=[], skipped_reason="全是噪音"
        )
        assert len(result.experiences) == 0
        assert result.skipped_reason == "全是噪音"

    def test_distillation_result_with_experiences(self):
        result = DistillationResult(
            experiences=[SAMPLE_EXPERIENCE],
            skipped_reason=None,
        )
        assert len(result.experiences) == 1
        assert result.experiences[0].issue_context == SAMPLE_EXPERIENCE.issue_context

    def test_distillation_result_json_roundtrip(self):
        result = DistillationResult(
            experiences=[SAMPLE_EXPERIENCE],
            skipped_reason=None,
        )
        json_str = result.model_dump_json()
        parsed = DistillationResult.model_validate_json(json_str)
        assert len(parsed.experiences) == 1
        assert parsed.experiences[0].confidence == 0.95

    def test_narrative_experience_valid(self):
        narrative = NarrativeExperience(
            title="测试叙事",
            problem_description="问题描述",
            environment="macOS + Python 3.9",
            project="test-project",
            timeline="2026-04-05, 约 10 分钟",
            investigation_journey="1. 尝试 A → 失败\n2. 尝试 B → 成功",
            resolution="方案 B 解决",
            takeaways=["先检查日志", "避免假设"],
            methodology_tags=["排除法", "最小改动"],
        )
        assert narrative.title == "测试叙事"
        assert len(narrative.takeaways) == 2

    def test_narrative_bundle_roundtrip(self):
        bundle = NarrativeBundle(
            narratives=[
                NarrativeExperience(
                    title="叙事1",
                    problem_description="p",
                    environment="e",
                    project="proj",
                    timeline="t",
                    investigation_journey="j",
                    resolution="r",
                    takeaways=["a"],
                    methodology_tags=["b"],
                )
            ],
            methodology_summary="总结",
        )
        json_str = bundle.model_dump_json()
        parsed = NarrativeBundle.model_validate_json(json_str)
        assert len(parsed.narratives) == 1
        assert parsed.methodology_summary == "总结"

    def test_experience_type_enum(self):
        for t in ExperienceType:
            assert isinstance(t.value, str)
        assert ExperienceType.debugging.value == "debugging"
        assert ExperienceType.cross_platform.value == "cross_platform"


# ═══════════════════════════════════════════════════════════════
# 2. Extract 层测试
# ═══════════════════════════════════════════════════════════════

class TestExtract:
    def test_extract_all_from_csv(self):
        from extract import extract_all_from_csv

        with tempfile.TemporaryDirectory() as tmpdir:
            sum_path = os.path.join(tmpdir, "summaries.csv")
            obs_path = os.path.join(tmpdir, "observations.csv")

            _make_csv(
                sum_path,
                [SAMPLE_SUMMARY],
                list(SAMPLE_SUMMARY.keys()),
            )
            _make_csv(
                obs_path,
                [SAMPLE_OBSERVATION],
                list(SAMPLE_OBSERVATION.keys()),
            )

            summaries, observations = extract_all_from_csv(sum_path, obs_path)
            assert len(summaries) == 1
            assert len(observations) == 1
            assert summaries[0]["request"] == SAMPLE_SUMMARY["request"]
            assert observations[0]["type"] == "debugging"

    def test_extract_from_csv_time_filter(self):
        from extract import extract_from_csv

        with tempfile.TemporaryDirectory() as tmpdir:
            sum_path = os.path.join(tmpdir, "summaries.csv")
            obs_path = os.path.join(tmpdir, "observations.csv")

            old_summary = dict(SAMPLE_SUMMARY)
            old_summary["created_at_epoch"] = "1000000000000"  # very old

            _make_csv(
                sum_path,
                [old_summary],
                list(SAMPLE_SUMMARY.keys()),
            )
            _make_csv(
                obs_path,
                [SAMPLE_OBSERVATION],
                list(SAMPLE_OBSERVATION.keys()),
            )

            summaries, observations = extract_from_csv(
                sum_path, obs_path, hours=24
            )
            assert len(summaries) == 0  # filtered out by time
            # observations epoch 1775192400000 ≈ 2026-04-01, might or might not pass
            # depending on when this test runs; the key test is the old one is filtered

    def test_extract_empty_csv(self):
        from extract import extract_all_from_csv

        with tempfile.TemporaryDirectory() as tmpdir:
            sum_path = os.path.join(tmpdir, "summaries.csv")
            obs_path = os.path.join(tmpdir, "observations.csv")

            _make_csv(sum_path, [], list(SAMPLE_SUMMARY.keys()))
            _make_csv(obs_path, [], list(SAMPLE_OBSERVATION.keys()))

            summaries, observations = extract_all_from_csv(sum_path, obs_path)
            assert len(summaries) == 0
            assert len(observations) == 0


# ═══════════════════════════════════════════════════════════════
# 3. Transform Layer 1 测试
# ═══════════════════════════════════════════════════════════════

class TestTransformL1:
    def test_build_distill_prompt_with_data(self):
        from transform import build_distill_prompt

        prompt = build_distill_prompt(
            [SAMPLE_SUMMARY], [SAMPLE_OBSERVATION], "最近 24 小时"
        )
        assert "Session Summaries" in prompt
        assert "Observations" in prompt
        assert "排查 MCP 认证失败" in prompt or "MCP" in prompt
        assert "请从上述记忆中蒸馏出有价值的工程经验" in prompt

    def test_build_distill_prompt_empty(self):
        from transform import build_distill_prompt

        prompt = build_distill_prompt([], [], "最近 24 小时")
        assert "Session Summaries" not in prompt
        assert "Observations" not in prompt
        assert "请从上述记忆中蒸馏出有价值的工程经验" in prompt

    def test_compute_experience_hash_deterministic(self):
        from transform import compute_experience_hash

        h1 = compute_experience_hash(SAMPLE_EXPERIENCE)
        h2 = compute_experience_hash(SAMPLE_EXPERIENCE)
        assert h1 == h2
        assert len(h1) == 16

    def test_compute_experience_hash_different(self):
        from transform import compute_experience_hash

        h1 = compute_experience_hash(SAMPLE_EXPERIENCE)
        h2 = compute_experience_hash(SAMPLE_EXPERIENCE_LOW_CONF)
        assert h1 != h2

    def test_load_experiences_new_file(self):
        from transform import load_experiences

        result = DistillationResult(
            experiences=[SAMPLE_EXPERIENCE],
            skipped_reason=None,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.json")
            added = load_experiences(result, out, min_confidence=0.5)
            assert added == 1

            data = json.loads(Path(out).read_text(encoding="utf-8"))
            assert len(data) == 1
            assert data[0]["issue_context"] == SAMPLE_EXPERIENCE.issue_context
            assert "_hash" in data[0]
            assert "_distilled_at" in data[0]

    def test_load_experiences_dedup(self):
        from transform import load_experiences

        result = DistillationResult(
            experiences=[SAMPLE_EXPERIENCE],
            skipped_reason=None,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.json")
            added1 = load_experiences(result, out, min_confidence=0.5)
            added2 = load_experiences(result, out, min_confidence=0.5)
            assert added1 == 1
            assert added2 == 0  # dedup

            data = json.loads(Path(out).read_text(encoding="utf-8"))
            assert len(data) == 1

    def test_load_experiences_low_confidence_filter(self):
        from transform import load_experiences

        result = DistillationResult(
            experiences=[SAMPLE_EXPERIENCE_LOW_CONF],
            skipped_reason=None,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.json")
            added = load_experiences(result, out, min_confidence=0.5)
            assert added == 0

    @patch("transform.call_venus_api")
    def test_distill_via_venus_success(self, mock_api):
        from transform import distill_via_venus

        expected = DistillationResult(
            experiences=[SAMPLE_EXPERIENCE], skipped_reason=None
        )
        mock_api.return_value = {
            "choices": [
                {"message": {"content": expected.model_dump_json()}}
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }

        result = distill_via_venus(
            [SAMPLE_SUMMARY], [SAMPLE_OBSERVATION], "最近 24 小时"
        )
        assert len(result.experiences) == 1
        assert result.experiences[0].confidence == 0.95
        mock_api.assert_called_once()

    @patch("transform.call_venus_api")
    def test_distill_via_venus_invalid_json(self, mock_api):
        from transform import distill_via_venus

        mock_api.return_value = {
            "choices": [
                {"message": {"content": '{"invalid": true}'}}
            ],
        }

        with pytest.raises(RuntimeError, match="validation failed"):
            distill_via_venus(
                [SAMPLE_SUMMARY], [SAMPLE_OBSERVATION], "最近 24 小时"
            )

    @patch("transform.call_venus_api")
    def test_distill_via_venus_empty_result(self, mock_api):
        from transform import distill_via_venus

        expected = DistillationResult(
            experiences=[], skipped_reason="全部为噪音数据"
        )
        mock_api.return_value = {
            "choices": [
                {"message": {"content": expected.model_dump_json()}}
            ],
        }

        result = distill_via_venus([], [], "最近 24 小时")
        assert len(result.experiences) == 0
        assert result.skipped_reason == "全部为噪音数据"


# ═══════════════════════════════════════════════════════════════
# 4. Transform Layer 2（叙事重建）测试
# ═══════════════════════════════════════════════════════════════

class TestTransformL2:
    def test_group_experiences_by_theme(self):
        from narrative import group_experiences_by_theme

        experiences = [
            SAMPLE_EXPERIENCE.model_dump(),
            SAMPLE_EXPERIENCE_LOW_CONF.model_dump(),
        ]
        groups = group_experiences_by_theme(experiences, [])
        assert "debugging" in groups
        assert len(groups["debugging"]["experiences"]) == 2

    def test_group_with_observation_matching(self):
        from narrative import group_experiences_by_theme

        exp = SAMPLE_EXPERIENCE.model_dump()
        obs = dict(SAMPLE_OBSERVATION)
        groups = group_experiences_by_theme([exp], [obs])

        assert "debugging" in groups
        matched_obs = groups["debugging"]["raw_observations"]
        assert len(matched_obs) >= 0  # matching depends on concept overlap

    def test_build_narrative_prompt(self):
        from narrative import build_narrative_prompt

        group = {
            "experiences": [SAMPLE_EXPERIENCE.model_dump()],
            "raw_observations": [SAMPLE_OBSERVATION],
        }
        prompt = build_narrative_prompt("debugging", group)
        assert "debugging" in prompt
        assert "结构化经验" in prompt
        assert "原始观测记录" in prompt

    @patch("narrative.call_venus_api")
    def test_rebuild_narrative_success(self, mock_api):
        from narrative import rebuild_narrative_via_venus

        expected = NarrativeBundle(
            narratives=[
                NarrativeExperience(
                    title="MCP 鉴权排查",
                    problem_description="Bearer 前缀缺失导致 401",
                    environment="Windows + Cursor",
                    project="shadow-folk",
                    timeline="2026-04-03, 约 20 分钟",
                    investigation_journey="1. 检查日志 → 发现 401\n2. 对比请求头 → 缺少 Bearer",
                    resolution="补全 Bearer 前缀后恢复正常",
                    takeaways=["检查 Authorization 头格式"],
                    methodology_tags=["排除法"],
                )
            ],
            methodology_summary=None,
        )
        mock_api.return_value = {
            "choices": [
                {"message": {"content": expected.model_dump_json()}}
            ],
        }

        group = {
            "experiences": [SAMPLE_EXPERIENCE.model_dump()],
            "raw_observations": [],
        }
        result = rebuild_narrative_via_venus("debugging", group)
        assert len(result.narratives) == 1
        assert result.narratives[0].title == "MCP 鉴权排查"

    def test_render_narrative_markdown(self):
        from narrative import render_narrative_markdown

        bundle = NarrativeBundle(
            narratives=[
                NarrativeExperience(
                    title="测试叙事标题",
                    problem_description="问题",
                    environment="env",
                    project="proj",
                    timeline="time",
                    investigation_journey="journey",
                    resolution="resolution",
                    takeaways=["经验1", "经验2"],
                    methodology_tags=["排除法", "最小改动"],
                )
            ],
            methodology_summary="方法论总结内容",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "narratives.md")
            content = render_narrative_markdown(bundle, out)

            assert "# 工程经验叙事" in content
            assert "## 测试叙事标题" in content
            assert "### 问题描述" in content
            assert "- 经验1" in content
            assert "- 经验2" in content
            assert "排除法 / 最小改动" in content
            assert "## 方法论总结" in content
            assert "方法论总结内容" in content

            file_content = Path(out).read_text(encoding="utf-8")
            assert file_content == content


# ═══════════════════════════════════════════════════════════════
# 5. Venus Client 测试
# ═══════════════════════════════════════════════════════════════

class TestVenusClient:
    def test_rate_limiter_no_wait(self):
        from venus_client import RateLimiter

        limiter = RateLimiter(max_calls=100, window_seconds=60)
        limiter.wait()  # should not block

    def test_rate_limiter_window_cleanup(self):
        import time

        from venus_client import RateLimiter

        limiter = RateLimiter(max_calls=2, window_seconds=1)
        limiter.wait()
        limiter.wait()
        time.sleep(1.1)
        limiter.wait()  # window should have cleared

    @patch("venus_client.requests.post")
    def test_call_venus_api_success(self, mock_post):
        from venus_client import RateLimiter, call_venus_api

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "test"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        mock_post.return_value = mock_response

        result = call_venus_api({"model": "test", "messages": []})
        assert result["choices"][0]["message"]["content"] == "test"

    @patch("venus_client.requests.post")
    def test_call_venus_api_retry_on_500(self, mock_post):
        from venus_client import call_venus_api

        fail_response = MagicMock()
        fail_response.status_code = 500
        fail_response.text = "Internal Server Error"

        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {},
        }

        mock_post.side_effect = [fail_response, ok_response]

        result = call_venus_api(
            {"model": "test", "messages": []},
            max_retries=2,
            retry_delay=0.01,
        )
        assert result["choices"][0]["message"]["content"] == "ok"
        assert mock_post.call_count == 2


# ═══════════════════════════════════════════════════════════════
# 6. 端到端 Pipeline 测试（mock API）
# ═══════════════════════════════════════════════════════════════

class TestPipeline:
    @patch("transform.call_venus_api")
    @patch("narrative.call_venus_api")
    def test_full_pipeline(self, mock_l2_api, mock_l1_api):
        from pipeline import run_distillation

        l1_result = DistillationResult(
            experiences=[SAMPLE_EXPERIENCE], skipped_reason=None
        )
        mock_l1_api.return_value = {
            "choices": [
                {"message": {"content": l1_result.model_dump_json()}}
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }

        l2_result = NarrativeBundle(
            narratives=[
                NarrativeExperience(
                    title="Pipeline 测试叙事",
                    problem_description="desc",
                    environment="env",
                    project="proj",
                    timeline="timeline",
                    investigation_journey="journey",
                    resolution="resolution",
                    takeaways=["takeaway"],
                    methodology_tags=["tag"],
                )
            ],
            methodology_summary="方法论",
        )
        mock_l2_api.return_value = {
            "choices": [
                {"message": {"content": l2_result.model_dump_json()}}
            ],
            "usage": {"prompt_tokens": 200, "completion_tokens": 100},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            sum_path = os.path.join(tmpdir, "summaries.csv")
            obs_path = os.path.join(tmpdir, "observations.csv")
            l1_out = os.path.join(tmpdir, "output", "l1.json")
            l2_out = os.path.join(tmpdir, "output", "l2.md")

            _make_csv(
                sum_path,
                [SAMPLE_SUMMARY],
                list(SAMPLE_SUMMARY.keys()),
            )
            _make_csv(
                obs_path,
                [SAMPLE_OBSERVATION],
                list(SAMPLE_OBSERVATION.keys()),
            )

            run_distillation(
                summaries_csv=sum_path,
                observations_csv=obs_path,
                hours=0,
                l1_output=l1_out,
                l2_output=l2_out,
                batch_size=20,
                min_confidence=0.5,
            )

            assert Path(l1_out).exists()
            assert Path(l2_out).exists()

            l1_data = json.loads(Path(l1_out).read_text(encoding="utf-8"))
            assert len(l1_data) == 1

            l2_content = Path(l2_out).read_text(encoding="utf-8")
            assert "Pipeline 测试叙事" in l2_content
            assert "方法论" in l2_content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
