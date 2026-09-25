"""The HTTP surface: routing, status mapping and the health endpoint."""

from __future__ import annotations

import json
import threading
import urllib.request

from line_control.console.server import LineConsole

from tests.support import feed_ticket


def test_health_endpoint_reports_ok(router) -> None:
    status, payload = router.handle("GET", "/api/health")

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["records"] == 0


def test_health_alias_answers_the_same_way(router) -> None:
    assert router.handle("GET", "/healthz")[1]["status"] == "ok"


def test_unknown_route_returns_not_found(router) -> None:
    status, payload = router.handle("GET", "/api/nowhere")

    assert status == 404
    assert payload["code"] == "unknown_route"


def test_missing_field_returns_a_validation_error(router) -> None:
    status, payload = router.handle("POST", "/api/feed/close", {}, {})

    assert status == 400
    assert payload["code"] == "validation"


def test_limit_violation_maps_to_a_conflict_status(router, unit) -> None:
    status, payload = router.handle(
        "POST", "/api/feed/position", {}, {"unit": unit, "position": 900}
    )

    assert status == 409
    assert payload["code"] == "limit_violation"
    assert payload["context"]["high"] == 100


def test_gate_blocked_maps_to_a_conflict_status(router, unit) -> None:
    status, payload = router.handle("POST", "/api/feed/open", {}, {"unit": unit, "ticket": "cfm-9"})

    assert status == 409
    assert payload["code"] == "gate_blocked"


def test_unknown_unit_maps_to_not_found(router) -> None:
    status, payload = router.handle("GET", "/api/diagnostics", {"unit": ["ghost"]}, {})

    assert status == 404
    assert payload["code"] == "unknown_reference"


def test_stale_baseline_maps_to_a_conflict_status(router, unit) -> None:
    router.handle("POST", "/api/comp/retire", {}, {"unit": unit})

    status, payload = router.handle("GET", "/api/baselines", {"unit": [unit]}, {})

    assert status == 409
    assert payload["code"] == "stale_credential"


def test_expired_confirmation_maps_to_a_conflict_status(router, line, unit) -> None:
    ticket = feed_ticket(line, window=1)
    for index in range(5):
        line.audit.record("note", "u1", f"keep the clock moving {index}")

    status, payload = router.handle(
        "POST", "/api/confirm/consume", {}, {"ticket": ticket}
    )

    assert status == 409
    assert payload["code"] == "expired_credential"


def test_duplicate_batch_maps_to_a_conflict_status(router) -> None:
    router.handle("POST", "/api/batch/open", {}, {"batchId": "b-1", "unit": "u1"})

    status, payload = router.handle(
        "POST", "/api/batch/open", {}, {"batchId": "b-1", "unit": "u1"}
    )

    assert status == 409
    assert payload["code"] == "duplicate"


def test_unit_lifecycle_over_the_console(router) -> None:
    assert router.handle("POST", "/api/unit/register", {}, {"unit": "u1"})[0] == 201
    ticket = router.handle(
        "POST",
        "/api/confirm/issue",
        {},
        {"scope": "feed_gate:u1", "subject": "feed.open"},
    )[1]["ticket"]
    started = router.handle("POST", "/api/unit/start", {}, {"unit": "u1", "ticket": ticket})
    stopped = router.handle("POST", "/api/unit/stop", {}, {"unit": "u1"})

    assert started[0] == 200
    assert started[1]["phase"] == "running"
    assert stopped[1]["phase"] == "idle"


def test_state_endpoint_lists_every_unit(router) -> None:
    router.handle("POST", "/api/unit/register", {}, {"unit": "u1"})
    router.handle("POST", "/api/unit/register", {}, {"unit": "u2"})

    status, payload = router.handle("GET", "/api/state")

    assert status == 200
    assert [row["unit"] for row in payload] == ["u1", "u2"]
    assert router.handle("GET", "/api/state", {"unit": ["u1"]}, {})[1]["unit"] == "u1"


def test_index_lists_the_service_surface(router, unit) -> None:
    status, payload = router.handle("GET", "/")

    assert status == 200
    assert payload["service"] == "line-control"
    assert payload["gates"] == ["feed_open", "start_ready"]
    assert payload["sequences"] == ["compressor", "ignition"]
    assert payload["decisions"] == ["load_shed", "start_readiness"]


def test_meta_lists_modules_and_decision_rules(router) -> None:
    status, payload = router.handle("GET", "/api/meta")

    assert status == 200
    assert "compressor" in payload["modules"]
    assert payload["decisionRules"]["start_readiness"][0] == "ignition_latched"
    assert payload["gateRequirements"]["feed_open"] == [
        "seal_established",
        "compressor_durable",
        "feed_not_latched",
        "ignition_not_latched",
    ]


def test_route_table_is_listed_and_unique(router) -> None:
    status, payload = router.handle("GET", "/api/routes")

    assert status == 200
    keys = [(row["method"], row["path"]) for row in payload]
    assert len(keys) == len(set(keys))
    assert ("GET", "/api/health") in keys
    assert len(keys) >= 100


def test_every_get_route_answers_below_server_error(router) -> None:
    for row in router.routes():
        if row["method"] != "GET":
            continue
        status, _ = router.handle("GET", row["path"])
        assert status < 500, row["path"]


def test_every_post_route_answers_below_server_error(router) -> None:
    for row in router.routes():
        if row["method"] != "POST":
            continue
        status, _ = router.handle("POST", row["path"], {}, {})
        assert status < 500, row["path"]


