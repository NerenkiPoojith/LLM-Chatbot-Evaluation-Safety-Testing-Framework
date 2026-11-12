"""Shared pytest fixtures and the score-report plugin.

Fixtures marked scope="session" are created once for the whole test run:
starting the API server and loading the embedding model are slow, so every
test shares the same instance.

Set API_BASE_URL to run the API tests against a server that is already running
(for example a staging deployment) instead of starting one locally:
    $env:API_BASE_URL = "http://127.0.0.1:8000"; pytest
"""

from __future__ import annotations

import os
import tempfile

# A server started by pytest gets its own empty uploads folder and a 1 MB upload limit (so the
# "too large" test needs only 1 MB). Set before app.config is used. With API_BASE_URL these
# settings do not reach the server: the upload tests then delete only the manuals they created
# and skip the cases that need the 1 MB limit.
os.environ["UPLOADS_DIR"] = tempfile.mkdtemp(prefix="groundcheck-test-uploads-")
os.environ["MAX_UPLOAD_MB"] = "1"

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.embeddings import get_embeddings
from app.pii import load_allowlist
from evaluation.checks import CaseResult, load_sensitive_values
from evaluation.client import ApiClient
from evaluation.groundedness import build_evaluator
from evaluation.report import build_report, write_report
from evaluation.run_eval import collect_meta
from evaluation.server import running_server

RESULTS = pytest.StashKey[list]()
META = pytest.StashKey[dict]()
REPORT = pytest.StashKey[dict]()
UPLOADED = pytest.StashKey[list]()  # titles of manuals already uploaded to an external server


def pytest_configure(config: pytest.Config) -> None:
    config.stash[RESULTS] = []


# ---------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(scope="session")
def embeddings(settings):
    return get_embeddings(settings.embedding_model, str(settings.embedding_cache_dir))


@pytest.fixture(scope="session")
def api_base_url():
    external = os.getenv("API_BASE_URL")
    if external:
        yield external.rstrip("/")
        return
    with running_server() as url:
        yield url


@pytest.fixture(scope="session")
def api(api_base_url, settings, pytestconfig) -> ApiClient:
    client = ApiClient(api_base_url)
    pytestconfig.stash[META] = collect_meta(settings, client.health(), api_base_url)
    if os.getenv("API_BASE_URL"):
        # A running server may hold the user's own manuals, which join every unscoped search
        # and can change quality and PII results; say so in the summary instead of failing silently.
        pytestconfig.stash[UPLOADED] = [m["title"] for m in client.manuals() if m["uploaded"]]
    return client


@pytest.fixture(scope="session")
def health(api) -> dict:
    return api.health()


@pytest.fixture(scope="session")
def evaluator(settings, embeddings):
    return build_evaluator(settings, embeddings)


@pytest.fixture(scope="session")
def allowlist(settings):
    return load_allowlist(str(settings.pii_allowlist_path))


@pytest.fixture(scope="session")
def sensitive() -> dict:
    return load_sensitive_values()


@pytest.fixture
def record(pytestconfig):
    """Tests call record(result) so the result ends up in the score report."""

    def _record(result: CaseResult) -> None:
        pytestconfig.stash[RESULTS].append(result)

    return _record


def mark_known_issue(request: pytest.FixtureRequest, reason: str) -> None:
    """Turn a documented known issue into an xfail: reported, but not a red build."""
    if reason:
        request.node.add_marker(pytest.mark.xfail(reason=reason, strict=False))


# ---------------------------------------------------------------- reporting
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    results = session.config.stash.get(RESULTS, [])
    meta = session.config.stash.get(META, None)
    if results and meta:
        report = build_report(results, meta)
        session.config.stash[REPORT] = {"report": report, "paths": write_report(report)}


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    uploaded = config.stash.get(UPLOADED, [])
    if uploaded:
        terminalreporter.section("Warning: uploaded manuals")
        terminalreporter.write_line(
            f"{os.getenv('API_BASE_URL')} has {len(uploaded)} uploaded manual(s) ({', '.join(uploaded[:5])}). "
            "They are searched with the sample manuals, so golden and PII results include them. "
            "Run pytest without API_BASE_URL for clean scores.", yellow=True)
    stored = config.stash.get(REPORT, None)
    if not stored:
        return
    summary, paths = stored["report"]["summary"], stored["paths"]
    tr = terminalreporter
    tr.section("LLM evaluation scores")
    tr.write_line(f"cases: {summary['passed']}/{summary['total']} passed, "
                  f"{summary['known_issues']} known issue(s), {summary['unexpected_failures']} unexpected failure(s)")
    for name, value in summary["metrics"].items():
        tr.write_line(f"  {name:24} {value}")
    tr.write_line(f"score report: {paths['html']}")
