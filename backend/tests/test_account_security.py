"""Sessions end when a password changes, sign-ups are closed, and guessing gets locked out."""
import time

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.main import app


@pytest.fixture
def account(monkeypatch):
    name = f"sec{int(time.time() * 1000) % 10**9}"
    auth.create_user(name, "first-password")
    yield name


def test_password_reset_signs_out_existing_sessions(account):
    token = auth.make_token(account)
    assert auth.read_token(token) == account
    auth.set_password(account, "second-password")
    assert auth.read_token(token) is None
    assert auth.verify_user(account, "second-password")
    assert not auth.verify_user(account, "first-password")


def test_disabled_account_cannot_log_in_or_use_old_cookie(account):
    token = auth.make_token(account)
    auth.disable_user(account)
    assert auth.read_token(token) is None
    assert not auth.verify_user(account, "first-password")


def test_expired_and_forged_tokens_are_rejected(account, monkeypatch):
    monkeypatch.setattr(settings, "SESSION_DAYS", -1)
    assert auth.read_token(auth.make_token(account)) is None
    monkeypatch.setattr(settings, "SESSION_DAYS", 30)
    payload, _ = auth.make_token(account).split(".", 1)
    assert auth.read_token(f"{payload}.{'0' * 64}") is None


def test_signup_is_closed_by_default():
    response = TestClient(app).post("/api/signup",
                                    json={"username": "stranger1", "password": "whatever123"})
    assert response.status_code == 403
    assert auth.get_user("stranger1") is None


def test_repeated_wrong_passwords_lock_the_account(account):
    client = TestClient(app)
    for _ in range(settings.LOGIN_MAX_FAILURES):
        assert client.post("/api/login", json={"username": account,
                                               "password": "wrong-guess"}).status_code == 401
    locked = client.post("/api/login", json={"username": account, "password": "first-password"})
    assert locked.status_code == 429
