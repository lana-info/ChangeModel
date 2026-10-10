"""Регрессионные тесты пула Codex-аккаунтов (codex_accounts.py).

Запуск: python test_codex_accounts.py  (или pytest)
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

import codex_accounts as ca


# ---------------------------------------------------------------- helpers

def _jwt(email: str, user_id: str, account_id: str = "") -> str:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    header = b64({"alg": "RS256", "typ": "JWT"})
    payload = {"email": email, "chatgpt_user_id": user_id}
    if account_id:
        payload["https://api.openai.com/auth.chatgpt_account_id"] = account_id
    return f"{header}.{b64(payload)}.sig"


def _jwt_nested(email: str, user_id: str, account_id: str, plan: str = "plus") -> str:
    """Новая форма токена: идентификаторы во вложенном объекте auth (без
    account_id в tokens и без user-claims в корне)."""
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    header = b64({"alg": "RS256", "typ": "JWT"})
    payload = {
        "email": email,
        "sub": f"sub-{user_id}",
        "https://api.openai.com/auth": {
            "chatgpt_user_id": user_id,
            "user_id": user_id,
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": plan,
            "organizations": [],
        },
    }
    return f"{header}.{b64(payload)}.sig"


def _auth_nested(email: str, user: str, account: str) -> dict:
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": _jwt_nested(email, user, account),
            "access_token": f"access-{user}",
            "refresh_token": f"refresh-{user}",
            "account_id": "",
        },
        "last_refresh": "2026-10-09T00:00:00Z",
    }


def _auth(email: str, user: str, account: str) -> dict:
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": _jwt(email, user, account),
            "access_token": f"access-{user}",
            "refresh_token": f"refresh-{user}",
            "account_id": account,
        },
        "last_refresh": "2026-10-09T00:00:00Z",
    }


class _Home:
    """Изолированный CODEX_HOME на время теста."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self._old = os.environ.get("CODEX_HOME")

    def __enter__(self) -> Path:
        os.environ["CODEX_HOME"] = str(self.path)
        return self.path

    def __exit__(self, *args) -> None:
        if self._old is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self._old
        self.tmp.cleanup()


# ---------------------------------------------------------------- tests

def test_add_switch_preserves_config() -> None:
    """Переключение меняет только auth.json: config.toml и плагины целы."""
    with _Home() as home:
        cfg = "[plugins.mine]\nenabled = true\n\n[projects.x]\ntrust_level = \"trusted\"\n"
        (home / "config.toml").write_text(cfg, encoding="utf-8")
        (home / "auth.json").write_text(json.dumps(_auth("a@x.io", "u1", "acc1")), encoding="utf-8")
        ok, _ = ca.add_current()
        assert ok
        (home / "auth.json").write_text(json.dumps(_auth("b@x.io", "u2", "acc2")), encoding="utf-8")
        ok, _ = ca.add_current()
        assert ok

        accounts, _ = ca.list_accounts()
        assert len(accounts) == 2
        key_b = next(a["account_key"] for a in accounts if a["email"] == "b@x.io")
        key_a = next(a["account_key"] for a in accounts if a["email"] == "a@x.io")

        ok, msg = ca.switch(key_a)
        assert ok, msg
        live = json.loads((home / "auth.json").read_text(encoding="utf-8"))
        assert live["tokens"]["account_id"] == "acc1"
        assert (home / "config.toml").read_text(encoding="utf-8") == cfg

        ok, _ = ca.switch(key_b)
        assert ok
        live = json.loads((home / "auth.json").read_text(encoding="utf-8"))
        assert live["tokens"]["account_id"] == "acc2"
        assert (home / "config.toml").read_text(encoding="utf-8") == cfg


