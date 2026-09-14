"""Public-only interview HTTP transport regression tests (no live network)."""
import importlib
import socket
import time
from io import BytesIO

import pytest


def module():
    return importlib.import_module("agent.interview_web")


def public_dns(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x", "http://user:pass@example.com",
    "http://localhost", "http://printer.local", "http://127.0.0.1",
    "http://10.0.0.1", "http://169.254.169.254", "http://[::1]",
    "http://[::ffff:127.0.0.1]", "http://224.0.0.1", "http://example.com\\@127.0.0.1",
    "http://example.com/\r\nInjected: yes",
])
def test_unsafe_urls_never_reach_transport(url):
    web = module()
    calls = []
    client = web.PublicWebClient(resolver=public_dns, transport=lambda *args: calls.append(args))
    with pytest.raises(web.WebError):
        client.get(url)
    assert calls == []


def test_mixed_public_private_dns_denies_entire_request():
    web = module()
    calls = []
    def resolver(host, port, **kwargs):
        return public_dns(host, port) + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port))]
    with pytest.raises(web.WebError, match="public"):
        web.PublicWebClient(resolver=resolver, transport=lambda *a: calls.append(a)).get("https://example.com")
    assert not calls


def test_redirect_to_private_never_sent():
    web = module()
    calls = []
    def transport(target, deadline):
        calls.append(target)
        return web.Response(302, {"location": "http://169.254.169.254/secret"}, b"", target.url)
    with pytest.raises(web.WebError):
        web.PublicWebClient(resolver=public_dns, transport=transport).get("https://example.com")
    assert len(calls) == 1


def test_relative_redirect_re_resolves_and_blocks_rebinding():
    web = module()
    resolved, sent = [], []
    def resolver(host, port, **kwargs):
        resolved.append(host)
        return public_dns(host, port) if len(resolved) == 1 else [(2, 1, 6, "", ("127.0.0.1", port))]
    def transport(target, deadline):
        sent.append(target.ip)
        return web.Response(302, {"location": "/next"}, b"", target.url)
    with pytest.raises(web.WebError):
        web.PublicWebClient(resolver=resolver, transport=transport).get("https://example.com")
    assert sent == ["93.184.216.34"]
    assert len(resolved) == 2


def test_actual_transport_pins_ip_keeps_tls_sni_host_and_ignores_proxy(monkeypatch):
    web = module()
    events = []
    class Sock:
        def settimeout(self, timeout):
            assert 0 < timeout <= 10
        def sendall(self, data):
            events.append(("request", data))
        def makefile(self, *args):
            return BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        def shutdown(self, *args):
            pass
        def close(self):
            pass
    sock = Sock()
    def connect(address, timeout):
        events.append(("connect", address))
        return sock
    class TLS:
        def wrap_socket(self, raw, server_hostname):
            assert raw is sock
            events.append(("sni", server_hostname))
            return sock
    monkeypatch.setenv("HTTPS_PROXY", "http://private:password@127.0.0.1:1")
    monkeypatch.setattr(web.socket, "create_connection", connect)
    monkeypatch.setattr(web.ssl, "create_default_context", lambda: TLS())
    response = web.PublicWebClient(resolver=public_dns).get("https://example.com/path?q=1")
    assert response.body == b"ok"
    assert ("connect", ("93.184.216.34", 443)) in events
    assert ("sni", "example.com") in events
    request = next(data for event, data in events if event == "request")
    assert b"Host: example.com\r\n" in request
    assert b"Authorization" not in request and b"Cookie:" not in request


def test_dns_obeys_total_deadline():
    web = module()
    def slow_dns(*args, **kwargs):
        time.sleep(0.3)
        return public_dns("example.com", 443)
    started = time.monotonic()
    with pytest.raises(web.WebError, match="deadline|timed out"):
        web.PublicWebClient(resolver=slow_dns, total_timeout=0.03).get("https://example.com")
    assert time.monotonic() - started < 0.2


def test_extract_html_removes_scripts_and_returns_per_url_errors(monkeypatch):
    web = module()
    def transport(target, deadline):
        return web.Response(200, {"content-type": "text/html; charset=utf-8"},
                            b"<html><title>A &amp; B</title><script>secret()</script><style>hidden</style><p>Hello <b>world</b></p></html>", target.url)
    monkeypatch.setattr(web, "_client", web.PublicWebClient(resolver=public_dns, transport=transport))
    result = web.web_extract(["https://example.com", "http://127.0.0.1"])
    page, denied = result["results"]
    assert page["title"] == "A & B"
    assert "Hello world" in page["content"]
    assert "secret" not in page["content"] and "hidden" not in page["content"]
    assert denied["error"] and denied["content"] == ""


DDG_HTML = b'''<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python.org%2F">Python <b>Home</b></a><a class="result__snippet">The programming language.</a></div>'''


