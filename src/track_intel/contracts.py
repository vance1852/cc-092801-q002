"""赛道比较服务的严格输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence


class ValidationError(ValueError):
    """输入不能满足领域契约。"""


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

PHASES = ("discovery", "preclinical", "phase1", "phase2", "phase3", "nda", "approved")
SOURCE_KINDS = ("internal", "external")
SENSITIVITY_LEVELS = ("open", "restricted", "confidential")
CREDIBILITY_LEVELS = ("high", "medium", "low")
TIMELINE_KINDS = ("publication", "trial_registry", "conference", "press_release", "regulatory", "patent")
DECISION_TYPES = ("advance", "hold", "decline", "watch")
SUBJECT_TYPES = ("record", "timeline_event")
MAX_CLINICAL_FEATURES = 32


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} 必须是对象")
    return value


def _require_sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{path} 必须是数组")
    return value


def _required_text(value: object, path: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} 必须是非空字符串")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationError(f"{path} 不能超过 {maximum} 个字符")
    return result


def _optional_text(value: object, path: str, maximum: int = 256) -> str | None:
    if value is None:
        return None
    return _required_text(value, path, maximum)


def _identifier(value: object, path: str) -> str:
    result = _required_text(value, path, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationError(f"{path} 格式不正确")
    return result


def _choice(value: object, path: str, allowed: Sequence[str]) -> str:
    result = _required_text(value, path, 64).lower()
    if result not in allowed:
        raise ValidationError(f"{path} 必须是 {'、'.join(allowed)} 之一")
    return result


def _date_text(value: object, path: str) -> str:
    result = _required_text(value, path, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationError(f"{path} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class KeyExperiment:
    """管线记录引用的关键实验版本。"""

    experiment_ref: str
    experiment_version: str

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "KeyExperiment":
        data = _require_mapping(raw, path)
        return cls(
            experiment_ref=_required_text(data.get("experiment_ref"), f"{path}.experiment_ref", 128),
            experiment_version=_required_text(data.get("experiment_version"), f"{path}.experiment_version", 128),
        )

    def as_dict(self) -> dict[str, str]:
        return {"experiment_ref": self.experiment_ref, "experiment_version": self.experiment_version}


@dataclass(frozen=True, slots=True)
class PipelineRecord:
    """一条内部或外部管线竞争记录。"""

    record_id: str
    source_kind: str
    target: str
    mechanism: str
    indication: str
    phase: str
    sponsor: str
    sensitivity: str
    clinical_features: tuple[str, ...]
    key_experiment: KeyExperiment | None
    first_public_date: str | None
    note: str | None

    @classmethod
    def from_dict(cls, raw: object) -> "PipelineRecord":
        data = _require_mapping(raw, "record")
        features_raw = _require_sequence(data.get("clinical_features", []), "record.clinical_features")
        features = tuple(sorted({
            _required_text(item, "record.clinical_features[]", 64).lower() for item in features_raw
        }))
        if len(features) > MAX_CLINICAL_FEATURES:
            raise ValidationError(f"record.clinical_features 不能超过 {MAX_CLINICAL_FEATURES} 项")
        key_experiment_raw = data.get("key_experiment")
        first_public_raw = data.get("first_public_date")
        return cls(
            record_id=_identifier(data.get("record_id"), "record.record_id"),
            source_kind=_choice(data.get("source_kind"), "record.source_kind", SOURCE_KINDS),
            target=_required_text(data.get("target"), "record.target", 128),
            mechanism=_required_text(data.get("mechanism"), "record.mechanism", 128),
            indication=_required_text(data.get("indication"), "record.indication", 128),
            phase=_choice(data.get("phase"), "record.phase", PHASES),
            sponsor=_required_text(data.get("sponsor"), "record.sponsor", 256),
            sensitivity=_choice(data.get("sensitivity"), "record.sensitivity", SENSITIVITY_LEVELS),
            clinical_features=features,
            key_experiment=None if key_experiment_raw is None else KeyExperiment.from_dict(key_experiment_raw, "record.key_experiment"),
            first_public_date=None if first_public_raw is None else _date_text(first_public_raw, "record.first_public_date"),
            note=_optional_text(data.get("note"), "record.note", 512),
        )


@dataclass(frozen=True, slots=True)
class TimelineEventInput:
    """一条公开时间线事件。"""

    event_date: str
    kind: str
    summary: str
    source_ref: str

    @classmethod
    def from_dict(cls, raw: object) -> "TimelineEventInput":
        data = _require_mapping(raw, "timeline_event")
        return cls(
            event_date=_date_text(data.get("event_date"), "timeline_event.event_date"),
            kind=_choice(data.get("kind"), "timeline_event.kind", TIMELINE_KINDS),
            summary=_required_text(data.get("summary"), "timeline_event.summary", 512),
            source_ref=_required_text(data.get("source_ref"), "timeline_event.source_ref", 256),
        )


@dataclass(frozen=True, slots=True)
class CredibilityAnnotationInput:
    """对记录或时间线事件的证据可信度标注。"""

    subject_type: str
    subject_id: str
    level: str
    rationale: str

    @classmethod
    def from_dict(cls, raw: object) -> "CredibilityAnnotationInput":
        data = _require_mapping(raw, "annotation")
        return cls(
            subject_type=_choice(data.get("subject_type"), "annotation.subject_type", SUBJECT_TYPES),
            subject_id=_required_text(data.get("subject_id"), "annotation.subject_id", 64),
            level=_choice(data.get("level"), "annotation.level", CREDIBILITY_LEVELS),
            rationale=_required_text(data.get("rationale"), "annotation.rationale", 512),
        )


@dataclass(frozen=True, slots=True)
class TrackSelector:
    """一次赛道比较的范围；未给出的维度视为通配。"""

    target: str
    mechanism: str | None
    indication: str | None

    @classmethod
    def from_dict(cls, raw: object) -> "TrackSelector":
        data = _require_mapping(raw, "track")
        mechanism = _optional_text(data.get("mechanism"), "track.mechanism", 128)
        indication = _optional_text(data.get("indication"), "track.indication", 128)
        return cls(
            target=_required_text(data.get("target"), "track.target", 128),
            mechanism=mechanism,
            indication=indication,
        )

    @property
    def track_key(self) -> str:
        return "|".join((
            self.target.lower(),
            (self.mechanism or "*").lower(),
            (self.indication or "*").lower(),
        ))

    def matches(self, record: Mapping[str, Any]) -> bool:
        if record["target"].lower() != self.target.lower():
            return False
        if self.mechanism is not None and record["mechanism"].lower() != self.mechanism.lower():
            return False
        if self.indication is not None and record["indication"].lower() != self.indication.lower():
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "mechanism": self.mechanism,
            "indication": self.indication,
            "track_key": self.track_key,
        }


@dataclass(frozen=True, slots=True)
class MergeInput:
    """同源记录合并请求。"""

    record_ids: tuple[str, ...]
    reason: str

    @classmethod
    def from_dict(cls, raw: object) -> "MergeInput":
        data = _require_mapping(raw, "merge")
        record_ids = tuple(dict.fromkeys(
            _identifier(item, "merge.record_ids[]") for item in _require_sequence(data.get("record_ids"), "merge.record_ids")
        ))
        if len(record_ids) < 2:
            raise ValidationError("merge.record_ids 至少需要两条不同记录")
        return cls(
            record_ids=record_ids,
            reason=_required_text(data.get("reason"), "merge.reason", 512),
        )


@dataclass(frozen=True, slots=True)
class DecisionInput:
    """投决会对某个快照版本形成的判断。"""

    decision: str
    rationale: str

    @classmethod
    def from_dict(cls, raw: object) -> "DecisionInput":
        data = _require_mapping(raw, "decision")
        return cls(
            decision=_choice(data.get("decision"), "decision.decision", DECISION_TYPES),
            rationale=_required_text(data.get("rationale"), "decision.rationale", 512),
        )