def test_backups_capped() -> None:
    """Бэкапов не больше лимита, старые удаляются."""
    with _Home() as home:
        (home / "auth.json").write_text(json.dumps(_auth("a@x.io", "u1", "acc1")), encoding="utf-8")
        ca.add_current()
        (home / "auth.json").write_text(json.dumps(_auth("b@x.io", "u2", "acc2")), encoding="utf-8")
        ca.add_current()
        accounts, _ = ca.list_accounts()
        keys = [a["account_key"] for a in accounts]
        for _ in range(6):
            for k in keys:
                ok, _ = ca.switch(k, keep_backups=3)
                assert ok
        left = sorted((home / "changemodel-accounts" / "backup").glob("auth.json.bak-*"))
        assert len(left) <= 3, [p.name for p in left]


def test_corrupt_snapshot_fails_safe() -> None:
    """Битый слепок: switch отказывает, живой auth.json не трогает."""
    with _Home() as home:
        (home / "auth.json").write_text(json.dumps(_auth("a@x.io", "u1", "acc1")), encoding="utf-8")
        ca.add_current()
        (home / "auth.json").write_text(json.dumps(_auth("b@x.io", "u2", "acc2")), encoding="utf-8")
        ca.add_current()
        accounts, _ = ca.list_accounts()
        key_a = next(a["account_key"] for a in accounts if a["email"] == "a@x.io")
        key_b = next(a["account_key"] for a in accounts if a["email"] == "b@x.io")
        ca.switch(key_a)
        before = (home / "auth.json").read_text(encoding="utf-8")
        (home / "changemodel-accounts" / "snapshots" / ca.snapshot_filename(key_b)).write_text(
            "{broken", encoding="utf-8")
        ok, _ = ca.switch(key_b)
        assert not ok
        assert (home / "auth.json").read_text(encoding="utf-8") == before


def test_remove_active_forbidden() -> None:
    with _Home() as home:
        (home / "auth.json").write_text(json.dumps(_auth("a@x.io", "u1", "acc1")), encoding="utf-8")
        ca.add_current()
        accounts, active = ca.list_accounts()
        assert active
        ok, _ = ca.remove(active)
        assert not ok
        assert len(ca.list_accounts()[0]) == 1


def test_usage_parse_shapes() -> None:
    p1 = {"plan_type": "plus",
          "usage": {"primary_window": {"used_percent": 12.5, "resets_at": 123},
                    "secondary_window": {"used_percent": 3.0, "resets_at": 456}},
          "credits": {"balance": 7}}
    out = ca.parse_usage_payload(p1)
    assert out["primary"]["used_percent"] == 12.5
    assert out["secondary"]["resets_at"] == 456
    assert out["credits"] == {"balance": 7.0}
    assert out["plan"] == "plus"
    # плоский формат
    out2 = ca.parse_usage_payload({"used_percent": 50})
    assert out2["primary"]["used_percent"] == 50
    # мусор
    out3 = ca.parse_usage_payload({"unexpected": 1})
    assert out3["primary"] is None


def test_refresh_failure_keeps_cache() -> None:
    """Сеть упала: refresh=False, старый кэш на месте."""
    with _Home() as home:
        (home / "auth.json").write_text(json.dumps(_auth("a@x.io", "u1", "acc1")), encoding="utf-8")
        ca.add_current()
        accounts, _ = ca.list_accounts()
        key = accounts[0]["account_key"]
        orig = ca._http_get_json
        ca._http_get_json = lambda *a, **k: (None, 503, "http_503")
        try:
            ok, _ = ca.refresh_account(key)
        finally:
            ca._http_get_json = orig
        assert not ok
        usage = ca.load_registry()["accounts"][key]["usage"]
        assert usage["refresh"]["status"] == "http_error"
        assert usage["refresh"]["http_status"] == 503


def test_rebuild_from_snapshots() -> None:
    with _Home() as home:
        (home / "auth.json").write_text(json.dumps(_auth("a@x.io", "u1", "acc1")), encoding="utf-8")
        ca.add_current()
        (home / "changemodel-accounts" / "registry.json").write_text("{broken", encoding="utf-8")
        msg = ca.rebuild()
        assert "1" in msg
        accounts, active = ca.list_accounts()
        assert len(accounts) == 1 and active == accounts[0]["account_key"]


