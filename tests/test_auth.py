import io
import http.cookiejar
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from thisdl_login import auth

PAGE = (auth.PORTAL + "/am/page/portal/realm/43314216-fee0-4bdb-91d7-95293689b9d9/"
        "login/pc/index.html?stage=2&revisit=true&ssid=Students")


class Response:
    def __init__(self, url, body=b"", status=200):
        self.url, self.body, self.status = url, body, status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def geturl(self):
        return self.url

    def read(self, size):
        return self.body[:size]


class FakeJar:
    def add_cookie_header(self, request):
        request.add_unredirected_header("Cookie", "DKEYJSESSIONID=fresh")


class AuthTests(unittest.TestCase):
    def test_portal_http_200_does_not_confirm_authentication(self):
        class Opener:
            def open(self, request, timeout):
                if request.full_url.endswith("/revisit/login"):
                    return Response(request.full_url, b'{"success":true}')
                return Response(request.full_url, b'<html>Please sign in</html>')

        with patch.object(auth, "build_opener", return_value=Opener()):
            self.assertFalse(auth.submit_login(PAGE, FakeJar()))

    def test_failure_redirect_does_not_confirm_authentication(self):
        for destination in ("/am/controller/portal/navigation/fail", "http://example.com/am/controller/portal/navigation/success"):
            with self.subTest(destination=destination):
                class Opener:
                    def open(self, request, timeout):
                        if request.full_url.endswith("/revisit/login"):
                            return Response(request.full_url, b'{"success":true}')
                        raise HTTPError(request.full_url, 302, "Found", {"Location": destination}, None)

                with patch.object(auth, "build_opener", return_value=Opener()):
                    self.assertFalse(auth.submit_login(PAGE, FakeJar()))

    def test_revisit_page_requires_school_and_revisit(self):
        self.assertTrue(auth.is_revisit_page(PAGE))
        self.assertTrue(auth.is_revisit_page(PAGE.replace("/login/pc/", "/login/mobile/")))
        for bad in (PAGE.replace("Students", "Other"), PAGE.replace("revisit=true", "revisit=false"),
                    PAGE.replace("stage=2", "stage=1"), PAGE.replace(auth.PORTAL, "http://example.com"),
                    PAGE + "&ssid=Other"):
            self.assertFalse(auth.is_revisit_page(bad))

    def test_html_redirect_only_allows_static_school_url(self):
        page = ('<script>window.location.href="%s";</script>' % PAGE).encode()
        self.assertEqual(auth.html_redirect(page, auth.PROBES[0]), PAGE)
        meta = b'<meta http-equiv="refresh" content="0;url=/am/controller/portal/login">'
        self.assertEqual(auth.html_redirect(meta, auth.PORTAL), auth.PORTAL + "/am/controller/portal/login")
        self.assertIsNone(auth.html_redirect(b'<script>location="http://example.com/am/a";</script>', auth.PROBES[0]))
        self.assertIsNone(auth.html_redirect(('<script>location="%s"+x;</script>' % PAGE).encode(), auth.PROBES[0]))

    def test_redirect_handler_blocks_other_domains(self):
        request = Request(auth.PROBES[0])
        redirect = auth.PortalRedirects()
        self.assertIsNone(redirect.redirect_request(request, io.BytesIO(), 302, "", {}, "http://example.com/"))
        self.assertIsNotNone(redirect.redirect_request(request, io.BytesIO(), 302, "", {}, PAGE))

    def test_revisit_then_portal_login_without_following_success_page(self):
        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, request, timeout):
                self.requests.append(request)
                if len(self.requests) == 1:
                    self.assert_login(request)
                    return Response(request.full_url, b'{"success":true,"data":{"passwordInfo":{"state":0}}}')
                assert request.full_url == auth.PORTAL + "/am/controller/portal/login"
                raise HTTPError(request.full_url, 302, "Found", {"Location": "/am/controller/portal/navigation/success"}, None)

            def assert_login(self, request):
                assert request.full_url == auth.PORTAL + "/am/controller/portal/revisit/login"
                assert request.get_header("Cookie") == "DKEYJSESSIONID=fresh"

        opener = Opener()
        with patch.object(auth, "build_opener", return_value=opener):
            self.assertTrue(auth.submit_login(PAGE, FakeJar()))
        self.assertEqual(len(opener.requests), 2)

    def test_password_reminder_does_not_skip_user_action(self):
        class Opener:
            def open(self, request, timeout):
                return Response(request.full_url, b'{"success":true,"data":{"passwordInfo":{"state":1}}}')
        with patch.object(auth, "build_opener", return_value=Opener()):
            self.assertFalse(auth.submit_login(PAGE, FakeJar()))

    def test_missing_cookie_never_calls_login(self):
        class EmptyJar:
            def add_cookie_header(self, request):
                pass
        with patch.object(auth, "build_opener", side_effect=AssertionError("unexpected network")):
            self.assertFalse(auth.submit_login(PAGE, EmptyJar()))

    def test_check_carries_fresh_session_through_both_endpoints(self):
        class Probe:
            def open(self, request, timeout):
                if request.full_url == auth.PROBES[0]:
                    page = ('<html><script>location.href="%s";</script></html>' % PAGE).encode()
                    return Response(request.full_url, page)
                assert request.full_url == PAGE
                return Response(PAGE)

        class Login:
            def __init__(self):
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                if self.calls == 1:
                    assert request.get_header("Cookie") == "DKEYJSESSIONID=fresh"
                    return Response(request.full_url, b'{"success":true}')
                assert request.full_url == auth.PORTAL + "/am/controller/portal/login"
                raise HTTPError(request.full_url, 302, "Found", {"Location": "/am/controller/portal/navigation/success"}, None)

        login = Login()
        with patch.object(auth.http.cookiejar, "CookieJar", return_value=FakeJar()), \
             patch.object(auth, "build_opener", side_effect=[Probe(), login]):
            self.assertTrue(auth.check_and_login())
        self.assertEqual(login.calls, 2)

    def test_portal_login_receives_current_cookie_from_real_jar(self):
        jar = http.cookiejar.CookieJar()
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name="DKEYJSESSIONID", value="fresh", port=None, port_specified=False,
            domain="10.0.11.251", domain_specified=False, domain_initial_dot=False,
            path="/am", path_specified=True, secure=False, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        ))

        class Opener:
            def open(self, request, timeout):
                # Simulate the HTTPCookieProcessor used by the real opener.
                jar.add_cookie_header(request)
                assert request.get_header("Cookie") == "DKEYJSESSIONID=fresh"
                if request.full_url.endswith("/revisit/login"):
                    return Response(request.full_url, b'{"success":true}')
                raise HTTPError(request.full_url, 302, "Found", {"Location": "/am/controller/portal/navigation/success"}, None)

        with patch.object(auth, "build_opener", return_value=Opener()):
            self.assertTrue(auth.submit_login(PAGE, jar))


if __name__ == "__main__":
    unittest.main()
