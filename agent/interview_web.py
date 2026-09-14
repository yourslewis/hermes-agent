"""Credential-free, public-only HTTP research for interview mode.

No urllib opener, proxies, cookies, netrc, or ambient authorization. DNS answers
are checked in their entirety; HTTP connects to a validated numeric IP, with the
original hostname retained for TLS certificate verification/SNI and Host.
"""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import http.client
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET


class WebError(ValueError):
    """A denied URL, bounded transport failure, or unusable public response."""


@dataclass(frozen=True)
class Target:
    url: str
    scheme: str
    hostname: str
    port: int
    ip: str
    path: str
    authority: str


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes
    url: str


_DNS_SLOTS = threading.BoundedSemaphore(8)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WebError("Total request deadline exceeded")
    return remaining


def _public_ip(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError as exc:
        raise WebError("DNS returned an invalid IP address") from exc
    if (not ip.is_global or ip.is_multicast or ip.is_reserved
            or ip.is_loopback or ip.is_link_local or ip.is_unspecified):
        raise WebError("Only public IP addresses are allowed")
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            _public_ip(str(ip.ipv4_mapped))
        # Avoid transition mechanisms whose embedded endpoint can be private.
        if ip.sixtofour or ip.teredo or ip in ipaddress.ip_network("64:ff9b::/96"):
            raise WebError("IPv6 transition addresses are not allowed")
    return str(ip)


def _parse_url(url):
    if not isinstance(url, str) or len(url) > 8192 or re.search(r"[\x00-\x20\x7f\\]", url):
        raise WebError("Invalid URL characters or length")
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise WebError("Only absolute HTTP/HTTPS URLs are allowed")
        if parts.username is not None or parts.password is not None:
            raise WebError("URL credentials are not allowed")
        hostname = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except (ValueError, UnicodeError) as exc:
        raise WebError("Invalid HTTP URL") from exc
    if "%" in hostname:
        raise WebError("Scoped or encoded hostnames are not allowed")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if ("." not in hostname or hostname.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".test", ".invalid"))
                or not re.fullmatch(r"[a-z0-9.-]+", hostname)):
            raise WebError("Local or invalid hostnames are not allowed")
    else:
        _public_ip(hostname)
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port != (443 if parts.scheme == "https" else 80):
        authority += f":{port}"
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parts.query:
        path += "?" + quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
    normalized = urlunsplit((parts.scheme, authority, path.split("?", 1)[0], path.partition("?")[2], ""))
    return normalized, parts.scheme, hostname, port, path, authority