def test_import_from_file() -> None:
    with _Home() as home:
        (home / "auth.json").write_text(json.dumps(_auth("a@x.io", "u1", "acc1")), encoding="utf-8")
        ca.add_current()
        other = home / "other-auth.json"
        other.write_text(json.dumps(_auth("c@x.io", "u3", "acc3")), encoding="utf-8")
        ok, msg = ca.import_file(other, alias="запасной")
        assert ok, msg
        accounts, _ = ca.list_accounts()
        assert len(accounts) == 2
        imported = next(a for a in accounts if a["email"] == "c@x.io")
        assert imported["alias"] == "запасной"


def test_refresh_does_not_clobber_concurrent_switch() -> None:
    """Фоновый refresh не откатывает чужой switch: пишет только usage/plan."""
    with _Home() as home:
        (home / "auth.json").write_text(json.dumps(_auth("a@x.io", "u1", "acc1")), encoding="utf-8")
        ca.add_current()
        (home / "auth.json").write_text(json.dumps(_auth("b@x.io", "u2", "acc2")), encoding="utf-8")
        ca.add_current()
        accounts, _ = ca.list_accounts()
        key_a = next(a["account_key"] for a in accounts if a["email"] == "a@x.io")
        key_b = next(a["account_key"] for a in accounts if a["email"] == "b@x.io")

        payload = {"plan_type": "plus",
                   "usage": {"primary_window": {"used_percent": 10.0, "resets_at": 99}}}
        orig = ca._http_get_json

        def fake(url, access, account, timeout=15.0):
            # имитация гонки: пока летела сеть, пользователь переключился на B
            reg = ca.load_registry()
            reg["active_account_key"] = key_b
            ca.save_registry(reg)
            return payload, 200, None

        ca._http_get_json = fake
        try:
            ok, _ = ca.refresh_account(key_a)
        finally:
            ca._http_get_json = orig
        assert ok
        reg = ca.load_registry()
        assert reg["active_account_key"] == key_b  # switch уцелел
        assert reg["accounts"][key_a]["usage"]["source"] == "api"
        assert reg["accounts"][key_a]["usage"]["primary"]["used_percent"] == 10.0


def test_codex_home_env_used_as_is() -> None:
    """Несуществующий CODEX_HOME не подменяется молча на ~/.codex."""
    old = os.environ.get("CODEX_HOME")
    try:
        os.environ["CODEX_HOME"] = str(Path(tempfile.gettempdir()) / "cm-no-such-dir-xyz")
        assert ca.codex_home().name == "cm-no-such-dir-xyz"
        accounts, active = ca.list_accounts()
        assert accounts == [] and active == ""
    finally:
        if old is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = old


def test_active_identity() -> None:
    """Метка живого auth.json: с псевдонимом из пула и без него."""
    with _Home() as home:
        assert ca.active_identity() == ("", "")
        (home / "auth.json").write_text(json.dumps(_auth("a@x.io", "u1", "acc1")), encoding="utf-8")
        label, key = ca.active_identity()
        assert label == "a@x.io" and key == "u1::acc1"
        ca.add_current()
        ca.set_alias(key, "основной")
        label, key2 = ca.active_identity()
        assert label == "основной" and key2 == key
        (home / "auth.json").write_text("{broken", encoding="utf-8")
        assert ca.active_identity() == ("", "")


def test_nested_claim_shape() -> None:
    """Форма токена с вложенным https://api.openai.com/auth и пустым
    tokens.account_id разбирается, план подхватывается."""
    with _Home() as home:
        (home / "auth.json").write_text(
            json.dumps(_auth_nested("n@x.io", "u9", "acc9")), encoding="utf-8")
        ok, msg = ca.add_current()
        assert ok, msg
        accounts, active = ca.list_accounts()
        assert len(accounts) == 1
        assert accounts[0]["email"] == "n@x.io"
        assert accounts[0]["account_key"] == "u9::acc9"
        assert accounts[0]["plan"] == "plus"
        assert active == "u9::acc9"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\nPassed: {len(fns)}")