def test_records_endpoints_expose_the_visible_stream(router, unit) -> None:
    status, payload = router.handle("GET", "/api/records")

    assert status == 200
    assert payload
    assert all(record["seq"] >= 1 for record in payload)
    assert router.handle("GET", "/api/records/counts")[1]["unit.register"] == 1
    assert "unit.register" in router.handle("GET", "/api/records/kinds")[1]
    assert router.handle("GET", "/api/records/export")[0] == 200


def test_record_query_parameters_narrow_the_result(router, unit) -> None:
    status, payload = router.handle(
        "GET", "/api/records", {"kind": ["unit.register"]}, {}
    )

    assert status == 200
    assert len(payload) == 1
    assert payload[0]["payload"]["unit"] == unit


def test_record_trace_and_slice_endpoints(router, unit) -> None:
    trace = router.handle("GET", "/api/records/trace", {"key": ["unit:u1:registered"]}, {})
    sliced = router.handle("GET", "/api/records/slice", {"startTick": ["0"]}, {})

    assert trace[0] == 200
    assert len(trace[1]) == 1
    assert sliced[0] == 200


def test_parameter_endpoints_round_trip(router, unit) -> None:
    router.handle("POST", "/api/params/set", {}, {"scope": "feed:u1", "name": "setpoint", "value": 50})
    status, payload = router.handle("GET", "/api/params", {"scope": ["feed:u1"]}, {})
    snapshot = router.handle("POST", "/api/params/snapshot", {}, {"scope": "feed:u1"})[1]
    bumped = router.handle("POST", "/api/params/bump", {}, {"scope": "feed:u1"})[1]
    restored = router.handle("POST", "/api/params/restore", {}, snapshot)

    assert status == 200
    assert payload["values"]["setpoint"] == 50
    assert payload["generation"] == 0
    assert bumped["generation"] == 1
    assert restored[0] == 409
    assert restored[1]["code"] == "stale_credential"


def test_bulk_parameter_endpoint_writes_one_commit(router, unit) -> None:
    status, payload = router.handle(
        "POST",
        "/api/params/set_many",
        {},
        {"scope": "feed:u1", "values": {"setpoint": 40, "valve_position": 60}},
    )

    assert status == 200
    assert payload["values"]["setpoint"] == 40
    assert payload["values"]["valve_position"] == 60


def test_confirmation_endpoints_issue_and_consume(router, line, unit) -> None:
    issued = router.handle(
        "POST",
        "/api/confirm/issue",
        {},
        {"scope": "feed_gate:u1", "subject": "feed.open"},
    )
    consumed = router.handle(
        "POST", "/api/confirm/consume", {}, {"ticket": issued[1]["ticket"]}
    )
    replayed = router.handle(
        "POST", "/api/confirm/consume", {}, {"ticket": issued[1]["ticket"]}
    )
    outstanding = router.handle("GET", "/api/confirmations", {"scope": ["feed_gate:u1"]}, {})

    assert issued[0] == 201
    assert consumed[1]["consumed_tick"] is not None
    assert replayed[1]["code"] == "duplicate"
    assert outstanding[1] == []


def test_batch_endpoints_track_open_and_closed_runs(router, unit) -> None:
    opened = router.handle(
        "POST", "/api/batch/open", {}, {"batchId": "b-7", "unit": unit, "subject": "start"}
    )
    listed = router.handle("GET", "/api/batches", {}, {})
    read = router.handle("GET", "/api/batches/read", {"batchId": ["b-7"]}, {})
    resolved = router.handle(
        "POST", "/api/batch/resolve", {}, {"batchId": "b-7", "decision": "accepted"}
    )
    abandoned = router.handle(
        "POST", "/api/batch/abandon", {}, {"batchId": "b-7", "note": "duplicate"}
    )

    assert opened[0] == 201
    assert [batch["batchId"] for batch in listed[1]["open"]] == ["b-7"]
    assert read[1]["open"] is True
    assert resolved[1]["decision"] == "accepted"
    assert abandoned[1]["code"] == "duplicate"


def test_trail_endpoints_record_and_describe(router, unit) -> None:
    router.handle(
        "POST", "/api/trail/record", {}, {"kind": "note", "unit": unit, "detail": "checked"}
    )
    summary = router.handle("GET", "/api/trail/summary", {}, {})
    described = router.handle("POST", "/api/trail/describe", {}, {"index": 0})
    windowed = router.handle("GET", "/api/trail/window", {"startTick": ["0"]}, {})
    latest = router.handle("GET", "/api/trail/latest", {"kind": ["note"]}, {})

    assert summary[1]["perUnit"][unit] == 2
    assert "unit" in described[1]["line"]
    assert len(windowed[1]) == 2
    assert latest[1]["found"] is True
    assert router.handle("GET", "/api/trail", {"unit": [unit]}, {})[1]


def test_exhaust_and_drive_endpoints_report_their_modules(router, unit) -> None:
    verdict = router.handle("GET", "/api/comb/verdict", {"unit": [unit]}, {})
    margin = router.handle("GET", "/api/bleed/margin", {"unit": [unit], "load": ["10"]}, {})
    compressor = router.handle("GET", "/api/comp/state", {"unit": [unit]}, {})
    decision = router.handle("GET", "/api/decisions/load", {"unit": [unit]}, {})

    assert verdict[1]["within"] is True
    assert margin[1]["guard"] >= 0.0
    assert compressor[1]["stage"] == "idle"
    assert decision[1]["table"] == "load_shed"


def test_real_http_server_answers_the_health_check(line) -> None:
    console = LineConsole(line, port=0)
    httpd = console.server()
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health") as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert payload["status"] == "ok"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