class PublicWebClient:
    """Inject resolver/getaddrinfo and transport(Target, deadline) for tests."""

    def __init__(self, *, resolver=None, transport=None, socket_timeout=10,
                 total_timeout=25, max_bytes=2_000_000, max_redirects=4):
        self.resolver = resolver or socket.getaddrinfo
        self.transport = transport or self._transport
        self.socket_timeout = socket_timeout
        self.total_timeout = total_timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects

    def _resolve(self, hostname, port, deadline):
        # getaddrinfo has no timeout. Bound caller wait and outstanding daemon
        # workers, and never let the DNS worker perform HTTP after a timeout.
        if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
            raise WebError("DNS deadline exceeded")
        result = queue.Queue(maxsize=1)
        def worker():
            try:
                result.put((True, self.resolver(hostname, port, type=socket.SOCK_STREAM)))
            except Exception as exc:
                result.put((False, exc))
            finally:
                _DNS_SLOTS.release()
        threading.Thread(target=worker, daemon=True, name="interview-public-dns").start()
        try:
            ok, records = result.get(timeout=_remaining(deadline))
        except queue.Empty as exc:
            raise WebError("DNS deadline exceeded") from exc
        if not ok:
            raise WebError(f"DNS lookup failed: {records}")
        addresses = [_public_ip(record[4][0]) for record in records]
        if not addresses:
            raise WebError("DNS returned no public addresses")
        return addresses[0]

    def get(self, url, *, deadline=None):
        deadline = min(deadline, time.monotonic() + self.total_timeout) if deadline is not None else time.monotonic() + self.total_timeout
        for hop in range(self.max_redirects + 1):
            _remaining(deadline)
            normalized, scheme, hostname, port, path, authority = _parse_url(url)
            ip = self._resolve(hostname, port, deadline)
            target = Target(normalized, scheme, hostname, port, ip, path, authority)
            _remaining(deadline)
            try:
                response = self.transport(target, deadline)
            except (OSError, http.client.HTTPException) as exc:
                raise WebError(f"Public HTTP request failed: {exc}") from exc
            _remaining(deadline)
            if len(response.body) > self.max_bytes:
                raise WebError("Response exceeds byte limit")
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise WebError("Redirect is missing Location")
                if hop == self.max_redirects:
                    raise WebError("Too many redirects")
                # Validate raw Location before urljoin can normalize controls.
                if re.search(r"[\x00-\x20\x7f\\]", location):
                    raise WebError("Invalid redirect URL")
                url = urljoin(normalized, location)
                continue
            if not 200 <= response.status < 300:
                raise WebError(f"HTTP {response.status}")
            return response
        raise WebError("Too many redirects")

    def _transport(self, target, deadline):
        sock = socket.create_connection((target.ip, target.port), timeout=min(self.socket_timeout, _remaining(deadline)))
        # A socket timeout alone allows slow-drip headers/body indefinitely.
        # Shutdown at the absolute deadline interrupts even buffered reads.
        active = [sock]
        def expire():
            try:
                active[0].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(_remaining(deadline), expire)
        timer.daemon = True
        timer.start()
        response = None
        try:
            if target.scheme == "https":
                sock.settimeout(min(self.socket_timeout, _remaining(deadline)))
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=target.hostname)
                active[0] = sock
            sock.settimeout(min(self.socket_timeout, _remaining(deadline)))
            request = (f"GET {target.path} HTTP/1.1\r\nHost: {target.authority}\r\n"
                       "User-Agent: Mozilla/5.0 (compatible; HermesInterviewResearch/1.0)\r\n"
                       "Accept: text/html,text/plain,application/json,application/xml;q=0.8,*/*;q=0.1\r\n"
                       "Accept-Encoding: identity\r\nConnection: close\r\n\r\n")
            sock.sendall(request.encode("ascii"))
            response = http.client.HTTPResponse(sock)
            response.begin()
            headers = {key.lower(): value for key, value in response.getheaders()}
            if headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                raise WebError("Compressed responses are not supported")
            length = headers.get("content-length")
            if length is not None:
                try:
                    length = int(length)
                except ValueError as exc:
                    raise WebError("Invalid Content-Length") from exc
                if length < 0 or length > self.max_bytes:
                    raise WebError("Response exceeds byte limit")
            data = bytearray()
            while True:
                sock.settimeout(min(self.socket_timeout, _remaining(deadline)))
                block = response.read1(min(65536, self.max_bytes + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
                if len(data) > self.max_bytes:
                    raise WebError("Response exceeds byte limit")
            _remaining(deadline)
            return Response(response.status, headers, bytes(data), target.url)
        finally:
            timer.cancel()
            if response:
                response.close()
            sock.close()


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.in_title = False
        self.title = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag == "title":
            self.in_title = True
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "tr", "section", "article"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag == "title":
            self.in_title = False
        if tag in ("p", "div", "li", "h1", "h2", "h3", "tr", "section", "article"):
            self.text.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            (self.title if self.in_title else self.text).append(data)


def _decode(response):
    match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", response.headers.get("content-type", ""), re.I)
    encoding = match.group(1) if match else "utf-8"
    try:
        return response.body.decode(encoding, errors="replace")
    except LookupError:
        return response.body.decode("utf-8", errors="replace")


_client = PublicWebClient()


def web_extract(urls) -> dict:
    """Extract at most five public HTML/text pages, returning per-URL errors."""
    if not isinstance(urls, (list, tuple)) or not 1 <= len(urls) <= 5:
        return {"results": [], "error": "urls must contain 1 to 5 URLs"}
    results = []
    deadline = time.monotonic() + _client.total_timeout
    for url in urls:
        item = {"url": url, "title": "", "content": "", "error": None}
        try:
            response = _client.get(url, deadline=deadline)
            kind = response.headers.get("content-type", "text/html").lower().split(";", 1)[0]
            if not (kind.startswith("text/") or kind in ("application/json", "application/xml", "application/xhtml+xml")):
                raise WebError(f"Unsupported content type: {kind}")
            text = _decode(response)
            if kind in ("text/html", "application/xhtml+xml"):
                parser = _TextParser()
                parser.feed(text)
                item["title"] = " ".join("".join(parser.title).split())
                text = "\n".join(line for line in (" ".join(line.split()) for line in "".join(parser.text).splitlines()) if line)
            item.update(url=response.url, content=text[:50000], truncated=len(text) > 50000)
        except (WebError, OSError, ValueError) as exc:
            item["error"] = str(exc)
        results.append(item)
    return {"results": results}


class _SearchParser(HTMLParser):
    """DuckDuckGo HTML result anchors and their adjacent snippets."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self.capture = None
        self.end_tag = None
        self.hidden = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style"):
            self.hidden = tag
        if self.hidden:
            return
        classes = (attrs.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self.results.append({"url": attrs.get("href", ""), "title": "", "description": ""})
            self.capture, self.end_tag = "title", tag
        elif "result__snippet" in classes and self.results:
            self.capture, self.end_tag = "description", tag

    def handle_endtag(self, tag):
        if self.hidden:
            if self.hidden == tag:
                self.hidden = None
            return
        if tag == self.end_tag:
            self.capture = self.end_tag = None

    def handle_data(self, data):
        if not self.hidden and self.capture and self.results:
            self.results[-1][self.capture] += data


def _search_results(response, source):
    text = _decode(response)
    if any(marker in text.lower() for marker in (
        "challenge-form", "anomaly.js", "bots use duckduckgo", "captcha", "verify you are human"
    )):
        raise WebError("Search provider returned a bot challenge")
    if source == "duckduckgo":
        parser = _SearchParser()
        parser.feed(text)
        rows = parser.results
    else:
        # Reject DTDs/entities instead of permitting XML expansion.
        if "<!doctype" in text.lower() or "<!entity" in text.lower():
            raise WebError("Unexpected search XML declarations")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise WebError("No results: search provider returned invalid RSS") from exc
        rows = [{"url": item.findtext("link", ""), "title": item.findtext("title", ""),
                 "description": item.findtext("description", "")} for item in root.findall("./channel/item")]
    clean = []
    seen = set()
    for row in rows:
        try:
            url = urljoin(response.url, row["url"])
            parts = urlsplit(url)
            if (parts.hostname or "").endswith("duckduckgo.com") and parts.path.startswith("/l/"):
                url = parse_qs(parts.query).get("uddg", [""])[0]
            url = _parse_url(url)[0]
        except (ValueError, WebError):
            continue
        if url in seen:
            continue
        seen.add(url)
        clean.append({"url": url, "title": " ".join(row["title"].split()),
                      "description": " ".join(row["description"].split())})
    if not clean:
        raise WebError("No results returned by public search provider")
    return clean


def web_search(query, limit=5) -> dict:
    """Search public DDG HTML; fall back to public Bing RSS on failure.

    Both providers use the same pinned-IP transport. Failures are explicit,
    never replaced with synthetic results. The whole operation has one deadline.
    """
    empty = {"data": {"web": []}}
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        return {**empty, "error": "query must be a non-empty string of at most 2000 characters"}
    if type(limit) is not int or not 1 <= limit <= 20:
        return {**empty, "error": "limit must be an integer from 1 to 20"}
    query = quote(query.strip(), safe="")
    providers = (("duckduckgo", f"https://html.duckduckgo.com/html/?q={query}"),
                 ("bing", f"https://www.bing.com/search?format=rss&q={query}"))
    deadline = time.monotonic() + _client.total_timeout
    errors = []
    for index, (source, url) in enumerate(providers):
        try:
            # Reserve time for fallback even when the first provider hangs.
            attempt_deadline = min(deadline, time.monotonic() + _client.total_timeout / 2) if index == 0 else deadline
            response = _client.get(url, deadline=attempt_deadline)
            rows = _search_results(response, source)
            return {"data": {"web": rows[:limit]}, "source": source, "warnings": errors}
        except (WebError, OSError, ValueError) as exc:
            errors.append(f"{source}: {exc}")
    return {**empty, "error": "; ".join(errors)}
