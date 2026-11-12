"""The web pages served by the API: the chat page at /, the guide, and the linked files.
Only an explicit list of files is public; everything else in the project must stay private."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.api


@pytest.mark.parametrize(
    "path, content_type, must_contain",
    [
        ("/", "text/html", "Ask your appliance manual."),
        ("/index.html", "text/html", "Drop your manual here"),
        ("/guide.html", "text/html", "Open the chatbot"),
        ("/sample-report.html", "text/html", "Chatbot evaluation report"),
        ("/docs/LLM_Chatbot_Eval_Guide.pdf", "application/pdf", None),
        ("/README.md", "text/plain", "LLM Chatbot Evaluation"),
    ],
)
def test_public_pages_are_served(api, path, content_type, must_contain):
    response = api.session.get(f"{api.base_url}{path}", timeout=30)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    if must_contain:
        assert must_contain in response.text


@pytest.mark.parametrize(
    "path",
    ["/.env", "/.env.example", "/app/main.py", "/site/src/chat.js", "/data/pii_allowlist.json",
     "/data/uploads/", "/..%2f.env", "/site/build.py"],
)
def test_private_files_are_not_served(api, path):
    assert api.session.get(f"{api.base_url}{path}", timeout=30).status_code == 404


def test_chat_page_is_self_contained(api):
    html = api.session.get(f"{api.base_url}/", timeout=30).text
    assert "<style>" in html and "<script>" in html
    assert 'href="styles.css"' not in html and 'src="chat.js"' not in html
