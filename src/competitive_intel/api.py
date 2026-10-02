"""无第三方依赖的赛道竞争快照 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ServiceError, ValidationFailed
from .service import CompetitiveIntelService
from .storage import connect


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(self, service: CompetitiveIntelService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ):
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                from .storage import inspect_schema

                return _Response(200, {"status": "ok", "schema_version": inspect_schema(self.service.connection)["schema_version"]})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}

            if method == "POST" and path == "/users":
                result = self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                )
                return _Response(201, result)

            if method == "POST" and path == "/records":
                result = self.service.register_record(
                    self._actor(normalized_headers), payload["record_id"], payload
                )
                return _Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "records" and parts[2] == "revisions":
                result = self.service.revise_record(
                    self._actor(normalized_headers), parts[1], payload
                )
                return _Response(201, result)
            if method == "GET" and len(parts) >= 2 and parts[0] == "records":
                version = None
                if len(parts) == 4 and parts[2] == "versions":
                    version = int(parts[3])
                result = self.service.get_record(self._actor(normalized_headers), parts[1], version)
                return _Response(200, result)
            if method == "POST" and path == "/credibility_annotations":
                result = self.service.annotate_credibility(
                    self._actor(normalized_headers),
                    payload["record_id"],
                    int(payload["record_version"]),
                    payload["level"],
                    payload["rationale"],
                    payload.get("evidence_refs", []),
                    payload.get("scope", "track_review"),
                )
                return _Response(201, result)

            if method == "POST" and path == "/assets":
                result = self.service.create_asset(
                    self._actor(normalized_headers),
                    payload["asset_id"],
                    payload["record_ids"],
                    payload["reason"],
                )
                return _Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "assets" and parts[2] == "merge":
                result = self.service.merge_into_asset(
                    self._actor(normalized_headers),
                    parts[1],
                    payload["record_ids"],
                    payload["reason"],
                )
                return _Response(200, result)
            if method == "POST" and path == "/assets/unmerge":
                result = self.service.unmerge_record(
                    self._actor(normalized_headers), payload["record_id"], payload["reason"]
                )
                return _Response(200, result)
            if method == "GET" and len(parts) == 2 and parts[0] == "assets":
                return _Response(200, self.service.get_asset(self._actor(normalized_headers), parts[1]))

            if method == "POST" and path == "/snapshots":
                result = self.service.create_snapshot(
                    self._actor(normalized_headers),
                    payload["snapshot_id"],
                    payload["scope"],
                    payload.get("record_ids"),
                )
                return _Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "snapshots" and parts[2] == "publish":
                result = self.service.publish_snapshot(
                    self._actor(normalized_headers), parts[1], int(payload["version"])
                )
                return _Response(200, result)
            if method == "GET" and len(parts) >= 2 and parts[0] == "snapshots":
                if len(parts) == 2:
                    result = self.service.get_snapshot(self._actor(normalized_headers), parts[1])
                elif len(parts) == 4 and parts[2] == "versions":
                    result = self.service.get_snapshot(
                        self._actor(normalized_headers), parts[1], int(parts[3])
                    )
                else:
                    return _Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
                return _Response(200, result)
            if method == "GET" and path == "/snapshots":
                return _Response(200, {"snapshots": self.service.list_snapshots(self._actor(normalized_headers))})
            if method == "POST" and path == "/judgments":
                result = self.service.record_judgment(
                    self._actor(normalized_headers),
                    payload["judgment_id"],
                    payload["snapshot_id"],
                    int(payload["snapshot_version"]),
                    payload["decision"],
                    payload["rationale"],
                )
                return _Response(201, result)
            if method == "GET" and path == "/audit_events":
                result = self.service.audit_events(self._actor(normalized_headers))
                return _Response(200, {"events": result})

            return _Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return _Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return _Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


class _Response:
    __slots__ = ("status", "body")

    def __init__(self, status: int, body: Mapping[str, Any]) -> None:
        self.status = status
        self.body = body


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CompetitiveIntel/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动靶点赛道竞争快照 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("competitive_intel.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = JsonApplication(CompetitiveIntelService(connection))
    # 单线程 HTTP 服务：单个 SQLite 连接不应跨线程共享，赛道评审是离线低并发
    # 内部工具，串行处理即可保证连接一致；并发写由 BEGIN IMMEDIATE 串行化。
    server = HTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
