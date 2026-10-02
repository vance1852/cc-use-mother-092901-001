"""社会资本准入退出域使用的只读数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PolicyVersion:
    """一版准入门槛规则。"""

    policy_id: str
    version: int
    thresholds: dict[str, Any]
    published_by: str
    published_at: str
    effective_at: str
    status: str


@dataclass(frozen=True)
class Investor:
    """社会资本投资人主体。"""

    investor_id: str
    name: str
    ownership_kind: str
    created_at: str


@dataclass(frozen=True)
class ProjectVersion:
    """项目的一个已发布版本。"""

    project_id: str
    version: int
    name: str
    total_investment: float
    private_target_ratio: float
    assets: tuple[str, ...]
    published_at: str


@dataclass(frozen=True)
class Project:
    """项目聚合的折叠状态。"""

    project_id: str
    current_version: int
    versions: tuple[ProjectVersion, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Evidence:
    """资格证据登记项。"""

    evidence_id: str
    investor_id: str
    evidence_type: str
    document_ref: str
    content_hash: str
    valid_from: str
    valid_until: str
    verified: bool


@dataclass(frozen=True)
class CapitalPlan:
    """出资计划：总额、民营部分与分期安排。"""

    total_amount: float
    private_amount: float
    private_ratio: float
    tranches: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Guarantee:
    """履约担保。"""

    guarantee_id: str
    investor_id: str
    guarantee_type: str
    amount: float
    currency: str
    status: str
    reference: str
    history: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Approval:
    """一次会签意见。"""

    role: str
    actor_id: str
    decision: str
    comment: str
    signed_at: str


@dataclass(frozen=True)
class ReviewTask:
    """准入退出审查任务（会签流）。"""

    review_id: str
    project_id: str
    task_type: str
    applicant_id: str
    status: str
    policy_version: int
    project_version: int
    proposal: dict[str, Any]
    evidence_hashes: dict[str, str]
    approvals: tuple[Approval, ...]
    competing: tuple[str, ...]
    outcome: str
    sealed_agreement: dict[str, Any] | None
    sequence: int
    created_at: str
    created_by: str
    decided_at: str | None
    message: str = ""
    settlement: dict[str, Any] | None = None


@dataclass(frozen=True)
class AdmissionReceipt:
    """准入域幂等写入回执。"""

    request_id: str
    action: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class ExitSettlement:
    """退出结清结果。"""

    review_id: str
    investor_id: str
    paid_capital: float
    committed_capital: float
    outstanding_capital: float
    unfinished_obligations: tuple[dict[str, Any], ...]
    recourse_amount: float
    guarantees: tuple[dict[str, Any], ...]
    released_assets: tuple[str, ...]
    forfeited_amount: float
    refunded_amount: float
    settled_at: str
