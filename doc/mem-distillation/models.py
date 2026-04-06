"""蒸馏数据模型定义 — Pydantic v2"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ExperienceType(str, Enum):
    debugging = "debugging"
    architecture = "architecture"
    deployment = "deployment"
    configuration = "configuration"
    cross_platform = "cross_platform"
    workflow = "workflow"
    tooling = "tooling"


class EngineeringExperience(BaseModel):
    issue_context: str = Field(
        description="遇到了什么问题，包含具体错误信息或异常现象"
    )
    root_cause: str = Field(
        description="根因分析，问题的真正原因是什么"
    )
    solution: str = Field(
        description="最终决定怎么做，具体的修复步骤或方案"
    )
    rationale: str = Field(
        description="为什么这么做，背后的第一性原理或权衡取舍"
    )
    experience_type: ExperienceType = Field(
        description="经验类型分类"
    )
    related_components: List[str] = Field(
        description="关联的系统模块、文件或技术栈"
    )
    prevention: Optional[str] = Field(
        default=None,
        description="如何从机制上避免同类问题再次发生"
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="该经验的置信度，0-1 之间"
    )


class DistillationResult(BaseModel):
    experiences: List[EngineeringExperience] = Field(
        description="从本批记忆中蒸馏出的工程经验列表，"
                    "如果没有有价值的经验则返回空列表"
    )
    skipped_reason: Optional[str] = Field(
        default=None,
        description="如果本批记忆全部为噪音，说明跳过原因"
    )


class RawMemoryBatch(BaseModel):
    session_id: str
    project: str
    summaries: List[dict]
    observations: List[dict]
    time_range: str


# ── Layer 2 叙事模型 ──

class NarrativeExperience(BaseModel):
    title: str = Field(description="经验标题，简洁有力")
    problem_description: str = Field(description="问题描述，包含具体错误和环境背景")
    environment: str = Field(description="局限环境：技术栈、部署方式、工具链等")
    project: str = Field(description="所属项目")
    timeline: str = Field(description="处理时间和耗时")
    investigation_journey: str = Field(
        description="完整的排查过程，必须包含失败的尝试和每次失败带来的线索。"
                    "用编号步骤描述，标注哪些路走通了、哪些走不通、每步得到了什么结论"
    )
    resolution: str = Field(description="最终解决方案和结果")
    takeaways: List[str] = Field(
        description="可复用的经验提炼，每条以动词开头，可直接指导行动"
    )
    methodology_tags: List[str] = Field(
        description="该经验体现的方法论标签，如：排除法、最小改动、噪音过滤、机制化预防"
    )


class NarrativeBundle(BaseModel):
    narratives: List[NarrativeExperience]
    methodology_summary: Optional[str] = Field(
        default=None,
        description="如果多条经验呈现出一致的解决问题思路，在此总结方法论"
    )
