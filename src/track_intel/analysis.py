"""赛道拥挤度、临床差异与证据缺口的确定性、可解释计算。

所有函数都是纯函数：同一份输入清单（manifest）永远得到同一份结果，
使快照可以在任意时间重放校验，结论可追溯到具体算法版本与输入版本。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping

ALGORITHM_VERSION = "track-compare/1"

PHASE_ORDER = ("discovery", "preclinical", "phase1", "phase2", "phase3", "nda", "approved")
PHASE_WEIGHTS = {
    "discovery": 1,
    "preclinical": 2,
    "phase1": 3,
    "phase2": 4,
    "phase3": 5,
    "nda": 6,
    "approved": 6,
}
SPARSE_BELOW = 8
MODERATE_BELOW = 20
STALE_DAYS = 365
CONTESTED_EXTERNAL_PROGRAMS = 2
CROWDED_FOLLOW_EXTERNALS = 4


def derive_groups(link_events: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """按顺序重放合并/拆分事件，得到当前记录到合并组的归属。"""

    membership: dict[str, str] = {}
    for event in link_events:
        record_ids = event["record_ids"]
        if event["action"] == "merge":
            for record_id in record_ids:
                membership[record_id] = event["group_id"]
        elif event["action"] == "split":
            for record_id in record_ids:
                membership.pop(record_id, None)
        else:
            raise ValueError(f"未知关联事件类型: {event['action']}")
    return membership


def _phase_rank(phase: str) -> int:
    return PHASE_ORDER.index(phase)


def _build_programs(
    records: list[Mapping[str, Any]], groups: Mapping[str, str]
) -> list[dict[str, Any]]:
    """把记录按合并组折叠为竞争项目；同源记录只计算一次。"""

    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        key = groups.get(record["record_id"], f"single:{record['record_id']}")
        buckets.setdefault(key, []).append(record)
    ordered = sorted(buckets.values(), key=lambda members: min(item["record_id"] for item in members))
    programs: list[dict[str, Any]] = []
    for index, members in enumerate(ordered, start=1):
        phase = max((item["phase"] for item in members), key=_phase_rank)
        features = sorted({feature for item in members for feature in item["clinical_features"]})
        programs.append({
            "program_key": f"program-{index:02d}",
            "member_record_ids": sorted(item["record_id"] for item in members),
            "source_kind": "internal" if any(item["source_kind"] == "internal" for item in members) else "external",
            "phase": phase,
            "clinical_features": features,
            "has_key_experiment": any(item["key_experiment"] for item in members),
            "merged": len(members) > 1,
        })
    return programs


def _crowding(programs: list[Mapping[str, Any]]) -> dict[str, Any]:
    """按开发阶段加权的拥挤度；每个因子都可单独解释。"""

    factors: list[dict[str, Any]] = []
    score = 0
    for phase in PHASE_ORDER:
        count = sum(1 for program in programs if program["phase"] == phase)
        if not count:
            continue
        contribution = count * PHASE_WEIGHTS[phase]
        score += contribution
        factors.append({
            "phase": phase,
            "programs": count,
            "weight": PHASE_WEIGHTS[phase],
            "contribution": contribution,
        })
    if score < SPARSE_BELOW:
        band = "sparse"
    elif score < MODERATE_BELOW:
        band = "moderate"
    else:
        band = "crowded"
    return {
        "score": score,
        "band": band,
        "factors": factors,
        "program_count": len(programs),
        "internal_programs": sum(1 for program in programs if program["source_kind"] == "internal"),
        "external_programs": sum(1 for program in programs if program["source_kind"] == "external"),
    }


def _follow_risk(programs: list[Mapping[str, Any]]) -> dict[str, Any]:
    """判断内部项目是在形成差异化优势，还是进入拥挤的跟随赛道。"""

    internal = [program for program in programs if program["source_kind"] == "internal"]
    external = [program for program in programs if program["source_kind"] == "external"]
    if not internal:
        return {
            "signal": "no_internal_program",
            "most_advanced_internal_phase": None,
            "external_ahead_or_equal": len(external),
            "rationale": "赛道内没有内部管线，无法评估跟随风险",
        }
    best = max(_phase_rank(program["phase"]) for program in internal)
    ahead = sum(1 for program in external if _phase_rank(program["phase"]) >= best)
    if ahead <= 1:
        signal = "differentiated"
        rationale = f"仅 {ahead} 个外部项目不落后于内部最高阶段，差异化窗口仍然开放"
    elif ahead < CROWDED_FOLLOW_EXTERNALS:
        signal = "contested"
        rationale = f"{ahead} 个外部项目不落后于内部最高阶段，赛道开始拥挤"
    else:
        signal = "crowded_follow"
        rationale = f"{ahead} 个外部项目不落后于内部最高阶段，已进入拥挤跟随赛道"
    return {
        "signal": signal,
        "most_advanced_internal_phase": PHASE_ORDER[best],
        "external_ahead_or_equal": ahead,
        "rationale": rationale,
    }


def _differentiation(programs: list[Mapping[str, Any]]) -> dict[str, Any]:
    """内部项目尚未被外部覆盖的临床差异，以及已被多方争夺的差异。"""

    internal = [program for program in programs if program["source_kind"] == "internal"]
    external = [program for program in programs if program["source_kind"] == "external"]
    internal_features = {feature for program in internal for feature in program["clinical_features"]}
    external_features = {feature for program in external for feature in program["clinical_features"]}
    uncovered = [
        {
            "feature": feature,
            "internal_programs": sum(1 for program in internal if feature in program["clinical_features"]),
        }
        for feature in sorted(internal_features - external_features)
    ]
    contested = [
        {
            "feature": feature,
            "external_programs": sum(1 for program in external if feature in program["clinical_features"]),
        }
        for feature in sorted(internal_features & external_features)
        if sum(1 for program in external if feature in program["clinical_features"]) >= CONTESTED_EXTERNAL_PROGRAMS
    ]
    return {"uncovered": uncovered, "contested": contested}


def _evidence_gaps(
    programs: list[Mapping[str, Any]],
    timeline: Mapping[str, list[Mapping[str, Any]]],
    record_credibility: Mapping[str, str],
    as_of: date,
) -> list[dict[str, Any]]:
    """逐条列出证据缺口；每条缺口都带规则名，便于追溯与复核。"""

    gaps: list[dict[str, Any]] = []
    for program in programs:
        member_ids = program["member_record_ids"]
        events = [event for record_id in member_ids for event in timeline.get(record_id, [])]
        if program["source_kind"] == "internal" and not program["has_key_experiment"]:
            gaps.append({
                "rule": "missing_key_experiment",
                "program_key": program["program_key"],
                "record_id": None,
                "detail": "内部项目缺少关键实验版本",
            })
        if not events:
            gaps.append({
                "rule": "missing_public_timeline",
                "program_key": program["program_key"],
                "record_id": None,
                "detail": "没有任何公开时间线事件",
            })
        else:
            last = max(event["event_date"] for event in events)
            if (as_of - date.fromisoformat(last)).days > STALE_DAYS:
                gaps.append({
                    "rule": "stale_public_signal",
                    "program_key": program["program_key"],
                    "record_id": None,
                    "detail": f"最近一次公开事件 {last} 已超过 {STALE_DAYS} 天",
                })
            low = sum(1 for event in events if event["credibility"] == "low")
            unrated = sum(1 for event in events if event["credibility"] == "unrated")
            if low:
                gaps.append({
                    "rule": "low_credibility_timeline",
                    "program_key": program["program_key"],
                    "record_id": None,
                    "detail": f"{low} 条时间线证据被标注为低可信",
                })
            if unrated:
                gaps.append({
                    "rule": "unrated_timeline",
                    "program_key": program["program_key"],
                    "record_id": None,
                    "detail": f"{unrated} 条时间线证据尚未标注可信度",
                })
        for record_id in member_ids:
            level = record_credibility.get(record_id, "unrated")
            if level == "low":
                gaps.append({
                    "rule": "low_credibility_record",
                    "program_key": program["program_key"],
                    "record_id": record_id,
                    "detail": "记录被标注为低可信",
                })
            elif level == "unrated":
                gaps.append({
                    "rule": "unrated_record",
                    "program_key": program["program_key"],
                    "record_id": record_id,
                    "detail": "记录尚未标注可信度",
                })
    return gaps


def compare(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """对一份赛道输入清单执行完整比较，返回可解释结果。"""

    records = list(manifest["records"])
    groups = manifest["link_groups"]
    timeline = manifest["timeline"]
    record_credibility = manifest["record_credibility"]
    as_of = date.fromisoformat(manifest["as_of"])
    programs = _build_programs(records, groups)
    crowding = _crowding(programs)
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "track": manifest["track"],
        "as_of": manifest["as_of"],
        "programs": programs,
        "crowding": crowding,
        "follow_risk": _follow_risk(programs),
        "differentiation": _differentiation(programs),
        "evidence_gaps": _evidence_gaps(programs, timeline, record_credibility, as_of),
        "provenance": {
            "algorithm_version": ALGORITHM_VERSION,
            "as_of": manifest["as_of"],
            "record_revisions": {record["record_id"]: record["revision"] for record in records},
            "program_count": len(programs),
            "phase_weights": dict(PHASE_WEIGHTS),
            "crowding_bands": {"sparse_below": SPARSE_BELOW, "moderate_below": MODERATE_BELOW},
            "stale_days": STALE_DAYS,
        },
    }
