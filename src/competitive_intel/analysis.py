"""可解释的赛道比较分析。

输入是快照捕获时点冻结的记录版本与归并关系，输出三类结论及其完整依据：

1. 拥挤程度：按“靶点 × 作用机制”归组，以可逐条核对的有序规则判定热度；
2. 尚未被覆盖的临床差异：在适应症宇宙内列出无人占位的临床定位格、
   仅单一资产占据的差异化位置，以及外部占据而内部尚未覆盖的窗口；
3. 证据缺口：逐记录指出证据层级、可信度判定、实验版本与时间线新鲜度的不足，
   每条缺口都引用形成结论的具体记录版本和实验版本。

分析是纯函数、确定性的；规则阈值集中声明，快照同时保存算法版本与输入摘要。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from .contracts import DEVELOPMENT_STAGES, EVIDENCE_TIER_RANK, STAGE_RANK, normalize_label


TRACK_ANALYSIS_VERSION = "track-landscape/1"

# 公开时间线超过该天数无更新即视为停滞。
STALE_TIMELINE_DAYS = 365

# 热度判定规则（按顺序取第一条命中），阈值显式列出以便在结论中解释。
HEAT_RULES: tuple[Mapping[str, Any], ...] = (
    {"level": "saturated", "min_assets": 8, "min_phase2plus": 4},
    {"level": "crowded", "min_assets": 5, "min_phase2plus": 3},
    {"level": "active", "min_assets": 3, "min_phase2plus": 2},
    {"level": "emerging", "min_assets": 2, "min_phase2plus": None},
    {"level": "open", "min_assets": 1, "min_phase2plus": None},
)

# 超过该阶段即算作“进入临床后期”，参与热度与证据缺口判定。
LATE_CLINICAL_STAGE = "phase_2"

CLINICAL_DIMENSIONS: tuple[str, ...] = (
    "patient_segments",
    "combination",
    "line_of_therapy",
    "endpoints",
    "biomarker_strategy",
    "route",
)


@dataclass(frozen=True, slots=True)
class CapturedItem:
    """快照捕获时点的一条记录版本及其冻结归属。"""

    record_id: str
    version: int
    content: Mapping[str, Any]
    membership_id: int | None
    asset_id: str | None
    credibility: Mapping[str, Any] | None
    content_sha256: str

    @property
    def asset_key(self) -> str:
        return self.asset_id or f"standalone:{self.record_id}"


def _as_date(value: str) -> date:
    text = value[:10]
    return datetime.strptime(text, "%Y-%m-%d").date()


def _heat(asset_count: int, phase2plus: int) -> tuple[str, list[dict[str, Any]]]:
    """按有序规则判定热度，并返回每条规则的求值过程。"""

    evaluations: list[dict[str, Any]] = []
    matched = "open"
    for rule in HEAT_RULES:
        asset_hit = asset_count >= rule["min_assets"]
        late_threshold = rule["min_phase2plus"]
        late_hit = True if late_threshold is None else phase2plus >= late_threshold
        is_match = asset_hit and late_hit
        evaluations.append(
            {
                "level": rule["level"],
                "thresholds": {
                    "min_assets": rule["min_assets"],
                    "min_phase2plus": late_threshold,
                },
                "actual": {"assets": asset_count, "phase2plus": phase2plus},
                "matched": is_match,
            }
        )
        if is_match:
            matched = rule["level"]
            break
    return matched, evaluations


def _tracks(items: Sequence[CapturedItem]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[CapturedItem]] = {}
    for item in items:
        key = (
            normalize_label(item.content["target"]),
            normalize_label(item.content["mechanism"]),
        )
        grouped.setdefault(key, []).append(item)

    tracks: list[dict[str, Any]] = []
    for (target_key, mechanism_key), members in sorted(grouped.items()):
        assets: dict[str, list[CapturedItem]] = {}
        for member in members:
            assets.setdefault(member.asset_key, []).append(member)
        lead_rank = max(STAGE_RANK[member.content["stage"]] for member in members)
        phase2plus = sum(
            1
            for asset_members in assets.values()
            if max(STAGE_RANK[m.content["stage"]] for m in asset_members)
            >= STAGE_RANK[LATE_CLINICAL_STAGE]
        )
        heat_level, rule_trace = _heat(len(assets), phase2plus)
        modality_counts: dict[str, int] = {}
        for member in members:
            for modality in member.content["modalities"]:
                label = normalize_label(modality)
                modality_counts[label] = modality_counts.get(label, 0) + 1
        stage_distribution: dict[str, int] = {}
        for member in members:
            stage = member.content["stage"]
            stage_distribution[stage] = stage_distribution.get(stage, 0) + 1
        leaders = sorted(
            {
                member.record_id
                for member in members
                if STAGE_RANK[member.content["stage"]] == lead_rank
            }
        )
        tracks.append(
            {
                "target": members[0].content["target"],
                "target_key": target_key,
                "mechanism": members[0].content["mechanism"],
                "mechanism_key": mechanism_key,
                "asset_count": len(assets),
                "record_count": len(members),
                "internal_assets": sum(
                    1
                    for asset_members in assets.values()
                    if any(m.content["origin"] == "internal" for m in asset_members)
                ),
                "external_assets": sum(
                    1
                    for asset_members in assets.values()
                    if any(m.content["origin"] == "external" for m in asset_members)
                ),
                "lead_stage": next(
                    stage for stage, rank in STAGE_RANK.items() if rank == lead_rank
                ),
                "phase2plus_assets": phase2plus,
                "stage_distribution": dict(sorted(stage_distribution.items())),
                "modality_distribution": dict(sorted(modality_counts.items())),
                "heat": heat_level,
                "heat_rule_trace": rule_trace,
                "leader_records": leaders,
                "asset_keys": sorted(assets),
            }
        )
    return tracks


def _clinical_white_space(
    items: Sequence[CapturedItem], indication_universe: Sequence[str]
) -> dict[str, Any]:
    universe = [normalize_label(value) for value in indication_universe]
    universe_lookup = {
        normalize_label(value): value for value in indication_universe
    }

    # position[(indication, dimension, label)] = 占据该位置的资产集合
    positions: dict[tuple[str, str, str], dict[str, Any]] = {}
    covered_cells: set[tuple[str, str]] = set()
    for item in items:
        is_internal = item.content["origin"] == "internal"
        attributes = item.content["clinical_attributes"]
        for indication in item.content["indications"]:
            indication_key = normalize_label(indication)
            if indication_key not in universe:
                continue
            for dimension, labels in attributes.items():
                if labels:
                    covered_cells.add((indication_key, dimension))
                for label in labels:
                    label_key = normalize_label(label)
                    position = positions.setdefault(
                        (indication_key, dimension, label_key),
                        {
                            "indication": universe_lookup[indication_key],
                            "dimension": dimension,
                            "label": label,
                            "assets": set(),
                            "internal": False,
                        },
                    )
                    position["assets"].add(item.asset_key)
                    position["internal"] = position["internal"] or is_internal

    indications_report: list[dict[str, Any]] = []
    unique_positions: list[dict[str, Any]] = []
    external_only_positions: list[dict[str, Any]] = []
    uncovered_cells: list[dict[str, Any]] = []
    for indication_key in universe:
        covered_dimensions: list[str] = []
        missing_dimensions: list[str] = []
        for dimension in CLINICAL_DIMENSIONS:
            if (indication_key, dimension) in covered_cells:
                covered_dimensions.append(dimension)
            else:
                missing_dimensions.append(dimension)
                uncovered_cells.append(
                    {"indication": universe_lookup[indication_key], "dimension": dimension}
                )
        indications_report.append(
            {
                "indication": universe_lookup[indication_key],
                "covered_dimensions": covered_dimensions,
                "missing_dimensions": missing_dimensions,
            }
        )

    for (_, _, _), position in sorted(
        positions.items(),
        key=lambda entry: (entry[0][0], entry[0][1], entry[0][2]),
    ):
        summary = {
            "indication": position["indication"],
            "dimension": position["dimension"],
            "label": position["label"],
            "asset_count": len(position["assets"]),
            "assets": sorted(position["assets"]),
            "internal_present": position["internal"],
        }
        if len(position["assets"]) == 1:
            unique_positions.append(summary)
        if not position["internal"]:
            external_only_positions.append(summary)

    return {
        "indication_universe": [universe_lookup[key] for key in universe],
        "indications": indications_report,
        "uncovered_cells": uncovered_cells,
        "unique_positions": unique_positions,
        "external_only_positions": external_only_positions,
    }


def _evidence_gaps(items: Sequence[CapturedItem], as_of: str) -> list[dict[str, Any]]:
    as_of_date = _as_date(as_of)
    gaps: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda member: (member.record_id, member.version)):
        ref = f"{item.record_id}@v{item.version}"
        content = item.content

        if item.credibility is None:
            gaps.append(
                {
                    "record_ref": ref,
                    "kind": "credibility_unannotated",
                    "severity": "medium",
                    "detail": "该记录版本尚无分析员证据可信度判定",
                }
            )
        elif item.credibility["level"] == "low":
            gaps.append(
                {
                    "record_ref": ref,
                    "kind": "credibility_low",
                    "severity": "high",
                    "detail": f"可信度判定为 low：{item.credibility['rationale']}",
                    "annotation_id": item.credibility["annotation_id"],
                }
            )

        if EVIDENCE_TIER_RANK[content["evidence_tier"]] < EVIDENCE_TIER_RANK["conference"]:
            gaps.append(
                {
                    "record_ref": ref,
                    "kind": "evidence_tier_weak",
                    "severity": "medium",
                    "detail": f"来源证据层级仅为 {content['evidence_tier']}，缺乏会议或同行评议支撑",
                }
            )

        if not content["experiments"]:
            gaps.append(
                {
                    "record_ref": ref,
                    "kind": "no_experiment_version",
                    "severity": "medium",
                    "detail": "未登记任何关键实验版本，结论无法回溯到具体实验",
                }
            )

        stage_rank = STAGE_RANK[content["stage"]]
        if stage_rank >= STAGE_RANK[LATE_CLINICAL_STAGE]:
            if EVIDENCE_TIER_RANK[content["evidence_tier"]] < EVIDENCE_TIER_RANK["peer_reviewed"]:
                gaps.append(
                    {
                        "record_ref": ref,
                        "kind": "stage_evidence_mismatch",
                        "severity": "high",
                        "detail": (
                            f"声称阶段已达 {content['stage']}，但来源证据层级 "
                            f"{content['evidence_tier']} 弱于 peer_reviewed"
                        ),
                    }
                )
            trial_events = {
                event["event_type"]
                for event in content["timeline"]
                if event["event_type"] in {"trial_started", "trial_update", "trial_readout"}
            }
            if not trial_events:
                gaps.append(
                    {
                        "record_ref": ref,
                        "kind": "timeline_stage_mismatch",
                        "severity": "high",
                        "detail": "声称进入临床后期，但公开时间线缺少试验启动/更新/读数事件",
                    }
                )

        if content["timeline"]:
            latest_event = max(content["timeline"], key=lambda event: event["event_date"])
            age_days = (as_of_date - _as_date(latest_event["event_date"])).days
            if age_days > STALE_TIMELINE_DAYS:
                gaps.append(
                    {
                        "record_ref": ref,
                        "kind": "stale_timeline",
                        "severity": "low",
                        "detail": (
                            f"最近公开事件 {latest_event['event_date']} 距今 {age_days} 天，"
                            f"超过 {STALE_TIMELINE_DAYS} 天阈值"
                        ),
                        "source_ref": latest_event.get("source_ref"),
                    }
                )
        else:
            gaps.append(
                {
                    "record_ref": ref,
                    "kind": "no_timeline",
                    "severity": "low",
                    "detail": "没有任何公开时间线事件",
                }
            )
    return gaps


def analyze(
    scope: Mapping[str, Any],
    items: Sequence[CapturedItem],
    *,
    membership_ids: Sequence[int],
) -> dict[str, Any]:
    """对冻结的快照输入执行确定性分析。"""

    if not items:
        raise ValueError("快照输入为空，无法形成赛道结论")
    raw_universe = scope.get("indication_universe")
    if raw_universe:
        indication_universe = [normalize_label(value) for value in raw_universe]
    else:
        indication_universe = sorted(
            {
                normalize_label(indication)
                for item in items
                for indication in item.content["indications"]
            }
        )
    # 把归一化宇宙还原成展示名：优先使用快照范围声明的原始写法，
    # 其次使用记录中首次出现的写法。
    display_names: dict[str, str] = {}
    if raw_universe:
        for indication in raw_universe:
            display_names.setdefault(normalize_label(indication), indication)
    for item in items:
        for indication in item.content["indications"]:
            display_names.setdefault(normalize_label(indication), indication)
    universe_display = [display_names.get(key, key) for key in indication_universe]

    tracks = _tracks(items)
    white_space = _clinical_white_space(items, universe_display)
    gaps = _evidence_gaps(items, scope["as_of"])
    versions = {
        "as_of": scope["as_of"],
        "algorithm_version": TRACK_ANALYSIS_VERSION,
        "records": [
            {
                "record_id": item.record_id,
                "version": item.version,
                "content_sha256": item.content_sha256,
                "frozen_asset_id": item.asset_id,
                "membership_id": item.membership_id,
                "credibility_annotation_id": (
                    None if item.credibility is None else item.credibility["annotation_id"]
                ),
            }
            for item in sorted(items, key=lambda member: (member.record_id, member.version))
        ],
        "membership_ids": sorted(membership_ids),
    }
    return {
        "algorithm_version": TRACK_ANALYSIS_VERSION,
        "tracks": tracks,
        "clinical_white_space": white_space,
        "evidence_gaps": gaps,
        "versions": versions,
    }