def test_search_parses_ddg_links_snippets_and_limit(monkeypatch):
    web = module()
    sent = []
    def transport(target, deadline):
        sent.append(target)
        return web.Response(200, {"content-type": "text/html"}, DDG_HTML * 2, target.url)
    monkeypatch.setattr(web, "_client", web.PublicWebClient(resolver=public_dns, transport=transport))
    result = web.web_search("python programming", limit=1)
    assert result["data"]["web"] == [{"url": "https://www.python.org/", "title": "Python Home", "description": "The programming language."}]
    assert sent[0].hostname == "html.duckduckgo.com"
    assert "python%20programming" in sent[0].path


def test_search_uses_alternate_public_engine_on_challenge(monkeypatch):
    web = module()
    sent = []
    def transport(target, deadline):
        sent.append(target.hostname)
        if "duckduckgo" in target.hostname:
            return web.Response(202, {}, b'<form id="challenge-form">Unfortunately, bots use DuckDuckGo too</form>', target.url)
        return web.Response(200, {"content-type": "application/rss+xml"}, b'<rss><channel><item><title>Python</title><link>https://www.python.org/</link><description>Official site</description></item></channel></rss>', target.url)
    monkeypatch.setattr(web, "_client", web.PublicWebClient(resolver=public_dns, transport=transport))
    result = web.web_search("python")
    assert result["data"]["web"][0]["url"] == "https://www.python.org/"
    assert sent == ["html.duckduckgo.com", "www.bing.com"]
    assert result["warnings"] and result["source"] == "bing"


@pytest.mark.parametrize("body, message", [(b"No results", "No results"), (b"challenge-form", "challenge")])
def test_search_returns_explicit_errors_not_fake_results(monkeypatch, body, message):
    web = module()
    def transport(target, deadline):
        return web.Response(200, {}, body, target.url)
    monkeypatch.setattr(web, "_client", web.PublicWebClient(resolver=public_dns, transport=transport))
    result = web.web_search("query")
    assert result["data"]["web"] == []
    assert message in result["error"]


@pytest.mark.parametrize("query, limit", [("", 5), (None, 5), ("hi", 0), ("hi", 100), ("hi", "2")])
def test_search_validates_inputs(query, limit):
    result = module().web_search(query, limit)
    assert result["error"] and result["data"]["web"] == []


def test_response_size_and_redirect_count_are_bounded():
    web = module()
    def large(target, deadline):
        return web.Response(200, {}, b"x" * 11, target.url)
    with pytest.raises(web.WebError, match="byte limit"):
        web.PublicWebClient(resolver=public_dns, transport=large, max_bytes=10).get("https://example.com")
    sent = []
    def loop(target, deadline):
        sent.append(target)
        return web.Response(302, {"location": "/loop"}, b"", target.url)
    with pytest.raises(web.WebError, match="redirects"):
        web.PublicWebClient(resolver=public_dns, transport=loop, max_redirects=2).get("https://example.com")
    assert len(sent) == 3


@pytest.mark.parametrize("payload, message", [
    (b"HTTP/1.1 200 OK\r\nContent-Length: 99999999\r\n\r\n", "byte limit"),
    (b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n", "Compressed"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nB\r\n12345678901\r\n0\r\n\r\n", "byte limit"),
])
def test_real_transport_bounds_body_and_rejects_compression(monkeypatch, payload, message):
    web = module()
    class Sock:
        def settimeout(self, value):
            pass
        def sendall(self, value):
            pass
        def makefile(self, *args):
            return BytesIO(payload)
        def close(self):
            pass
    monkeypatch.setattr(web.socket, "create_connection", lambda *a, **k: Sock())
    with pytest.raises(web.WebError, match=message):
        web.PublicWebClient(resolver=public_dns, max_bytes=10).get("http://example.com")


def test_absolute_deadline_interrupts_slow_drip_headers(monkeypatch):
    import threading
    web = module()
    client_sock, server_sock = socket.socketpair()
    def server():
        try:
            server_sock.recv(8192)
            for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
                server_sock.send(bytes([byte]))
                time.sleep(0.01)
        except OSError:
            pass
        finally:
            server_sock.close()
    worker = threading.Thread(target=server, daemon=True)
    worker.start()
    monkeypatch.setattr(web.socket, "create_connection", lambda *a, **k: client_sock)
    started = time.monotonic()
    with pytest.raises(web.WebError):
        web.PublicWebClient(resolver=public_dns, total_timeout=0.08).get("http://example.com")
    assert time.monotonic() - started < 0.3
    worker.join(timeout=1)


def test_search_handles_boolean_class_attribute(monkeypatch):
    web = module()
    def transport(target, deadline):
        return web.Response(200, {}, b"<div class>unrelated</div>" + DDG_HTML, target.url)
    monkeypatch.setattr(web, "_client", web.PublicWebClient(resolver=public_dns, transport=transport))
    assert web.web_search("python")["data"]["web"][0]["title"] == "Python Home"
