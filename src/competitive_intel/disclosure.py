"""按角色最小披露的字段级遮蔽策略。

敏感项目的“身份”（项目名、机构）与“实验细节”（关键实验版本结论、内部备注、
时间线细节与证据出处）对不同角色呈现不同粒度；比较分析所需的结构化字段
（靶点、机制、适应症、阶段、临床属性标签）始终可见，否则赛道比较无法进行。

- analyst：外部情报分析员。只见公开项目的身份与实验细节；受限/敏感项目只看
  得到用于比较的结构信息。
- reviewer：科学平台主管/评审科学家。可见身份，敏感项目的实验细节仍遮蔽。
- committee：投决委员，对已发布快照拥有完整披露。
- auditor：审计，拥有完整披露以核证不可变历史。
"""

from __future__ import annotations

from typing import Any, Mapping

from .jsonio import redacted

# 可见的字段类别 -> 该角色可见该类别的最高敏感集合。
_ROLE_FIELD_VISIBILITY: Mapping[str, Mapping[str, frozenset[str]]] = {
    "analyst": {
        "identity": frozenset({"public"}),
        "experiment_detail": frozenset({"public"}),
        "timeline_detail": frozenset({"public"}),
        "evidence_refs": frozenset({"public", "restricted"}),
        "notes": frozenset({"public"}),
    },
    "reviewer": {
        "identity": frozenset({"public", "restricted", "sensitive"}),
        "experiment_detail": frozenset({"public", "restricted"}),
        "timeline_detail": frozenset({"public", "restricted", "sensitive"}),
        "evidence_refs": frozenset({"public", "restricted", "sensitive"}),
        "notes": frozenset({"public", "restricted"}),
    },
    "committee": {
        "identity": frozenset({"public", "restricted", "sensitive"}),
        "experiment_detail": frozenset({"public", "restricted", "sensitive"}),
        "timeline_detail": frozenset({"public", "restricted", "sensitive"}),
        "evidence_refs": frozenset({"public", "restricted", "sensitive"}),
        "notes": frozenset({"public", "restricted", "sensitive"}),
    },
    "auditor": {
        "identity": frozenset({"public", "restricted", "sensitive"}),
        "experiment_detail": frozenset({"public", "restricted", "sensitive"}),
        "timeline_detail": frozenset({"public", "restricted", "sensitive"}),
        "evidence_refs": frozenset({"public", "restricted", "sensitive"}),
        "notes": frozenset({"public", "restricted", "sensitive"}),
    },
}

REDACTION_PLACEHOLDER = "***REDACTED***"


def can_see(role: str, field_class: str, sensitivity: str) -> bool:
    return sensitivity in _ROLE_FIELD_VISIBILITY[role][field_class]


def disclose_item(role: str, item: Mapping[str, Any]) -> dict[str, Any]:
    """对单条记录版本内容应用字段级遮蔽，结构字段始终保留。"""

    sensitivity = item["sensitivity"]
    view: dict[str, Any] = {
        "record_id": item["record_id"],
        "version": item["version"],
        "source": item["source"],
        "origin": item["origin"],
        "sensitivity": sensitivity,
        "target": item["target"],
        "mechanism": item["mechanism"],
        "modalities": list(item["modalities"]),
        "indications": list(item["indications"]),
        "clinical_attributes": item["clinical_attributes"],
        "stage": item["stage"],
        "evidence_tier": item["evidence_tier"],
        "asset_id": item.get("asset_id"),
        "membership_id": item.get("membership_id"),
        "credibility": item.get("credibility"),
        "content_sha256": item.get("content_sha256"),
    }
    if can_see(role, "identity", sensitivity):
        view["display_name"] = item["display_name"]
        view["organization"] = item["organization"]
    else:
        view["display_name"] = redacted(item["display_name"])
        view["organization"] = redacted(item["organization"])

    if can_see(role, "experiment_detail", sensitivity):
        view["experiments"] = item["experiments"]
    else:
        view["experiments"] = [
            {"experiment_version_id": redacted(exp["experiment_version_id"]), "redacted": True}
            for exp in item["experiments"]
        ]

    if can_see(role, "timeline_detail", sensitivity):
        view["timeline"] = item["timeline"]
    else:
        # 保留日期与事件类型（时间线结构可比较），遮蔽叙述与出处细节。
        view["timeline"] = [
            {
                "event_type": event["event_type"],
                "event_date": event["event_date"],
                "summary": redacted(event["summary"]),
                "source_ref": redacted(event.get("source_ref")),
            }
            for event in item["timeline"]
        ]

    if can_see(role, "evidence_refs", sensitivity):
        view["evidence_refs"] = item["evidence_refs"]
    else:
        view["evidence_refs"] = []

    if can_see(role, "notes", sensitivity):
        view["notes"] = item.get("notes")
    else:
        view["notes"] = redacted(item.get("notes"))

    # 可信度等级本身可用于比较；判定理由与证据出处可能复述实验细节，
    # 对看不到实验细节的角色一并遮蔽。
    credibility = item.get("credibility")
    if credibility is not None and not can_see(role, "experiment_detail", sensitivity):
        view["credibility"] = {
            "annotation_id": credibility["annotation_id"],
            "level": credibility["level"],
            "rationale": redacted(credibility.get("rationale")),
            "evidence_refs": [],
            "annotated_by": credibility.get("annotated_by"),
            "annotated_at": credibility.get("annotated_at"),
        }
    return view


def disclose_evidence_gaps(
    role: str, gaps: list[dict[str, Any]], sensitivity_by_ref: Mapping[str, str]
) -> list[dict[str, Any]]:
    result = []
    for gap in gaps:
        view = dict(gap)
        sensitivity = sensitivity_by_ref.get(gap["record_ref"])
        if sensitivity is not None and not can_see(role, "experiment_detail", sensitivity):
            view["detail"] = redacted(gap.get("detail"))
            if "source_ref" in gap:
                view["source_ref"] = redacted(gap.get("source_ref"))
            if "annotation_id" in gap:
                view["annotation_id"] = gap["annotation_id"]
        result.append(view)
    return result
