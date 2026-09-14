"""Session plumbing for the TechSara OpenAI-SDK conformance suite.

Read README.md first. In one paragraph: the suite points the official
`openai` Python SDK (plus raw httpx where the SDK hides the wire) at ANY
TechSara `/v1` base URL with a test key, and reports every contract promise
as PASS, FAIL, SKIP (with the reason) or XFAIL(strict) "not built yet". Which
features count as built is read from the target's own /v1/openapi.json
(techsara_conformance/features.py), printed at the top and bottom of the run.

WHY A `-p` PLUGIN AND NOT conftest.py (2026-09-13): pytest.ini loads this
module with `-p techsara_conformance.plugin`, so the same fixtures and
options serve `tests/` while `selftest/` can run with the plugin switched off
(`-p no:techsara_conformance.plugin`) and needs no server or key.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

from techsara_conformance import config as conf
from techsara_conformance import features as feat
from techsara_conformance import pacing

_STATE: Dict[str, Any] = {"target": None, "features": None, "results": [], "openapi": None, "limits_off_expected": False}


# ----------------------------------------------------------------- options --


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("techsara", "TechSara conformance target")
    group.addoption("--base-url", default=None, help="the /v1 base URL (or origin); env TECHSARA_BASE_URL")
    group.addoption("--keys-file", default=None, help="JSON from tools/provision_key.py; env TECHSARA_KEYS_FILE")
    group.addoption(
        "--feature",
        action="append",
        default=[],
        metavar="NAME=built|planned|auto",
        help="override feature detection (repeatable); env TECHSARA_FEATURES",
    )
    group.addoption(
        "--long-output-tokens",
        type=int,
        default=None,
        help="output tokens for the timeout=None long-request tests (default 1024; env TECHSARA_LONG_OUTPUT_TOKENS)",
    )
    group.addoption(
        "--capacity-probe",
        action="store_true",
        default=False,
        help="run the tests that deliberately fill a capacity gate (sustained engine load: never on shared production)",
    )
    group.addoption("--conformance-report", default=None, help="also write the result table as JSON to this path")
    group.addoption(
        "--no-limit-wait",
        action="store_true",
        default=False,
        help="do not wait out a 429 rate_limit_error/quota_exceeded/concurrency_limit_exceeded (stacks with limits enforced)",
    )
    group.addoption(
        "--strict-scopes",
        action="store_true",
        default=False,
        help="stop with a usage error, instead of SKIP, when a BUILT feature needs a scope the key does not hold",
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--no-limit-wait", default=False):
        pacing.disable_limit_waits()


def _target(config: pytest.Config) -> conf.Target:
    if _STATE["target"] is None:
        try:
            _STATE["target"] = conf.load(config)
        except (ValueError, OSError) as exc:
            raise pytest.UsageError(str(exc)) from None
    return _STATE["target"]


def _resolve_features(config: pytest.Config) -> Dict[str, feat.Resolution]:
    if _STATE["features"] is not None:
        return _STATE["features"]
    import os

    target = _target(config)
    try:
        overrides = feat.parse_overrides([os.environ.get("TECHSARA_FEATURES", ""), *config.getoption("--feature")])
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from None
    doc: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    try:
        # Unauthenticated on purpose: CONTRACT-3 §7 lists no scope for it.
        response = httpx.get(f"{target.base_url}/openapi.json", timeout=30.0)
        if response.status_code == 200:
            doc = response.json()
        else:
            error = f"HTTP {response.status_code}"
    except (httpx.HTTPError, ValueError) as exc:
        error = type(exc).__name__
    _STATE["openapi"] = doc
    _STATE["features"] = feat.resolve(doc, error, overrides)
    return _STATE["features"]


# -------------------------------------------------------------- collection --


def pytest_collection_modifyitems(config: pytest.Config, items: List[pytest.Item]) -> None:
    resolutions = _resolve_features(config)
    target = _target(config)
    # WHY (2026-09-13, review finding): the limit-patient transport once stayed
    # on however limits_off resolved, so a server whose schema says "no limits"
    # but still answered 429 got PASS and exit 0. When the target claims limits
    # are off, the suite stops being patient and pytest_sessionfinish fails the
    # run on any RateLimit header or limit 429 it saw.
    if resolutions["limits_off"].state == feat.BUILT:
        pacing.expect_limits_off()
        _STATE["limits_off_expected"] = True
    missing_by_feature: Dict[str, List[str]] = {}
    for item in items:
        for mark in item.iter_markers("feature"):
            name = mark.args[0] if mark.args else None
            if name not in feat.FEATURES:
                raise pytest.UsageError(f"{item.nodeid}: unknown feature marker {name!r}")
            resolution = resolutions[name]
            if resolution.state == feat.PLANNED:
                item.add_marker(
                    pytest.mark.xfail(strict=True, reason=f"not built yet: {name} — {resolution.evidence}")
                )
                continue
            missing = feat.missing_scopes(name, target.scopes)
            if missing and not item.get_closest_marker("limited_key"):
                missing_by_feature[name] = missing
                item.add_marker(
                    pytest.mark.skip(
                        reason=f"key lacks {', '.join(missing)} for built feature {name} "
                        f"(scopes {sorted(target.scopes or [])} from {target.scopes_source}); CONTRACT-3 §7: a key "
                        "keeps the scopes it was minted with — run tools/provision_key.py again after a rebuild"
                    )
                )
        if item.get_closest_marker("capacity_probe") and not target.capacity_probe:
            item.add_marker(
                pytest.mark.skip(
                    reason="capacity probe not enabled: filling a gate needs sustained engine load "
                    "(pass --capacity-probe on a stack whose engines nobody else is using)"
                )
            )
    if missing_by_feature and config.getoption("--strict-scopes", default=False):
        detail = "; ".join(f"{name} needs {', '.join(m)}" for name, m in sorted(missing_by_feature.items()))
        raise pytest.UsageError(
            f"--strict-scopes: the key from {target.scopes_source} lacks scopes for built features ({detail}). "
            "Mint a new key with tools/provision_key.py."
        )


def pytest_report_header(config: pytest.Config) -> List[str]:
    try:
        target = _target(config)
        resolutions = _resolve_features(config)
    except pytest.UsageError:
        return []
    import openai

    limits = (
        "target claims limits OFF: no pacing, no 429 waits; any RateLimit header or limit 429 fails the run"
        if resolutions["limits_off"].state == feat.BUILT
        else "target documents limits: advertised budget paced, limit 429s waited out and reported"
    )
    return [
        f"techsara target: {target.base_url}  ({_sdk_versions_line()})",
        f"models: {target.models}",
        f"key scopes: {sorted(target.scopes) if target.scopes is not None else 'unknown'} (from {target.scopes_source})",
        f"limits: {limits}",
        "features (state, evidence):",
        *feat.table(resolutions),
    ]


# ---------------------------------------------------------------- results --


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: Any):
    outcome = yield
    report = outcome.get_result()
    marks = [m.args[0] for m in item.iter_markers("feature") if m.args]
    report.techsara_feature = marks[0] if marks else "core"


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    label: Optional[str] = None
    detail = ""
    if report.when == "setup" and not report.passed:
        if report.skipped:
            label = "XFAIL" if hasattr(report, "wasxfail") else "SKIP"
            detail = report.wasxfail if hasattr(report, "wasxfail") else _skip_reason(report)
        else:
            label, detail = "ERROR", _first_line(report)
    elif report.when == "call":
        if report.skipped and hasattr(report, "wasxfail"):
            label, detail = "XFAIL", _why_it_failed(report)
        elif report.skipped:
            label, detail = "SKIP", _skip_reason(report)
        elif report.failed and "XPASS(strict)" in str(report.longrepr):
            label, detail = "XPASS(strict)", str(report.longrepr)
        elif report.failed:
            label, detail = "FAIL", _first_line(report)
        else:
            label = "PASS"
    elif report.when == "teardown" and report.failed:
        label, detail = "ERROR", _first_line(report)
    if label is None:
        return
    notes = [str(value) for key, value in report.user_properties if key == "note"]
    _STATE["results"].append(
        {
            "test": report.nodeid,
            "feature": getattr(report, "techsara_feature", "core"),
            "result": label,
            "detail": detail.strip()[:400],
            "notes": notes,
            "seconds": round(report.duration, 2),
        }
    )


def _skip_reason(report: pytest.TestReport) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2]).removeprefix("Skipped: ")
    return str(longrepr)


def _first_line(report: pytest.TestReport) -> str:
    crash = getattr(report.longrepr, "reprcrash", None)
    if crash is not None:
        return str(crash.message).splitlines()[0] if crash.message else ""
    return str(report.longrepr).splitlines()[-1] if report.longrepr else ""


def _why_it_failed(report: pytest.TestReport) -> str:
    crash = getattr(report.longrepr, "reprcrash", None)
    observed = str(crash.message).splitlines()[0] if crash is not None and crash.message else ""
    return f"{report.wasxfail} | observed: {observed}" if observed else str(report.wasxfail)


def _sdk_versions() -> Dict[str, str]:
    """The client stack a result is attributable to. openai-python 3.x sends
    through httpx2 (its Requires-Dist), not httpx; httpx is only the raw client."""
    import openai

    out = {"openai": openai.__version__}
    try:
        import httpx2

        out["httpx2 (SDK transport)"] = httpx2.__version__
    except ImportError:  # pragma: no cover — an SDK older than 3.x
        pass
    out["httpx (raw calls)"] = httpx.__version__
    return out


def _sdk_versions_line() -> str:
    return ", ".join(f"{k} {v}" for k, v in _sdk_versions().items())


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """A limits-off target that showed a limit anyway fails the RUN, not just a
    line: a release gate reading the exit code or the JSON totals must see it."""
    if not _STATE["limits_off_expected"]:
        return
    violations = pacing.limits_off_violations()
    if not violations:
        return
    _STATE["results"].append(
        {
            "test": "session::limits_off_held_for_the_whole_run",
            "feature": "limits_off",
            "result": "FAIL",
            "detail": "the target's schema says limits are off (CONTRACT-3 §12.1) but this run saw "
            + "; ".join(violations),
            "notes": [],
            "seconds": 0.0,
        }
    )
    if session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    results = _STATE["results"]
    if not results:
        return
    write = terminalreporter.write_line
    terminalreporter.section("TechSara conformance")
    if _STATE["features"]:
        write("features (state, evidence):")
        for line in feat.table(_STATE["features"]):
            write(line)
        write("")
    width = max(len(r["test"].split("::", 1)[-1]) for r in results)
    fwidth = max(len(r["feature"]) for r in results)
    for r in results:
        name = r["test"].split("::", 1)[-1]
        module = Path(r["test"].split("::", 1)[0]).stem.removeprefix("test_")
        write(f"{r['result']:<13} {module:<16} {r['feature']:<{fwidth}}  {name:<{width}}")
        if r["result"] in ("FAIL", "ERROR", "XPASS(strict)"):
            write(f"{'':<13} why: {r['detail'][:300]}")
        elif r["result"] == "XFAIL" and "| observed:" in r["detail"]:
            write(f"{'':<13} observed: {r['detail'].split('| observed:', 1)[1].strip()[:300]}")
        for note in r["notes"]:
            write(f"{'':<13} note: {note}")
    counts = Counter(r["result"] for r in results)
    write("")
    write("totals: " + ", ".join(f"{k} {counts[k]}" for k in ["PASS", "FAIL", "XFAIL", "XPASS(strict)", "SKIP", "ERROR"] if counts[k]))
    waited = pacing.waited_seconds()
    if waited:
        write(f"paced: waited {waited:.0f} s for the target's advertised request budget (RateLimit header)")
    count, seconds = pacing.limit_waits()
    if count:
        write(
            f"limits: waited out {count} limit 429(s), {seconds:.0f} s in total — this target ENFORCES usage limits "
            "(CONTRACT-3 §12.1 says none by default); see the limits_off row"
        )
    path = config.getoption("--conformance-report")
    if path:
        target = _STATE["target"]
        Path(path).write_text(
            json.dumps(
                {
                    "base_url": target.base_url if target else None,
                    "sdk": _sdk_versions(),
                    "key_scopes": sorted(target.scopes) if target and target.scopes is not None else None,
                    "limits_off_expected": _STATE["limits_off_expected"],
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "features": {k: vars(v) for k, v in (_STATE["features"] or {}).items()},
                    "results": results,
                    "totals": dict(counts),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        write(f"report written to {path}")


# ---------------------------------------------------------------- fixtures --


@pytest.fixture(scope="session")
def target(pytestconfig: pytest.Config) -> conf.Target:
    return _target(pytestconfig)


@pytest.fixture(scope="session")
def features(pytestconfig: pytest.Config) -> Dict[str, feat.Resolution]:
    return _resolve_features(pytestconfig)


@pytest.fixture(scope="session")
def openapi_doc(pytestconfig: pytest.Config) -> Optional[Dict[str, Any]]:
    _resolve_features(pytestconfig)
    return _STATE["openapi"]


class AttemptLog:
    """Every HTTP exchange a client made, in order — how the retry tests see
    what the SDK really did on the wire."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.responses: List[Dict[str, Any]] = []

    def on_request(self, request: Any) -> None:
        self.requests.append(
            {"t": time.monotonic(), "method": request.method, "url": str(request.url), "headers": dict(request.headers)}
        )

    def on_response(self, response: Any) -> None:
        body: Any = None
        if response.status_code >= 400:
            # An error body is small and never a stream; reading it here is
            # the only way to see the code of an attempt the SDK retried away.
            try:
                response.read()
                body = response.json()
            except Exception:  # noqa: BLE001 — a non-JSON error is recorded as None
                body = None
        self.responses.append(
            {"t": time.monotonic(), "status": response.status_code, "headers": dict(response.headers), "body": body}
        )


