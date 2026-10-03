"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .renewal import RenewalService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          renewal: RenewalService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        if renewal is not None:
            result = _renewal_route(renewal, method, parsed, body, actor_id)
            if result is not None:
                return result
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        payload = {"error": exc.code, "message": str(exc)}
        payload.update(getattr(exc, "extra", None) or {})
        return exc.status, payload
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _created(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return (200 if payload.get("replayed") else 201), payload


def _renewal_route(renewal: RenewalService, method: str, parsed, body: dict[str, Any],
                   actor_id: str) -> tuple[int, dict[str, Any]] | None:
    """分派设施更新窗口编排平台的接口。"""

    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments or segments[0] != "renewal":
        return None
    rest = segments[1:]
    query = parse_qs(parsed.query)

    if method == "POST" and rest == ["facilities"]:
        return _created(renewal.register_facility(actor_id=actor_id, **body))
    if method == "POST" and rest == ["routes"]:
        return _created(renewal.register_route(actor_id=actor_id, **body))
    if method == "POST" and rest == ["crews"]:
        return _created(renewal.register_crew(actor_id=actor_id, **body))
    if method == "POST" and rest == ["funds"]:
        return _created(renewal.register_fund(actor_id=actor_id, **body))
    if method == "POST" and rest == ["materials"]:
        return _created(renewal.register_material(actor_id=actor_id, **body))
    if method == "GET" and rest == ["calendar"]:
        site_id = query.get("site_id", [""])[0]
        if not site_id:
            raise ValidationError("site_id 不能为空")
        return 200, renewal.calendar(site_id, start=query.get("start", [None])[0],
                                     end=query.get("end", [None])[0])
    if method == "GET" and rest == ["outages"]:
        site_id = query.get("site_id", [""])[0]
        if not site_id:
            raise ValidationError("site_id 不能为空")
        return 200, {"items": renewal.list_outages(site_id)}
    if method == "GET" and rest == ["payments"]:
        site_id = query.get("site_id", [""])[0]
        if not site_id:
            raise ValidationError("site_id 不能为空")
        return 200, {"items": renewal.list_payments(site_id)}
    if rest and rest[0] == "plans":
        if method == "POST" and len(rest) == 1:
            return _created(renewal.create_plan(actor_id=actor_id, **body))
        if method == "GET" and len(rest) == 1:
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": renewal.list_plans(site_id, query.get("status", [None])[0])}
        if len(rest) >= 2:
            plan_id = rest[1]
            if method == "GET" and len(rest) == 2:
                return 200, renewal.get_plan(plan_id)
            if method == "POST" and len(rest) == 3 and rest[2] == "approvals":
                return 200, renewal.approve_plan(actor_id=actor_id, plan_id=plan_id, **body)
            if method == "POST" and len(rest) == 3 and rest[2] == "lock":
                return 200, renewal.lock_plan(actor_id=actor_id, plan_id=plan_id, **body)
            if method == "POST" and len(rest) == 3 and rest[2] == "adjustments":
                return 200, renewal.adjust_plan(actor_id=actor_id, plan_id=plan_id, **body)
            if method == "POST" and len(rest) == 3 and rest[2] == "reopen":
                return 200, renewal.reopen_plan(actor_id=actor_id, plan_id=plan_id, **body)
            if method == "GET" and len(rest) == 3 and rest[2] == "replay":
                return 200, renewal.replay_plan(plan_id)
            if method == "POST" and len(rest) == 5 and rest[2] == "phases":
                phase_id, action = rest[3], rest[4]
                if action == "start":
                    return 200, renewal.start_phase(actor_id=actor_id, plan_id=plan_id,
                                                    phase_id=phase_id, **body)
                if action == "complete":
                    return 200, renewal.complete_phase(actor_id=actor_id, plan_id=plan_id,
                                                       phase_id=phase_id, **body)
                if action == "accept":
                    return 200, renewal.accept_phase(actor_id=actor_id, plan_id=plan_id,
                                                     phase_id=phase_id, **body)
                if action == "reject":
                    return 200, renewal.reject_phase(actor_id=actor_id, plan_id=plan_id,
                                                     phase_id=phase_id, **body)
            if method == "POST" and len(rest) == 5 and rest[2] == "leases" and rest[4] == "release":
                return 200, renewal.release_lease(actor_id=actor_id, plan_id=plan_id,
                                                  lease_id=rest[3], **body)
    return None


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    renewal: RenewalService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                renewal=self.renewal)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    Handler.renewal = RenewalService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
