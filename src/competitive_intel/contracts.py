"""赛道资产记录的严格数据契约。

一条资产记录（pipeline record）描述内部或外部一条在研管线在某个版本时点的
靶点、作用机制、适应症、开发阶段、关键实验版本与公开时间线。所有字段都经过
严格校验，保证进入快照的内容可追溯、可比较。

两个容易混淆的概念：
- ``evidence_tier``：记录自带的来源证据层级（传闻/新闻稿/会议/同行评议/监管），
  随记录版本一起不可变；
- ``credibility``：分析员在评审中对该版本证据给出的可信度判定（高/中/低），
  独立于记录存在、可修订并全程留痕。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence


class ContractError(ValueError):
    """输入不能满足领域契约。"""


# 开发阶段按推进顺序排列；索引即“阶段深度”，用于拥挤度与领先差距计算。
DEVELOPMENT_STAGES: tuple[str, ...] = (
    "target_validation",
    "lead_optimization",
    "preclinical",
    "phase_1",
    "phase_1_2",
    "phase_2",
    "phase_2_3",
    "phase_3",
    "filed",
    "approved",
)
STAGE_RANK: dict[str, int] = {stage: index for index, stage in enumerate(DEVELOPMENT_STAGES)}

# 记录自带的来源证据层级，由弱到强。
EVIDENCE_TIERS: tuple[str, ...] = (
    "rumor",
    "press_release",
    "conference",
    "peer_reviewed",
    "regulatory",
)
EVIDENCE_TIER_RANK: dict[str, int] = {tier: index for index, tier in enumerate(EVIDENCE_TIERS)}

# 资产来源：内部管线与公开/外部情报，最小披露策略对两者区别对待。
ASSET_ORIGINS: frozenset[str] = frozenset({"internal", "external"})

# 敏感级别：决定身份与实验细节按角色最小披露的遮蔽粒度。
SENSITIVITY_LEVELS: frozenset[str] = frozenset({"public", "restricted", "sensitive"})

# 分析员对一条记录版本证据的可信度判定（独立于来源证据层级）。
CREDIBILITY_LEVELS: frozenset[str] = frozenset({"high", "medium", "low"})

# 允许用于临床差异比较的结构化属性维度；取值为自由文本标签集合（归一化后比较）。
CLINICAL_ATTRIBUTE_DIMENSIONS: frozenset[str] = frozenset(
    {
        "patient_segments",     # 例如 biomarker 阳性、经治线数
        "combination",          # 单药/联用方案
        "line_of_therapy",      # 治疗线数
        "endpoints",            # 主要终点
        "biomarker_strategy",   # 伴随诊断/富集策略
        "route",                # 给药途径
    }
)

# 公开时间线允许的事件类型。
TIMELINE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "disclosed",            # 首次公开
        "preclinical_update",
        "ind_filed",
        "trial_started",
        "trial_update",
        "trial_readout",
        "regulatory_filing",
        "approval",
        "setback",
        "deal",
    }
)


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} 必须是对象")
    return value


def _required_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{path} 必须是非空字符串")
    return value.strip()


def _optional_text(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, path)


def _require_sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ContractError(f"{path} 必须是数组")
    return value


def _require_choice(value: object, path: str, choices) -> str:
    text = _required_text(value, path)
    if text not in choices:
        raise ContractError(f"{path} 必须是 {sorted(choices)} 之一")
    return text


def _validate_date(value: str, path: str) -> None:
    """接受 YYYY-MM-DD 或完整 ISO 时间，保证时间线可排序。"""

    text = value.strip()
    try:
        if len(text) == 10:
            datetime.strptime(text, "%Y-%m-%d")
        else:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{path} 必须是 YYYY-MM-DD 或 ISO 时间") from exc


def normalize_label(value: str) -> str:
    """靶点/机制/适应症/临床属性标签的归一化键，用于跨记录比较。"""

    return value.strip().lower().replace(" ", "_").replace("-", "_")


@dataclass(frozen=True, slots=True)
class ExperimentVersion:
    """一个关键实验版本及其结论，是快照结论最细粒度的证据出处。"""

    experiment_version_id: str
    experiment_type: str
    version: str
    result_summary: str
    observed_at: str

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "ExperimentVersion":
        data = _require_mapping(raw, path)
        observed_at = _required_text(data.get("observed_at"), f"{path}.observed_at")
        _validate_date(observed_at, f"{path}.observed_at")
        return cls(
            experiment_version_id=_required_text(
                data.get("experiment_version_id"), f"{path}.experiment_version_id"
            ),
            experiment_type=_required_text(data.get("experiment_type"), f"{path}.experiment_type"),
            version=_required_text(data.get("version"), f"{path}.version"),
            result_summary=_required_text(data.get("result_summary"), f"{path}.result_summary"),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """公开时间线上的一个可追溯事件。"""

    event_type: str
    event_date: str
    summary: str
    source_ref: str | None

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "TimelineEvent":
        data = _require_mapping(raw, path)
        event_date = _required_text(data.get("event_date"), f"{path}.event_date")
        _validate_date(event_date, f"{path}.event_date")
        return cls(
            event_type=_require_choice(
                data.get("event_type"), f"{path}.event_type", TIMELINE_EVENT_TYPES
            ),
            event_date=event_date,
            summary=_required_text(data.get("summary"), f"{path}.summary"),
            source_ref=_optional_text(data.get("source_ref"), f"{path}.source_ref"),
        )


@dataclass(frozen=True, slots=True)
class AssetRecordDraft:
    """一次资产记录写入（新建或修订）所携带的完整内容。"""

    source: str
    display_name: str
    organization: str
    origin: str
    sensitivity: str
    target: str
    mechanism: str
    modalities: tuple[str, ...]
    indications: tuple[str, ...]
    clinical_attributes: Mapping[str, tuple[str, ...]]
    stage: str
    experiments: tuple[ExperimentVersion, ...]
    timeline: tuple[TimelineEvent, ...]
    evidence_tier: str
    evidence_refs: tuple[str, ...]
    notes: str | None

    @classmethod
    def from_dict(cls, raw: object) -> "AssetRecordDraft":
        data = _require_mapping(raw, "asset_record")
        origin = _require_choice(data.get("origin"), "asset_record.origin", ASSET_ORIGINS)
        sensitivity = _require_choice(
            data.get("sensitivity", "public"), "asset_record.sensitivity", SENSITIVITY_LEVELS
        )
        stage = _require_choice(
            data.get("stage"), "asset_record.stage", frozenset(DEVELOPMENT_STAGES)
        )
        evidence_tier = _require_choice(
            data.get("evidence_tier"), "asset_record.evidence_tier", frozenset(EVIDENCE_TIERS)
        )
        modalities = tuple(
            _required_text(item, f"asset_record.modalities[{index}]")
            for index, item in enumerate(
                _require_sequence(data.get("modalities", []), "asset_record.modalities")
            )
        )
        indications = tuple(
            _required_text(item, f"asset_record.indications[{index}]")
            for index, item in enumerate(
                _require_sequence(data.get("indications"), "asset_record.indications")
            )
        )
        if not indications:
            raise ContractError("asset_record.indications 不能为空")
        if len(set(indications)) != len(indications):
            raise ContractError("asset_record.indications 不能重复")
        clinical_raw = _require_mapping(
            data.get("clinical_attributes", {}), "asset_record.clinical_attributes"
        )
        unknown_dims = set(clinical_raw) - CLINICAL_ATTRIBUTE_DIMENSIONS
        if unknown_dims:
            raise ContractError(
                f"asset_record.clinical_attributes 含未知维度 {sorted(unknown_dims)}"
            )
        clinical_attributes: dict[str, tuple[str, ...]] = {}
        for dimension, labels in clinical_raw.items():
            parsed = tuple(
                _required_text(label, f"asset_record.clinical_attributes.{dimension}[{index}]")
                for index, label in enumerate(_require_sequence(labels, f"asset_record.clinical_attributes.{dimension}"))
            )
            if len(set(normalize_label(label) for label in parsed)) != len(parsed):
                raise ContractError(f"asset_record.clinical_attributes.{dimension} 标签不能重复")
            clinical_attributes[dimension] = parsed
        experiments = tuple(
            ExperimentVersion.from_dict(item, f"asset_record.experiments[{index}]")
            for index, item in enumerate(
                _require_sequence(data.get("experiments", []), "asset_record.experiments")
            )
        )
        experiment_ids = [item.experiment_version_id for item in experiments]
        if len(set(experiment_ids)) != len(experiment_ids):
            raise ContractError("asset_record.experiments.experiment_version_id 不能重复")
        timeline = tuple(
            TimelineEvent.from_dict(item, f"asset_record.timeline[{index}]")
            for index, item in enumerate(
                _require_sequence(data.get("timeline", []), "asset_record.timeline")
            )
        )
        evidence_refs = tuple(
            _required_text(item, f"asset_record.evidence_refs[{index}]")
            for index, item in enumerate(
                _require_sequence(data.get("evidence_refs"), "asset_record.evidence_refs")
            )
        )
        if not evidence_refs:
            raise ContractError("asset_record.evidence_refs 不能为空")
        return cls(
            source=_required_text(data.get("source"), "asset_record.source"),
            display_name=_required_text(data.get("display_name"), "asset_record.display_name"),
            organization=_required_text(data.get("organization"), "asset_record.organization"),
            origin=origin,
            sensitivity=sensitivity,
            target=_required_text(data.get("target"), "asset_record.target"),
            mechanism=_required_text(data.get("mechanism"), "asset_record.mechanism"),
            modalities=modalities,
            indications=indications,
            clinical_attributes=clinical_attributes,
            stage=stage,
            experiments=experiments,
            timeline=timeline,
            evidence_tier=evidence_tier,
            evidence_refs=evidence_refs,
            notes=_optional_text(data.get("notes"), "asset_record.notes"),
        )

    def to_content(self) -> dict[str, Any]:
        """转换为参与内容摘要的规范化字典。"""

        return {
            "source": self.source,
            "display_name": self.display_name,
            "organization": self.organization,
            "origin": self.origin,
            "sensitivity": self.sensitivity,
            "target": self.target,
            "mechanism": self.mechanism,
            "modalities": list(self.modalities),
            "indications": list(self.indications),
            "clinical_attributes": {
                dimension: list(labels)
                for dimension, labels in sorted(self.clinical_attributes.items())
            },
            "stage": self.stage,
            "experiments": [
                {
                    "experiment_version_id": item.experiment_version_id,
                    "experiment_type": item.experiment_type,
                    "version": item.version,
                    "result_summary": item.result_summary,
                    "observed_at": item.observed_at,
                }
                for item in self.experiments
            ],
            "timeline": [
                {
                    "event_type": item.event_type,
                    "event_date": item.event_date,
                    "summary": item.summary,
                    "source_ref": item.source_ref,
                }
                for item in self.timeline
            ],
            "evidence_tier": self.evidence_tier,
            "evidence_refs": list(self.evidence_refs),
            "notes": self.notes,
        }
