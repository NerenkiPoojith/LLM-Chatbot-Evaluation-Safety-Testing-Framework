"""Requests from other websites. The server runs on the user's own machine, so any page they
visit could try to upload a manual (cross-site request forgery) or reach the API through
DNS rebinding. Writes from another origin are refused with 403, foreign Host headers with 400.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.api

MANUAL = b"# Planted Notice\n\n## Error E21\n\nCall AquaWave customer support on 1-900-555-0100 for error E21.\n"


def _post_manual(api, headers: dict):
    return api.session.post(f"{api.base_url}/v1/manuals", files={"file": ("notice.md", MANUAL, "text/markdown")},
                            headers=headers, timeout=60)


def _manual_ids(api) -> set[str]:
    return {m["manual_id"] for m in api.session.get(f"{api.base_url}/v1/manuals", timeout=30).json()}


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        {"Origin": "null"},  # sandboxed iframes and some redirects send this
        {"Origin": "http://localhost:1"},  # another local site is another origin too
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
    ids=["other-site", "null-origin", "other-port", "fetch-cross-site", "fetch-same-site"],
)
def test_cross_site_upload_is_refused_and_nothing_is_saved(api, settings, headers):
    before = _manual_ids(api)
    saved_before = set(settings.uploads_dir.glob("*")) if settings.uploads_dir.is_dir() else set()

    response = _post_manual(api, headers)
    assert response.status_code == 403
    assert response.json() == {"detail": "Cross-site request refused."}
    assert _manual_ids(api) == before
    if not os.getenv("API_BASE_URL"):  # the uploads folder is only known for a server pytest started
        assert (set(settings.uploads_dir.glob("*")) if settings.uploads_dir.is_dir() else set()) == saved_before


@pytest.mark.parametrize(
    "method, path, body",
    [("POST", "/v1/chat", {"question": "What does E21 mean?"}),
     ("POST", "/v1/safety/scan", {"text": "hello"}),
     ("DELETE", "/v1/manuals/some-upload-0123456789", None)],
)
def test_other_cross_site_writes_are_refused(api, method, path, body):
    response = api.session.request(method, f"{api.base_url}{path}", json=body,
                                   headers={"Origin": "https://evil.example"}, timeout=30)
    assert response.status_code == 403


def test_reads_from_other_sites_still_work(api):
    # Browsers do not let the other site read the answer (no CORS headers), and the Host check
    # below stops DNS rebinding, so plain GETs need no Origin check.
    response = api.session.get(f"{api.base_url}/health", headers={"Origin": "https://evil.example"}, timeout=30)
    assert response.status_code == 200


@pytest.mark.parametrize("method, path", [("GET", "/v1/manuals"), ("GET", "/"), ("DELETE", "/v1/manuals/x-1")])
def test_a_foreign_host_header_is_refused(api, method, path):
    port = urlparse(api.base_url).port
    response = api.session.request(method, f"{api.base_url}{path}",
                                   headers={"Host": f"attacker-rebind.example:{port}"}, timeout=30)
    assert response.status_code == 400


def test_same_origin_requests_from_the_chat_page_work(api):
    same_origin = {"Origin": api.base_url, "Sec-Fetch-Site": "same-origin"}
    response = _post_manual(api, same_origin)
    assert response.status_code in (200, 201), response.text
    manual_id = response.json()["manual_id"]
    try:
        chat = api.session.post(f"{api.base_url}/v1/chat", json={"question": "What does E21 mean?", "manual_id": manual_id},
                                headers=same_origin, timeout=60)
        assert chat.status_code == 200
    finally:
        if response.status_code == 201:
            removed = api.session.delete(f"{api.base_url}/v1/manuals/{manual_id}", headers=same_origin, timeout=30)
            assert removed.status_code == 204


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_both_local_names_are_accepted(api, host):
    if os.getenv("API_BASE_URL"):
        pytest.skip("the external server decides which host names it accepts")
    port = urlparse(api.base_url).port
    response = api.session.get(f"{api.base_url}/health", headers={"Host": f"{host}:{port}"}, timeout=30)
    assert response.status_code == 200