def _client_factory(target: conf.Target):
    """Build `openai.OpenAI` clients for this target. Retries are OFF unless a
    test asks for them, so every other test sees the server's first answer."""
    import openai

    made: List[Any] = []

    def build(
        *,
        api_key: Optional[str] = None,
        max_retries: int = 0,
        timeout: Any = "default",
        log: Optional[AttemptLog] = None,
        wire: Any = None,
    ) -> Any:
        hooks_req: List[Callable[[Any], None]] = [pacing.on_request]
        hooks_resp: List[Callable[[Any], None]] = [pacing.on_response]
        if log is not None:
            hooks_req.append(log.on_request)
            hooks_resp.append(log.on_response)
        http_timeout = target.request_timeout_s if timeout == "default" else timeout
        import httpx2

        http_client = openai.DefaultHttpxClient(
            timeout=http_timeout,
            event_hooks={"request": hooks_req, "response": hooks_resp},
            transport=pacing.wrap(
                httpx2.HTTPTransport(), httpx2.BaseTransport, stream_base=httpx2.SyncByteStream, wire=wire
            ),
        )
        client = openai.OpenAI(
            base_url=target.base_url,
            api_key=api_key or target.api_key,
            max_retries=max_retries,
            timeout=http_timeout,
            http_client=http_client,
        )
        made.append(client)
        return client

    def close_all() -> None:
        for client in made:
            client.close()

    return build, close_all


@pytest.fixture
def make_client(target: conf.Target) -> Callable[..., Any]:
    build, close_all = _client_factory(target)
    yield build
    close_all()


@pytest.fixture(scope="module")
def make_module_client(target: conf.Target) -> Callable[..., Any]:
    build, close_all = _client_factory(target)
    yield build
    close_all()


@pytest.fixture
def client(make_client: Callable[..., Any]) -> Any:
    return make_client()


@pytest.fixture
def raw(target: conf.Target) -> httpx.Client:
    """httpx on the same base URL with the key, for what the SDK hides."""
    with httpx.Client(
        base_url=target.base_url + "/",
        headers={"Authorization": f"Bearer {target.api_key}"},
        timeout=target.request_timeout_s,
        event_hooks={"request": [pacing.on_request], "response": [pacing.on_response]},
        transport=pacing.wrap(httpx.HTTPTransport(), httpx.BaseTransport),
    ) as c:
        yield c


@pytest.fixture
def note(request: pytest.FixtureRequest) -> Callable[[str], None]:
    """Attach an observation to this test's line in the summary table."""

    def add(text: str) -> None:
        request.node.user_properties.append(("note", text))

    return add


@pytest.fixture
def attempt_log() -> AttemptLog:
    return AttemptLog()
