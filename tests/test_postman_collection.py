"""Keep the Postman collection in sync with the API.

Postman collections tend to go stale: someone renames an endpoint and the
collection quietly breaks. This test replays every request in the collection
with `requests` and checks it returns the status code that the request's own
Postman test script expects (`pm.response.to.have.status(...)`).

The JavaScript assertions themselves run in Postman or Newman:
    npx newman run postman/ApplianceHelp.postman_collection.json -e postman/local.postman_environment.json
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.api

COLLECTION = json.loads(
    (Path(__file__).resolve().parent.parent / "postman" / "ApplianceHelp.postman_collection.json").read_text(encoding="utf-8")
)


def _flatten(items: list[dict], prefix: str = "") -> list[tuple[str, dict]]:
    out = []
    for item in items:
        name = f"{prefix}{item['name']}"
        if "item" in item:
            out.extend(_flatten(item["item"], prefix=f"{name} / "))
        else:
            out.append((name, item))
    return out


REQUESTS = _flatten(COLLECTION["item"])


@pytest.mark.parametrize("name, item", REQUESTS, ids=[n for n, _ in REQUESTS])
def test_postman_request_returns_expected_status(name, item, api):
    request = item["request"]
    url = request["url"].replace("{{baseUrl}}", api.base_url)
    body = request.get("body", {}).get("raw")
    script = "\n".join(line for event in item.get("event", []) for line in event["script"]["exec"])
    expected = int(re.search(r"to\.have\.status\((\d+)\)", script).group(1))

    response = api.session.request(
        request["method"], url, data=body.encode("utf-8") if body else None,
        headers={h["key"]: h["value"] for h in request.get("header", [])}, timeout=120,
    )
    assert response.status_code == expected, f"{name}: {response.status_code} {response.text[:200]}"
