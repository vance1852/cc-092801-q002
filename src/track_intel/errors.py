"""赛道比较服务向 API 和 CLI 暴露的稳定错误。"""


class TrackIntelError(RuntimeError):
    code = "track_intel_error"
    status = 400


class NotFound(TrackIntelError):
    code = "not_found"
    status = 404


class Conflict(TrackIntelError):
    code = "conflict"
    status = 409


class Forbidden(TrackIntelError):
    code = "forbidden"
    status = 403


class InvalidState(TrackIntelError):
    code = "invalid_state"
    status = 409


class ValidationFailed(TrackIntelError):
    code = "validation_failed"
    status = 422
