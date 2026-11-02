"""Platform mode: the session cookie comes from the auth service, never from disk."""

import io
import json
import urllib.error

import pytest

from myfitnesspal_mcp import auth, mfp_client, refresh, session_persistence


class FakeHTTP:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.requests = status, body, []

    def __call__(self, req, timeout=0):
        self.requests.append(req)
        if self.status != 200:
            raise urllib.error.HTTPError(
                req.full_url, self.status, "x", {}, io.BytesIO(b"{}")
            )
        return io.BytesIO(json.dumps(self.body).encode())


@pytest.fixture
def platform(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_URL", "http://auth.auth.svc.cluster.local:8080")
    monkeypatch.setenv("AUTH_KEY", "app-key-value")
    monkeypatch.setenv("AUTH_PAGE_HINT", "sign in on the auth service page")
    monkeypatch.setattr(auth.config, "cookies_path", lambda: tmp_path / "cookies.json")
    monkeypatch.delenv("MFP_COOKIE", raising=False)
    return tmp_path


def test_cookie_comes_from_the_auth_service(platform, monkeypatch):
    http = FakeHTTP(
        200,
        {
            "name": "mfp",
            "value": "tok123",
            "state": "ok",
            "checked_at": "2026-11-02T00:00:00Z",
        },
    )
    monkeypatch.setattr("urllib.request.urlopen", http)
    assert auth.load_cookies() == {auth.SESSION_COOKIE: "tok123"}
    req = http.requests[0]
    assert req.full_url.endswith("/credentials/mfp")
    assert req.get_header("Authorization") == "Bearer app-key-value"
    assert not (platform / "cookies.json").exists()
    assert "auth service" in auth.session_source()


def test_dead_session_names_the_auth_page_and_no_value(platform, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", FakeHTTP(503))
    with pytest.raises(auth.CredentialUnavailable) as e:
        auth.load_cookies()
    assert "auth service page" in str(e.value) and "tok" not in str(e.value)
    mfp_client.reset()
    with pytest.raises(mfp_client.NotConnectedError, match="auth service page"):
        mfp_client.get_client()


def test_reconnect_hint_points_at_the_auth_page(platform):
    assert "auth service page" in mfp_client.reconnect_hint()
    assert "uvx" not in mfp_client.reconnect_hint()


def test_no_session_file_is_written_and_refresh_only_resets(platform, monkeypatch):
    class Client:
        session = type("S", (), {"cookies": type("J", (), {"jar": []})()})()
        loaded_cookies = {}
        saved_cookies = {}

    session_persistence.save_session(Client())
    assert not (platform / "cookies.json").exists()
    launched = []
    monkeypatch.setattr(
        refresh, "_visit_and_harvest", lambda seed: launched.append(True) or {}
    )
    monkeypatch.setattr(refresh, "profile_seeded", lambda: True)
    resets = []
    monkeypatch.setattr(mfp_client, "reset", lambda: resets.append(True))
    refresh.refresh_session()
    assert launched == [] and resets == [True]
