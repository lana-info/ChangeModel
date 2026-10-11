"""ChangeModel Codex-accounts: личный пул GPT-аккаунтов для Codex.

Механика — как у известных переключателей (Loongphy/codex-auth,
ndycode/codex-multi-auth): меняется ТОЛЬКО ``~/.codex/auth.json``
(слепок OAuth-токенов активного аккаунта). ``config.toml``, плагины,
skills, MCP и прочие настройки не трогаются — лимиты списываются
с выбранного аккаунта.

Хранилище (в каталоге Codex, рядом с auth.json)::

    <codex_home>/changemodel-accounts/
        registry.json                 # индекс: email/alias/активный/кэш квот
        snapshots/<key>.auth.json     # слепки auth.json (0600 на POSIX)
        backup/auth.json.bak-<метка>  # копии перед переключением (до 10 шт)

Квоты: ``GET https://chatgpt.com/backend-api/wham/usage`` с заголовками
``Authorization: Bearer <access_token>`` и ``ChatGPT-Account-Id``.
Ответ разбирается защитно (формат неофициальный и может меняться);
при ошибке показывается последний кэш с пометкой статуса.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

SCHEMA_VERSION = 1
POOL_DIR_NAME = "changemodel-accounts"
SNAPSHOTS_DIR = "snapshots"
BACKUP_DIR = "backup"
REGISTRY_FILE = "registry.json"
BACKUP_KEEP = 10  # сколько копий auth.json держать в backup/

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
ACCOUNTS_URL = "https://chatgpt.com/backend-api/accounts"
USER_AGENT = "ChangeModel-accounts/1.0"

# Замок на чтение-изменение-запись registry: фоновый refresh квот
# (live-таймер/кнопка) и переключение в главном потоке делят один файл.
# Refresh-пути пишут только свои поля (usage/имена) поверх свежего
# registry, чтобы не откатывать чужой switch/add/remove.
_REG_LOCK = threading.Lock()


# ---------- пути ----------

def codex_home() -> Path:
    """Каталог Codex: CODEX_HOME, иначе ~/.codex (порядок как у codex-auth).

    Значение CODEX_HOME используется как есть, даже если каталога пока нет:
    молча подменять его на ~/.codex опаснее (пул и бэкапы уехали бы не туда,
    куда смотрит пользователь). Подкаталоги пула создаются по требованию.
    """
    env = os.environ.get("CODEX_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex"


def pool_dir(home: Path | None = None) -> Path:
    return (home or codex_home()) / POOL_DIR_NAME


def snapshots_dir(home: Path | None = None) -> Path:
    return pool_dir(home) / SNAPSHOTS_DIR


def backup_dir(home: Path | None = None) -> Path:
    return pool_dir(home) / BACKUP_DIR


def live_auth_path(home: Path | None = None) -> Path:
    return (home or codex_home()) / "auth.json"


# ---------- identity ----------

def _b64url_json(part: str) -> dict | None:
    try:
        padded = part + "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        return None


def _jwt_payload(token: str) -> dict:
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    payload = _b64url_json(token.split(".")[1])
    return payload if isinstance(payload, dict) else {}


def parse_identity(auth: dict) -> dict | None:
    """email/user/account по слепку auth.json. None — слепок непригоден.

    Для API-ключей (OPENAI_API_KEY): ключ ``apikey::<sha8>``.
    Для ChatGPT OAuth: ключ ``user_id::account_id``; email — только для
    отображения, не для идентификации (как у codex-auth).
    """
    if not isinstance(auth, dict):
        return None
    api_key = auth.get("OPENAI_API_KEY")
    if isinstance(api_key, str) and api_key:
        import hashlib

        digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8]
        return {
            "account_key": f"apikey::local::{digest}",
            "email": "",
            "auth_mode": "apikey",
            "chatgpt_account_id": "",
        }
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    id_token = tokens.get("id_token")
    if not isinstance(access, str) or not access:
        return None
    claims = _jwt_payload(id_token) if isinstance(id_token, str) else {}
    # Новые токены кладут идентификаторы во вложенный объект
    # "https://api.openai.com/auth", старые — в корень payload.
    inner = claims.get("https://api.openai.com/auth")
    if not isinstance(inner, dict):
        inner = {}
    account_id = tokens.get("account_id") or ""
    if not isinstance(account_id, str) or not account_id:
        account_id = inner.get("chatgpt_account_id") or ""
    if not isinstance(account_id, str) or not account_id:
        account_id = claims.get("https://api.openai.com/auth.chatgpt_account_id") or ""
    if not isinstance(account_id, str) or not account_id:
        # запасной путь для phone-login файлов: первая организация
        orgs = inner.get("organizations") or claims.get("https://api.openai.com/auth.organizations") or []
        if isinstance(orgs, list):
            default = next(
                (o for o in orgs if isinstance(o, dict) and o.get("is_default") and o.get("id")),
                None,
            )
            first = default or next((o for o in orgs if isinstance(o, dict) and o.get("id")), None)
            if first:
                account_id = first["id"]
    user_id = (
        inner.get("chatgpt_user_id")
        or inner.get("user_id")
        or claims.get("chatgpt_user_id")
        or claims.get("user_id")
        or claims.get("sub")
        or ""
    )
    email = claims.get("email") or auth.get("email") or ""
    if not isinstance(user_id, str) or not user_id or not account_id:
        return None
    plan = inner.get("chatgpt_plan_type") or ""
    return {
        "account_key": f"{user_id}::{account_id}",
        "email": str(email).lower() if isinstance(email, str) else "",
        "auth_mode": str(auth.get("auth_mode") or "chatgpt"),
        "chatgpt_account_id": account_id,
        "plan": str(plan) if isinstance(plan, str) else "",
    }


def snapshot_filename(account_key: str) -> str:
    safe = base64.urlsafe_b64encode(account_key.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{safe}.auth.json"


# ---------- registry ----------

def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if os.name == "posix":
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _blank_registry() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "active_account_key": "",
        "previous_active_account_key": "",
        "accounts": {},
    }


def _blank_usage() -> dict:
    return {
        "source": "none",  # api | local | cache | none
        "updated_at": 0,
        "primary": None,  # {"used_percent", "resets_at"}
        "secondary": None,
        "credits": None,  # {"balance"}
        "reset_credits": None,
        "plan": "",
        "refresh": {"requested": False, "method": None, "status": "not_requested",
                    "http_status": None, "error_code": None},
    }


def load_registry(home: Path | None = None) -> dict:
    path = pool_dir(home) / REGISTRY_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _blank_registry()
    if not isinstance(data, dict):
        return _blank_registry()
    reg = _blank_registry()
    accounts = data.get("accounts")
    if isinstance(accounts, dict):
        for key, acc in accounts.items():
            if isinstance(acc, dict) and isinstance(acc.get("snapshot"), str):
                merged = {"account_key": key, "email": "", "alias": "",
                          "account_name": "", "plan": "", "auth_mode": "chatgpt",
                          "created_at": 0, "last_used_at": 0, "usage": _blank_usage()}
                merged.update(acc)
                if not isinstance(merged.get("usage"), dict):
                    merged["usage"] = _blank_usage()
                reg["accounts"][key] = merged
    for field in ("active_account_key", "previous_active_account_key"):
        val = data.get(field)
        if isinstance(val, str) and val in reg["accounts"]:
            reg[field] = val
    return reg


def save_registry(reg: dict, home: Path | None = None) -> None:
    """Запись registry. Внутренний код должен звать update_registry()."""
    _atomic_write_json(pool_dir(home) / REGISTRY_FILE, reg)


def update_registry(mutate, home: Path | None = None) -> dict:
    """Читает registry, применяет mutate(reg) под замком и записывает.

    Единая точка всех изменений пула: исключает гонку «фоновый refresh квот
    против switch/add/remove в главном потоке GUI». mutate возвращает то,
    что нужно наружу (обычно (ok, message)); возвращает None, если аккаунт
    не найден.
    """
    with _REG_LOCK:
        reg = load_registry(home)
        result = mutate(reg)
        if result is None:
            return reg, None
        save_registry(reg, home)
        return reg, result


def _snapshot_path(account_key: str, home: Path | None = None) -> Path:
    return snapshots_dir(home) / snapshot_filename(account_key)


def _write_snapshot(account_key: str, auth: dict, home: Path | None = None) -> None:
    _atomic_write_json(_snapshot_path(account_key, home), auth)


def list_accounts(home: Path | None = None) -> tuple[list[dict], str]:
    """(аккаунты в порядке добавления, активный ключ). Битые слепки пропускаются."""
    reg = load_registry(home)
    out = []
    for key, acc in reg["accounts"].items():
        if not _snapshot_path(key, home).exists():
            continue  # слепок удалён вручную — запись отлежится до rebuild/clean
        out.append(acc)
    return out, reg.get("active_account_key", "")


# ---------- добавление ----------

def _register_account(reg: dict, ident: dict, auth: dict, alias: str,
                      touch: bool) -> tuple[bool, str]:
    """Добавляет/обновляет запись аккаунта в переданном registry.

    Вызывается под update_registry (замок уже взят). Слепок пишется здесь же.
    """
    key = ident["account_key"]
    now = int(time.time())
    acc = reg["accounts"].get(key)
    if acc is None:
        acc = {"account_key": key, "usage": _blank_usage(), "created_at": now}
    acc.update({"email": ident["email"], "auth_mode": ident["auth_mode"],
                "chatgpt_account_id": ident["chatgpt_account_id"],
                "snapshot": snapshot_filename(key)})
    acc.setdefault("usage", _blank_usage())
    if ident.get("plan") and not acc.get("plan"):
        acc["plan"] = _normalize_plan(ident["plan"])
    if alias:
        acc["alias"] = alias
    if touch:
        acc["last_used_at"] = now
    try:
        _write_snapshot(key, auth)
    except OSError as e:
        return False, f"Не удалось сохранить слепок: {e}"
    reg["accounts"][key] = acc
    if not reg.get("active_account_key"):
        reg["active_account_key"] = key
    label = acc.get("alias") or ident["email"] or key
    return True, label


def add_current(alias: str = "", home: Path | None = None) -> tuple[bool, str]:
    """Слепок активного ~/.codex/auth.json в пул (после `codex login`)."""
    try:
        with open(live_auth_path(home), encoding="utf-8") as f:
            auth = json.load(f)
    except (OSError, ValueError) as e:
        return False, f"Не читается {live_auth_path(home)}: {e}"
    ident = parse_identity(auth)
    if ident is None:
        return False, (
            "Активный auth.json не похож на вход через ChatGPT "
            f"({live_auth_path(home)}): нет идентификаторов аккаунта.\n\n"
            "Войдите командой `codex login` и попробуйте снова —\n"
            "либо добавьте аккаунт через «Импорт из файла»."
        )

    def mutate(reg: dict):
        return _register_account(reg, ident, auth, alias, touch=True)

    _reg, result = update_registry(mutate, home)
    if not result[0]:
        return result
    return True, f"Аккаунт добавлен: {result[1]}"


def import_file(path: str | Path, alias: str = "", home: Path | None = None) -> tuple[bool, str]:
    """Импорт аккаунта из файла auth.json (напр. с другой машины/профиля)."""
    src = Path(path).expanduser()
    try:
        with open(src, encoding="utf-8") as f:
            auth = json.load(f)
    except (OSError, ValueError) as e:
        return False, f"Не читается {src}: {e}"
    ident = parse_identity(auth)
    if ident is None:
        return False, "Файл не похож на auth.json Codex (нет токенов/идентификаторов)."

    def mutate(reg: dict):
        return _register_account(reg, ident, auth, alias, touch=False)

    _reg, result = update_registry(mutate, home)
    if not result[0]:
        return result
    return True, f"Аккаунт импортирован: {result[1]}"


def set_alias(account_key: str, alias: str, home: Path | None = None) -> bool:
    def mutate(reg: dict):
        acc = reg["accounts"].get(account_key)
        if acc is None:
            return None  # не пишем registry, если аккаунт не найден
        acc["alias"] = alias
        return True

    _reg, result = update_registry(mutate, home)
    return result is True


# ---------- переключение ----------

def _backup_sort_key(path: Path):
    """Порядок бэкапов: сначала метка времени, затем номер .1/.2/… .

    Численный суффикс, иначе .1 > .10 > .2 лексикографически и лимит
    хранения режет не самые старые файлы.
    """
    name = path.name
    base, _, tail = name.partition(".")
    try:
        return (base, int(tail) if tail.isdigit() else 0)
    except ValueError:
        return (name, 0)


def _prune_backups(home: Path | None = None, keep: int = BACKUP_KEEP) -> None:
    try:
        files = sorted(backup_dir(home).glob("auth.json.bak-*"), key=_backup_sort_key)
    except OSError:
        return
    for old in files[:-keep] if len(files) > keep else []:
        try:
            old.unlink()
        except OSError:
            pass


def _replace_live_auth(auth: dict, home: Path | None) -> None:
    """Бэкап + атомарная замена живого auth.json. Вызывается под замком."""
    live = live_auth_path(home)
    backup_dir(home).mkdir(parents=True, exist_ok=True)
    if live.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = backup_dir(home) / f"auth.json.bak-{stamp}"
        n = 1
        while dest.exists():
            n += 1
            dest = backup_dir(home) / f"auth.json.bak-{stamp}.{n}"
        shutil.copy2(live, dest)
        if os.name == "posix":
            try:
                os.chmod(dest, 0o600)
            except OSError:
                pass
    # атомарная запись с сохранением режима живого файла
    fd, tmp = tempfile.mkstemp(dir=str(live.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(auth, f, ensure_ascii=False, indent=2)
        if os.name == "posix":
            try:
                mode = live.stat().st_mode if live.exists() else 0o600
                os.chmod(tmp, mode)
            except OSError:
                pass
        os.replace(tmp, live)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def switch(account_key: str, home: Path | None = None,
           keep_backups: int = BACKUP_KEEP) -> tuple[bool, str]:
    """Аккаунт → активный: бэкап живого auth.json, копия слепка на его место.

    Трогает только auth.json; config.toml/плагины/skills не меняются.
    Замена файла и запись registry — под одним замком (update_registry),
    иначе параллельные switch/refresh могут перепутать активный ключ.
    После переключения Codex CLI/App нужно перезапустить — долгоживущие
    процессы держат старый токен в памяти.
    """
    snap = _snapshot_path(account_key, home)
    if not snap.exists():
        return False, "Слепок аккаунта отсутствует (удалён вручную)."
    try:
        with open(snap, encoding="utf-8") as f:
            auth = json.load(f)
    except (OSError, ValueError) as e:
        return False, f"Слепок повреждён: {e}"
    if parse_identity(auth) is None:
        return False, "Слепок повреждён (нет токенов) — удалите и добавьте аккаунт заново."

    def mutate(reg: dict):
        acc = reg["accounts"].get(account_key)
        if acc is None:
            return False, "Аккаунт не найден в пуле."
        try:
            _replace_live_auth(auth, home)
        except OSError as e:
            return False, f"Не удалось переключить auth.json: {e}"
        prev = reg.get("active_account_key", "")
        if prev and prev != account_key:
            reg["previous_active_account_key"] = prev
        reg["active_account_key"] = account_key
        acc["last_used_at"] = int(time.time())
        return True, f"Активен: {acc.get('alias') or acc.get('email') or account_key}. Перезапустите Codex, чтобы вступил в силу."

    _reg, result = update_registry(mutate, home)
    if result[0]:
        _prune_backups(home, keep_backups)
    return result


def remove(account_key: str, home: Path | None = None) -> tuple[bool, str]:
    def mutate(reg: dict):
        if account_key not in reg["accounts"]:
            return False, "Аккаунт не найден в пуле."
        if reg.get("active_account_key") == account_key:
            return False, "Нельзя удалить активный аккаунт — сначала переключитесь на другой."
        del reg["accounts"][account_key]
        if reg.get("previous_active_account_key") == account_key:
            reg["previous_active_account_key"] = ""
        return True, "Аккаунт удалён из пула."

    _reg, result = update_registry(mutate, home)
    if result[0]:
        try:
            _snapshot_path(account_key, home).unlink(missing_ok=True)
        except OSError:
            pass
    return result


# ---------- квоты ----------

def _http_get_json(url: str, access_token: str, account_id: str,
                   timeout: float = 15.0) -> tuple[dict | None, int | None, str | None]:
    """(payload, http_status, error). Токен нигде не логируется."""
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + access_token,
            "ChatGPT-Account-Id": account_id,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r), r.status, None
    except urllib.error.HTTPError as e:
        return None, e.code, f"http_{e.code}"
    except Exception as e:  # сеть/таймаут/битый JSON
        return None, None, type(e).__name__


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _window(raw: dict | None) -> dict | None:
    """Окно лимита из разных вариантов формата ответа.

    Реальный wham/usage (см. тесты Loongphy/codex-auth):
    {"used_percent", "limit_window_seconds", "reset_after_seconds", "reset_at"}.
    resets_at хранит абсолютный epoch ИЛИ относительные секунды —
    _fmt_resets в GUI различает их по величине.
    """
    if not isinstance(raw, dict):
        return None
    used = None
    for key in ("used_percent", "used_percentage", "utilization"):
        if raw.get(key) is not None:
            used = _num(raw.get(key))
            break
    if used is None and raw.get("used") is not None and raw.get("limit"):
        limit = _num(raw.get("limit"))
        used_v = _num(raw.get("used"))
        if limit and used_v is not None:
            used = round(used_v / limit * 100, 1)
    if used is None:
        return None
    resets = None
    for key in ("reset_at", "resets_at", "reset_after_seconds", "resets_in_seconds",
                "reset_time"):
        if raw.get(key) is not None:
            resets = raw.get(key)
            break
    seconds = None
    if raw.get("limit_window_seconds") is not None:
        seconds = _num(raw.get("limit_window_seconds"))
    elif raw.get("window_minutes") is not None:
        minutes = _num(raw.get("window_minutes"))
        seconds = minutes * 60 if minutes is not None else None
    elif raw.get("window_hours") is not None:
        hours = _num(raw.get("window_hours"))
        seconds = hours * 3600 if hours is not None else None
    return {"used_percent": used, "resets_at": resets, "window_seconds": seconds}


def parse_usage_payload(payload: dict) -> dict:
    """Нормализованный слепок квот из сырого ответа wham/usage."""
    out = {"primary": None, "secondary": None, "credits": None,
           "reset_credits": None, "plan": ""}
    if not isinstance(payload, dict):
        return out
    # окна живут в rate_limit (реальный формат), старые варианты — в usage/корне
    rate = payload.get("rate_limit")
    if not isinstance(rate, dict):
        rate = payload.get("usage")
    usage = rate if isinstance(rate, dict) else payload
    primary = None
    for key in ("primary_window", "primary", "five_hour", "rate_limit_window"):
        win = usage.get(key) if isinstance(usage, dict) else None
        primary = _window(win)
        if primary:
            break
    secondary = None
    for key in ("secondary_window", "secondary", "weekly_window", "weekly"):
        win = usage.get(key) if isinstance(usage, dict) else None
        secondary = _window(win)
        if secondary:
            break
    # плоский формат: used_percent прямо в корне usage
    if primary is None and isinstance(usage, dict):
        primary = _window(usage)
    credits = payload.get("credits") or (usage.get("credits") if isinstance(usage, dict) else None)
    balance = None
    if isinstance(credits, dict):
        balance = _num(credits.get("balance"))
    elif credits is not None:
        balance = _num(credits)
    reset_credits = payload.get("reset_credits")
    if reset_credits is None:
        reset_credits = payload.get("rate_limit_reset_credits")
    if isinstance(reset_credits, dict):
        reset_credits = reset_credits.get("available_count")
    out.update({
        "primary": primary,
        "secondary": secondary,
        "credits": {"balance": balance} if balance is not None else None,
        "reset_credits": reset_credits,
        "plan": str(payload.get("plan_type") or payload.get("plan") or ""),
    })
    return out


def _merge_account_fields(account_key: str, home: Path | None,
                          **fields) -> bool:
    """Точечная запись полей аккаунта поверх свежего registry (под замком).

    Refresh-пути меняют только свои поля (usage/plan/имена) и никогда
    не перезаписывают active_account_key чужим устаревшим слепком.
    """
    with _REG_LOCK:
        reg = load_registry(home)
        acc = reg["accounts"].get(account_key)
        if acc is None:
            return False
        acc.update(fields)
        save_registry(reg, home)
    return True


def refresh_account(account_key: str, timeout: float = 15.0,
                    home: Path | None = None) -> tuple[bool, str]:
    """Свежие квоты одного аккаунта по его слепку → кэш в registry."""
    reg = load_registry(home)
    acc = reg["accounts"].get(account_key)
    if acc is None:
        return False, "Аккаунт не найден."
    try:
        with open(_snapshot_path(account_key, home), encoding="utf-8") as f:
            auth = json.load(f)
    except (OSError, ValueError):
        return False, "Слепок недоступен."
    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token") if isinstance(tokens, dict) else None
    account_id = acc.get("chatgpt_account_id") or (tokens.get("account_id") if isinstance(tokens, dict) else "")
    usage = acc.get("usage") if isinstance(acc.get("usage"), dict) else _blank_usage()
    usage["refresh"] = {"requested": True, "method": "api", "status": "error",
                        "http_status": None, "error_code": None}
    plan_update: dict = {}
    if not isinstance(access, str) or not access or not account_id:
        usage["refresh"]["status"] = "missing_auth"
        usage["refresh"]["error_code"] = "MissingAuth"
        _merge_account_fields(account_key, home, usage=usage)
        return False, "Нет токенов для запроса квот."
    payload, status, err = _http_get_json(USAGE_URL, access, account_id, timeout)
    if payload is None:
        usage["refresh"]["status"] = "http_error" if status else "error"
        usage["refresh"]["http_status"] = status
        usage["refresh"]["error_code"] = err
        _merge_account_fields(account_key, home, usage=usage)  # старый кэш остаётся видимым
        if status == 401:
            return False, "401: токен протух — войдите заново (`codex login`) и добавьте аккаунт."
        if status == 403:
            return False, "403: доступ запрещён для этого аккаунта."
        return False, f"Не удалось обновить квоты ({err or status}). Показан кэш."
    parsed = parse_usage_payload(payload)
    usage.update({"source": "api", "updated_at": int(time.time()),
                  "primary": parsed["primary"], "secondary": parsed["secondary"],
                  "credits": parsed["credits"], "reset_credits": parsed["reset_credits"]})
    if parsed["plan"]:
        plan_update = {"plan": _normalize_plan(parsed["plan"])}
    usage["refresh"] = {"requested": True, "method": "api", "status": "ok",
                        "http_status": status or 200, "error_code": None}
    _merge_account_fields(account_key, home, usage=usage, **plan_update)
    return True, "Квоты обновлены."


def refresh_all(timeout: float = 15.0, home: Path | None = None) -> dict[str, tuple[bool, str]]:
    reg = load_registry(home)
    return {key: refresh_account(key, timeout, home) for key in list(reg["accounts"])}


def _normalize_plan(plan: str) -> str:
    p = plan.strip().lower()
    if p in ("team", "self_serve_business_usage_based"):
        return "business"
    if p in ("business", "enterprise_cbp_usage_based", "enterprise", "hc"):
        return "enterprise"
    if p in ("education", "edu"):
        return "edu"
    return plan.strip() or ""


def refresh_names(timeout: float = 15.0, home: Path | None = None) -> None:
    """Имена аккаунтов из /backend-api/accounts (не критично, ошибки молча)."""
    reg = load_registry(home)
    for key, acc in list(reg["accounts"].items()):
        try:
            with open(_snapshot_path(key, home), encoding="utf-8") as f:
                auth = json.load(f)
        except (OSError, ValueError):
            continue
        tokens = auth.get("tokens") or {}
        if not isinstance(tokens, dict):
            continue
        access = tokens.get("access_token")
        account_id = acc.get("chatgpt_account_id") or tokens.get("account_id", "")
        if not access or not account_id:
            continue
        payload, status, _ = _http_get_json(ACCOUNTS_URL, access, account_id, timeout)
        if not isinstance(payload, dict):
            continue
        items = payload.get("items") or payload.get("accounts") or []
        name = None
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("id") == account_id:
                    name = item.get("name")
                    break
            if name is None and items and isinstance(items[0], dict):
                name = items[0].get("name")
        if isinstance(name, str) and name.strip() and acc.get("account_name") != name.strip():
            _merge_account_fields(key, home, account_name=name.strip())


# ---------- обслуживание ----------

def clean(keep_backups: int = BACKUP_KEEP, home: Path | None = None) -> str:
    """Удаляет бэкапы сверх лимита и записи без слепков. Возвращает отчёт.

    Активный ключ перепроверяется по живому auth.json (а не «первый
    попавшийся»), чтобы GUI не показывал активным не тот аккаунт.
    """
    _prune_backups(home, keep_backups)

    def mutate(reg: dict):
        orphaned = [k for k in reg["accounts"] if not _snapshot_path(k, home).exists()]
        for key in orphaned:
            del reg["accounts"][key]
        # активный ключ: сначала живой auth.json, затем сохранённый
        live_key = active_identity(home)[1]
        if live_key and live_key in reg["accounts"]:
            reg["active_account_key"] = live_key
        elif reg.get("active_account_key") not in reg["accounts"]:
            reg["active_account_key"] = next(iter(reg["accounts"]), "")
        return orphaned

    _reg, orphaned = update_registry(mutate, home)
    parts = [f"бэкапов оставлено ≤ {keep_backups}"]
    if orphaned:
        parts.append(f"убрано записей без слепков: {len(orphaned)}")
    return "; ".join(parts)


def rebuild(home: Path | None = None) -> str:
    """Пересобрать registry из слепков на диске (восстановление при битом индексе)."""
    reg = _blank_registry()
    now = int(time.time())
    try:
        snaps = sorted(snapshots_dir(home).glob("*.auth.json"))
    except OSError:
        snaps = []
    for snap in snaps:
        try:
            with open(snap, encoding="utf-8") as f:
                auth = json.load(f)
        except (OSError, ValueError):
            continue
        ident = parse_identity(auth)
        if ident is None:
            continue
        key = ident["account_key"]
        reg["accounts"][key] = {
            "account_key": key, "email": ident["email"], "alias": "",
            "account_name": "", "plan": "", "auth_mode": ident["auth_mode"],
            "chatgpt_account_id": ident["chatgpt_account_id"],
            "snapshot": snap.name, "created_at": now, "last_used_at": 0,
            "usage": _blank_usage(),
        }
    # живой auth.json — тоже кандидат
    try:
        with open(live_auth_path(home), encoding="utf-8") as f:
            live = json.load(f)
        ident = parse_identity(live)
        if ident is not None and ident["account_key"] in reg["accounts"]:
            reg["active_account_key"] = ident["account_key"]
    except (OSError, ValueError):
        pass
    if not reg["active_account_key"] and reg["accounts"]:
        reg["active_account_key"] = next(iter(reg["accounts"]))
    save_registry(reg, home)
    return f"Восстановлено аккаунтов: {len(reg['accounts'])}"


def account_label(acc: dict) -> str:
    return acc.get("alias") or acc.get("account_name") or acc.get("email") or acc.get("account_key", "?")


def active_identity(home: Path | None = None) -> tuple[str, str]:
    """Метка и ключ ЖИВОГО ~/.codex/auth.json (что реально использует Codex).

    Возвращает ("", "") если файл отсутствует/битый. Псевдоним подтягивается
    из пула, если живой аккаунт в нём есть.
    """
    try:
        with open(live_auth_path(home), encoding="utf-8") as f:
            auth = json.load(f)
    except (OSError, ValueError):
        return "", ""
    ident = parse_identity(auth)
    if ident is None:
        return "", ""
    key = ident["account_key"]
    reg = load_registry(home)
    acc = reg["accounts"].get(key)
    if acc is not None:
        return account_label(acc), key
    return ident["email"] or key, key
