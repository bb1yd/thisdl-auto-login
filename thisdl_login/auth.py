"""Discover and authenticate an existing campus-network revisit session."""

import html
import http.cookiejar
import json
import logging
import re
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .transport import WiFiHTTPHandler

LOG = logging.getLogger(__name__)
PORTAL = "http://10.0.11.251:8080"
PORTAL_HOST = "10.0.11.251:8080"
REALM_LOGIN = re.compile(r"^/am/page/portal/realm/43314216-fee0-4bdb-91d7-95293689b9d9/login/[A-Za-z0-9_-]{1,32}/index\.html$")
PROBES = (
    "http://connectivitycheck.gstatic.com/",
    "http://connectivitycheck.gstatic.com/generate_204",
    "http://captive.apple.com/hotspot-detect.html",
    "http://www.msftconnecttest.com/connecttest.txt",
)
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Safari/605.1.15"
TIMEOUT = 5


class PortalRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urlsplit(urljoin(req.full_url, newurl))
        if (parts.scheme, parts.netloc) != ("http", PORTAL_HOST):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RedirectPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.refreshes = []
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name and name.lower().startswith("on") and value:
                self.scripts.append(value)
        if tag.lower() == "script":
            self.in_script = True
        elif tag.lower() == "meta":
            attrs = dict(attrs)
            if (attrs.get("http-equiv") or "").lower() == "refresh":
                self.refreshes.append(attrs.get("content") or "")

    def handle_endtag(self, tag):
        if tag.lower() == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.scripts.append(data)


def html_redirect(body, base_url):
    page = RedirectPage()
    try:
        page.feed(body.decode("utf-8", "replace"))
    except (ValueError, AssertionError):
        return None
    candidates = []
    for refresh in page.refreshes:
        match = re.match(r"\s*\d+(?:\.\d+)?\s*;\s*url\s*=\s*(.+?)\s*$", refresh, re.I)
        if match:
            candidates.append(match.group(1).strip("\"'"))
    patterns = (
        r"(?<![\w.])(?:(?:window|top|document)\.)?location(?:\.href)?\s*=\s*([\"'])(.*?)\1\s*;",
        r"(?<![\w.])(?:(?:window|top|document)\.)?location\.replace\(\s*([\"'])(.*?)\1\s*\)",
    )
    for script in page.scripts:
        for pattern in patterns:
            candidates.extend(match.group(2) for match in re.finditer(pattern, script, re.I | re.S))
    for candidate in candidates:
        candidate = html.unescape(candidate).replace("\\/", "/")
        if any(char in candidate for char in "\\\r\n"):
            continue
        target = urljoin(base_url, candidate)
        parts = urlsplit(target)
        if (parts.scheme, parts.netloc) == ("http", PORTAL_HOST) and parts.path.startswith("/am/"):
            return target
    return None


def is_revisit_page(url):
    parts = urlsplit(url)
    params = parse_qs(parts.query)
    return (
        (parts.scheme, parts.netloc) == ("http", PORTAL_HOST)
        and REALM_LOGIN.fullmatch(parts.path) is not None
        and params.get("ssid") == ["Students"]
        and params.get("revisit") == ["true"]
        and params.get("stage") == ["2"]
        and params.get("editMode") != ["true"]
    )


def submit_login(page_url, jar, interface=None):
    headers = {"Accept": "application/json", "Cache-Control": "no-cache", "Referer": page_url,
               "User-Agent": USER_AGENT, "X-Requested-With": "XMLHttpRequest"}
    request = Request(PORTAL + "/am/controller/portal/revisit/login", headers=headers)
    jar.add_cookie_header(request)
    cookie = request.get_header("Cookie", "")
    if not any(part.strip().startswith("DKEYJSESSIONID=") and len(part.strip()) > len("DKEYJSESSIONID=") for part in cookie.split(";")):
        LOG.warning("认证页没有提供当前会话，请手动登录。")
        return False
    # The same in-memory cookie jar is used for both requests; no credentials are stored.
    handlers = [WiFiHTTPHandler(interface)] if interface else []
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(jar), NoRedirect(), *handlers)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("response too large")
            data = json.loads(raw)
        if not isinstance(data, dict) or data.get("success") is not True:
            LOG.warning("回访认证未成功，请手动登录。")
            return False
        info = data.get("data") or {}
        password_info = info.get("passwordInfo") if isinstance(info, dict) else None
        if isinstance(password_info, dict) and password_info.get("state") == 1:
            LOG.warning("认证页面需要处理密码提醒，请手动登录。")
            return False
        next_request = Request(PORTAL + "/am/controller/portal/login", headers={
            "Accept": "text/html,application/xhtml+xml,*/*", "Cache-Control": "no-cache",
            "Referer": page_url, "User-Agent": USER_AGENT,
        })
        try:
            with opener.open(next_request, timeout=TIMEOUT) as response:
                LOG.warning("认证请求返回 HTTP %s，但未确认成功页跳转，保留认证窗口。", response.status)
                return False
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                location = urlsplit(urljoin(next_request.full_url, exc.headers.get("Location", "")))
                expected = (location.scheme, location.netloc, location.path) == (
                    "http", PORTAL_HOST, "/am/controller/portal/navigation/success")
                if expected:
                    LOG.info("认证请求完成，服务器返回成功页跳转。")
                    return True
            LOG.warning("认证请求返回 HTTP %s。", exc.code)
            return False
    except (HTTPError, URLError, OSError, TimeoutError, ValueError):
        LOG.warning("无法完成认证请求，将在网络变化时重试。")
        return False


def check_and_login(interface=None):
    headers = {"User-Agent": USER_AGENT, "Cache-Control": "no-cache"}
    for probe in PROBES:
        jar = http.cookiejar.CookieJar()
        handlers = [WiFiHTTPHandler(interface)] if interface else []
        opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(jar), PortalRedirects(), *handlers)
        request = Request(probe, headers=headers)
        try:
            for hop in range(4):
                with opener.open(request, timeout=TIMEOUT) as response:
                    url = response.geturl()
                    if is_revisit_page(url):
                        LOG.info("发现校园网回访认证页。")
                        return submit_login(url, jar, interface=interface)
                    parts = urlsplit(url)
                    body = response.read(16384)
                    is_portal = (parts.scheme, parts.netloc) == ("http", PORTAL_HOST)
                    target = html_redirect(body, url) if is_portal or b"10.0.11.251" in body else None
                    if target and hop < 3:
                        request = Request(target, headers={**headers, "Referer": url})
                        continue
                    break
        except (HTTPError, URLError, OSError, TimeoutError, ValueError):
            continue
    return False
