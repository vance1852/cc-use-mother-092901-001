"""把准入域事件流折叠为当前状态或历史时点状态。"""

from __future__ import annotations

from typing import Any


class UnionFind:
    """投资人关联关系的并查集索引。"""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, node: str) -> None:
        if node not in self.parent:
            self.parent[node] = node

    def find(self, node: str) -> str:
        self.add(node)
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != node:
            parent = self.parent[node]
            self.parent[node] = root
            node = parent
        return root

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root

    def group(self, node: str) -> set[str]:
        root = self.find(node)
        return {member for member in self.parent if self.find(member) == root}


class Projection:
    """事件流的内存折叠结果。"""

    def __init__(self) -> None:
        self.policies: dict[str, dict[str, Any]] = {}
        self.investors: dict[str, dict[str, Any]] = {}
        self.relations: list[dict[str, Any]] = []
        self.projects: dict[str, dict[str, Any]] = {}
        self.evidence: dict[str, dict[str, Any]] = {}
        self.guarantees: dict[str, dict[str, Any]] = {}
        self.capital_plans: dict[str, dict[str, Any]] = {}
        self.reviews: dict[str, dict[str, Any]] = {}
        self.review_sequence: dict[str, int] = {}
        self.assets: dict[str, dict[str, Any]] = {}
        self.obligations: dict[str, dict[str, Any]] = {}
        self.union_find = UnionFind()

    def related_group(self, investor_id: str) -> set[str]:
        return self.union_find.group(investor_id)

    def pending_reviews(self) -> list[dict[str, Any]]:
        items = [review for review in self.reviews.values() if review["status"] == "pending"]
        items.sort(key=lambda review: (self.review_sequence[review["review_id"]], review["review_id"]))
        return items

    def awarded_review(self, project_id: str, stage_label: str,
                       task_type: str | None = None) -> dict[str, Any] | None:
        for review in self.reviews.values():
            agreement = review.get("sealed_agreement")
            if (review["status"] == "approved" and agreement
                    and review["project_id"] == project_id
                    and agreement.get("stage_label") == stage_label
                    and (task_type is None or review["task_type"] == task_type)):
                return review
        return None

    def asset(self, asset_key: str) -> dict[str, Any] | None:
        return self.assets.get(asset_key)


def apply_event(projection: Projection, event: dict[str, Any]) -> None:
    """把单个事件应用到投影。"""

    payload = event["payload"]
    kind = event["event_type"]
    uf = projection.union_find

    if kind == "policy.published":
        policy = projection.policies.setdefault(
            payload["policy_id"], {"policy_id": payload["policy_id"], "versions": {}, "active_version": 0})
        policy["versions"][payload["version"]] = payload
        if payload["version"] >= policy["active_version"]:
            policy["active_version"] = payload["version"]
    elif kind == "investor.registered":
        projection.investors[payload["investor_id"]] = payload
        uf.add(payload["investor_id"])
    elif kind == "investor.relation.linked":
        projection.relations.append(payload)
        uf.union(payload["left_id"], payload["right_id"])
    elif kind == "project.published":
        project = projection.projects.setdefault(
            payload["project_id"], {"project_id": payload["project_id"], "versions": {}, "current_version": 0})
        project["versions"][payload["version"]] = payload
        if payload["version"] >= project["current_version"]:
            project["current_version"] = payload["version"]
    elif kind == "evidence.recorded":
        projection.evidence[payload["evidence_id"]] = {**payload, "verified": False, "verified_by": None}
    elif kind == "evidence.verified":
        evidence = projection.evidence[payload["evidence_id"]]
        evidence["verified"] = payload["verified"]
        evidence["verified_by"] = payload.get("verified_by")
    elif kind == "guarantee.registered":
        projection.guarantees[payload["guarantee_id"]] = {
            **payload, "status": "registered", "history": [
                {"status": "registered", "reason": "登记", "at": event["created_at"],
                 "actor_id": event["actor_id"], "amount": payload["amount"]}]}
    elif kind == "guarantee.status_changed":
        guarantee = projection.guarantees[payload["guarantee_id"]]
        guarantee["status"] = payload["status"]
        guarantee["history"].append({
            "status": payload["status"], "reason": payload["reason"], "at": event["created_at"],
            "actor_id": event["actor_id"], "amount": payload.get("amount", guarantee["amount"])})
    elif kind == "capital_plan.submitted":
        projection.capital_plans[payload["plan_id"]] = payload
    elif kind == "review.proposed":
        projection.reviews[payload["review_id"]] = payload
        projection.review_sequence[payload["review_id"]] = event["sequence"]
    elif kind == "review.approval_recorded":
        review = projection.reviews[payload["review_id"]]
        review["approvals"].append(payload["approval"])
    elif kind == "review.decided":
        review = projection.reviews[payload["review_id"]]
        review["status"] = payload["status"]
        review["outcome"] = payload.get("outcome", review["outcome"])
        review["message"] = payload.get("message", "")
        review["decided_at"] = payload.get("decided_at")
        if payload.get("sealed_agreement"):
            review["sealed_agreement"] = payload["sealed_agreement"]
    elif kind == "review.exit_settled":
        review = projection.reviews[payload["review_id"]]
        review["status"] = "exited"
        review["settlement"] = payload["settlement"]
    elif kind == "asset.occupied":
        projection.assets[payload["asset_key"]] = {
            "asset_key": payload["asset_key"], "project_id": payload["project_id"],
            "review_id": payload["review_id"], "investor_id": payload["investor_id"],
            "status": payload["status"], "reason": payload["reason"], "at": event["created_at"]}
    elif kind == "asset.released":
        entry = projection.assets[payload["asset_key"]]
        entry["status"] = "released"
        entry["reason"] = payload["reason"]
        entry["released_at"] = event["created_at"]
        entry["released_by"] = event["actor_id"]
    elif kind == "obligation.recorded":
        projection.obligations[payload["obligation_id"]] = payload
    elif kind == "obligation.updated":
        obligation = projection.obligations[payload["obligation_id"]]
        obligation["status"] = payload["status"]


def build_projection(events) -> Projection:
    """按事件顺序折叠出一个投影。"""

    projection = Projection()
    for event in events:
        apply_event(projection, event)
    return projection
