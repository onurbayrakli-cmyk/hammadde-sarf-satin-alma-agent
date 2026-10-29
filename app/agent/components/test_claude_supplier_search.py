"""Offline protocol fixtures; these do not prove live account web-search access."""

from types import SimpleNamespace

import pytest

import claude_supplier_search as helper


def _citation(url="https://supplier.example/product#section", title="PE ürün sayfası"):
    return {"type": "web_search_result_location", "url": url, "title": title,
            "cited_text": "Unsupported free-form claim is not supplier evidence"}


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    monkeypatch.setattr(helper.socket, "getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("8.8.8.8", 443))])


def test_only_cited_public_sources_become_candidates():
    response = {"stop_reason": "end_turn", "content": [
        {"type": "web_search_tool_result", "content": [
            {"type": "web_search_result", "url": "https://uncited.example/", "title": "Skip"}]},
        {"type": "text", "text": "Invented business name or stock claim", "citations": [
            _citation(), _citation(), _citation("http://127.0.0.1/private", "Private")]}]}
    assert helper.parse_claude_supplier_citations(response) == [
        {"title": "PE ürün sayfası", "link": "https://supplier.example/product", "snippet": "", "content": ""}]


def test_sdk_object_citations_are_supported():
    citation = SimpleNamespace(**_citation())
    block = SimpleNamespace(type="text", citations=[citation], text="Ignored prose")
    response = SimpleNamespace(stop_reason="end_turn", content=[block])
    assert len(helper.parse_claude_supplier_citations(response)) == 1


@pytest.mark.parametrize("stop_reason", ["pause_turn", "max_tokens", "refusal", "tool_use"])
def test_incomplete_response_is_not_success(stop_reason):
    with pytest.raises(ValueError, match="tamamlanmadı"):
        helper.parse_claude_supplier_citations({"stop_reason": stop_reason, "content": []})


def test_http_success_with_tool_error_is_failure():
    response = {"stop_reason": "end_turn", "content": [
        {"type": "web_search_tool_result", "content": {
            "type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}},
        {"type": "text", "citations": [_citation()]}]}
    with pytest.raises(ValueError, match="arama aracı"):
        helper.parse_claude_supplier_citations(response)


def test_no_citations_is_not_no_supplier_evidence():
    with pytest.raises(ValueError, match="sonuç yok kabul edilmedi"):
        helper.parse_claude_supplier_citations({"stop_reason": "end_turn", "content": [
            {"type": "text", "text": "There are no suppliers", "citations": []}]})


@pytest.mark.parametrize("url", ["http://localhost/", "http://10.1.2.3/", "http://[::1]/",
                                 "https://user:password@example.org/", "file:///secret",
                                 "https://supplier.example:8080/", "https://company.local/"])
def test_private_or_credential_urls_are_rejected(url):
    with pytest.raises(ValueError):
        helper._claude_public_url(url)


def test_mixed_public_and_private_dns_is_rejected(monkeypatch):
    monkeypatch.setattr(helper.socket, "getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("8.8.8.8", 443)), (None, None, None, None, ("10.0.0.1", 443))])
    with pytest.raises(ValueError, match="özel ağa"):
        helper._claude_public_url("https://supplier.example/")


def test_request_is_bounded_and_has_only_public_query(monkeypatch):
    import anthropic
    calls = {}

    class FixtureClient:
        def __init__(self, **kwargs):
            calls["client"] = kwargs
            self.messages = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def create(self, **kwargs):
            calls["request"] = kwargs
            return {"stop_reason": "end_turn", "content": [{"type": "text", "citations": [_citation()]}]}

    monkeypatch.setattr(anthropic, "Anthropic", FixtureClient)
    rows = helper.claude_supplier_search("PE üretici tedarikçi Türkiye", "offline-fixture-credential")
    assert rows
    assert calls["client"]["timeout"] == 20.0
    assert calls["client"]["max_retries"] == 0
    assert calls["client"]["base_url"] == "https://api.anthropic.com"
    assert calls["request"]["max_tokens"] == 1200
    assert calls["request"]["tools"][0]["max_uses"] == 2
    assert calls["request"]["tools"][0]["user_location"]["country"] == "TR"
    assert calls["request"]["messages"] == [{"role": "user", "content": "PE üretici tedarikçi Türkiye"}]


def test_provider_exception_details_are_not_exposed(monkeypatch):
    import anthropic

    def fail(**kwargs):
        raise RuntimeError("credential or request body must not escape")

    monkeypatch.setattr(anthropic, "Anthropic", fail)
    with pytest.raises(ValueError) as captured:
        helper.claude_supplier_search("PE Türkiye", "offline-fixture-credential")
    assert "credential" not in str(captured.value)
    assert captured.value.__suppress_context__ is True
