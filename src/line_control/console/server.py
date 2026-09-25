"""HTTP wrapper around the router."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from line_control.console.handlers import Handlers
from line_control.console.router import Router
from line_control.line.supervisor import LineSupervisor


def build_router(supervisor: LineSupervisor) -> Router:
    """Bind every console route to one supervisor."""
    handlers = Handlers(supervisor)
    router = Router()
    router.add("GET", "/", handlers.index)
    router.add("GET", "/healthz", handlers.health)
    router.add("GET", "/api/health", handlers.health)
    router.add("GET", "/api/meta", handlers.meta)
    router.add("GET", "/api/state", handlers.state)
    router.add("GET", "/api/summary", handlers.summary)
    router.add("GET", "/api/state/snapshot", handlers.snapshot)
    router.add("GET", "/api/diagnostics", handlers.diagnostics)
    router.add("GET", "/api/gates", handlers.gates)
    router.add("GET", "/api/readiness", handlers.readiness)
    router.add("GET", "/api/decisions/load", handlers.load_decision)
    router.add("GET", "/api/records", handlers.records)
    router.add("GET", "/api/records/kinds", handlers.record_kinds)
    router.add("GET", "/api/records/counts", handlers.record_counts)
    router.add("GET", "/api/records/slice", handlers.record_slice)
    router.add("GET", "/api/records/trace", handlers.record_trace)
    router.add("GET", "/api/records/export", handlers.record_export)
    router.add("GET", "/api/trail", handlers.trail)
    router.add("GET", "/api/trail/latest", handlers.trail_latest)
    router.add("GET", "/api/trail/summary", handlers.trail_summary)
    router.add("GET", "/api/trail/window", handlers.trail_window)
    router.add("GET", "/api/params", handlers.parameters)
    router.add("POST", "/api/params/set", handlers.parameter_set)
    router.add("POST", "/api/params/set_many", handlers.parameter_set_many)
    router.add("POST", "/api/params/snapshot", handlers.parameter_snapshot)
    router.add("POST", "/api/params/restore", handlers.parameter_restore)
    router.add("POST", "/api/params/bump", handlers.parameter_bump)
    router.add("GET", "/api/confirmations", handlers.confirmations)
    router.add("POST", "/api/confirm/issue", handlers.confirmation_issue)
    router.add("POST", "/api/confirm/consume", handlers.confirmation_consume)
    router.add("POST", "/api/confirm/reissue", handlers.confirmation_reissue)
    router.add("GET", "/api/baselines", handlers.baselines)
    router.add("GET", "/api/batches", handlers.batches)
    router.add("GET", "/api/batches/read", handlers.batch_read)
    router.add("POST", "/api/batch/open", handlers.batch_open)
    router.add("POST", "/api/batch/resolve", handlers.batch_resolve)
    router.add("POST", "/api/batch/abandon", handlers.batch_abandon)
    router.add("POST", "/api/unit/register", handlers.unit_register)
    router.add("POST", "/api/unit/unregister", handlers.unit_unregister)
    router.add("POST", "/api/unit/start", handlers.unit_start)
    router.add("POST", "/api/unit/stop", handlers.unit_stop)
    router.add("POST", "/api/unit/trip", handlers.unit_trip)
    router.add("POST", "/api/unit/latch/release", handlers.unit_latch_release)
    router.add("POST", "/api/seal/establish", handlers.seal_establish)
    router.add("POST", "/api/seal/relieve", handlers.seal_relieve)
    router.add("POST", "/api/seal/trim", handlers.seal_trim)
    router.add("POST", "/api/feed/open", handlers.feed_open)
    router.add("POST", "/api/feed/close", handlers.feed_close)
    router.add("POST", "/api/feed/position", handlers.feed_position)
    router.add("POST", "/api/feed/setpoint", handlers.feed_setpoint)
    router.add("POST", "/api/feed/arbitrate", handlers.feed_arbitrate)
    router.add("POST", "/api/feed/limits", handlers.feed_limits)
    router.add("POST", "/api/feed/latch", handlers.feed_latch)
    router.add("POST", "/api/feed/unlatch", handlers.feed_unlatch)
    router.add("POST", "/api/feed/retry", handlers.feed_retry)
    router.add("GET", "/api/feed/retry", handlers.feed_retry_count)
    router.add("POST", "/api/bleed/valve", handlers.bleed_valve)
    router.add("POST", "/api/bleed/open", handlers.bleed_open)
    router.add("POST", "/api/bleed/close", handlers.bleed_close)
    router.add("POST", "/api/bleed/follow", handlers.bleed_follow)
    router.add("GET", "/api/bleed/margin", handlers.bleed_margin)
    router.add("POST", "/api/comp/persist", handlers.compressor_persist)
    router.add("POST", "/api/comp/advance", handlers.compressor_advance)
    router.add("POST", "/api/comp/reset", handlers.compressor_reset)
    router.add("POST", "/api/comp/recalibrate", handlers.compressor_recalibrate)
    router.add("POST", "/api/comp/retire", handlers.compressor_retire)
    router.add("POST", "/api/comp/maxload", handlers.compressor_maxload)
    router.add("POST", "/api/comp/margin", handlers.compressor_margin)
    router.add("POST", "/api/comp/surge", handlers.compressor_surge)
    router.add("GET", "/api/comp/state", handlers.compressor_state)
    router.add("POST", "/api/ign/crank", handlers.ignition_crank)
    router.add("POST", "/api/ign/spark", handlers.ignition_spark)
    router.add("POST", "/api/ign/fire", handlers.ignition_fire)
    router.add("POST", "/api/ign/flame", handlers.ignition_flame)
    router.add("POST", "/api/ign/verify", handlers.ignition_verify)
    router.add("POST", "/api/ign/start", handlers.ignition_start)
    router.add("POST", "/api/ign/fail", handlers.ignition_fail)
    router.add("POST", "/api/ign/reset", handlers.ignition_reset)
    router.add("POST", "/api/ign/window", handlers.ignition_window)
    router.add("POST", "/api/ign/attempts", handlers.ignition_attempts)
    router.add("POST", "/api/lube/prelube", handlers.lube_prelube)
    router.add("POST", "/api/lube/establish", handlers.lube_establish)
    router.add("POST", "/api/lube/tank", handlers.lube_tank)
    router.add("POST", "/api/lube/oiltemp", handlers.lube_oiltemp)
    router.add("POST", "/api/lube/stop", handlers.lube_stop)
    router.add("POST", "/api/comb/sample", handlers.exhaust_sample)
    router.add("POST", "/api/comb/protect", handlers.exhaust_protect)
    router.add("POST", "/api/comb/average", handlers.exhaust_average)
    router.add("POST", "/api/comb/sensors", handlers.exhaust_sensors)
    router.add("POST", "/api/comb/limit", handlers.exhaust_limit)
    router.add("POST", "/api/comb/reset", handlers.exhaust_reset)
    router.add("POST", "/api/comb/reload", handlers.exhaust_reload)
    router.add("POST", "/api/comb/channel/reset", handlers.exhaust_reset_channel)
    router.add("GET", "/api/comb/verdict", handlers.exhaust_verdict)
    router.add("POST", "/api/gen/speed", handlers.drive_speed)
    router.add("POST", "/api/gen/load", handlers.drive_load)
    router.add("POST", "/api/gen/coast", handlers.drive_coast)
    router.add("POST", "/api/gen/sync", handlers.drive_sync)
    router.add("POST", "/api/gen/trip", handlers.drive_trip)
    router.add("POST", "/api/gen/grid", handlers.drive_grid)
    router.add("POST", "/api/gen/voltage", handlers.drive_voltage)
    router.add("POST", "/api/gen/limits", handlers.drive_limits)
    router.add("POST", "/api/trail/record", handlers.trail_record)
    router.add("POST", "/api/trail/describe", handlers.trail_describe)
    router.add("GET", "/api/routes", lambda query, body: (200, router.routes()))
    return router


class _RequestHandler(BaseHTTPRequestHandler):
    """Adapts one HTTP request onto the router."""

    router: Router
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - name fixed by the base class
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - name fixed by the base class
        self._dispatch("POST")

    def log_message(self, format: str, *args: Any) -> None:
        """Stay quiet so the service output stays deterministic."""

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError as malformed:
            self._send(400, {"error": str(malformed), "code": "validation"})
            return
        if not isinstance(body, dict):
            self._send(400, {"error": "the request body must be an object", "code": "validation"})
            return
        status, payload = self.router.handle(method, parsed.path, query, body)
        self._send(status, payload)

    def _send(self, status: int, payload: Any) -> None:
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class LineConsole:
    """Binds a supervisor to an HTTP listener."""

    def __init__(self, supervisor: LineSupervisor, host: str = "127.0.0.1", port: int = 8080):
        self._supervisor = supervisor
        self._host = host
        self._port = int(port)
        self._router = build_router(supervisor)

    @property
    def router(self) -> Router:
        """Return the router this console serves."""
        return self._router

    @property
    def supervisor(self) -> LineSupervisor:
        """Return the supervisor behind this console."""
        return self._supervisor

    def server(self) -> ThreadingHTTPServer:
        """Create a listening server without starting it."""
        handler = type("_BoundHandler", (_RequestHandler,), {"router": self._router})
        return ThreadingHTTPServer((self._host, self._port), handler)

    def serve(self) -> None:
        """Serve until interrupted."""
        httpd = self.server()
        bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]
        print(f"line-control listening on {bound_host}:{bound_port}", flush=True)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
