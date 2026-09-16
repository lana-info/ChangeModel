"""ChangeModel config generator: switches ~/.codex/config.toml between profiles.

Only touches the keys this project owns (model, model_provider, model_providers.*)
and preserves everything else (desktop, mcp_servers, plugins, projects, ...).

Profiles:
  base       - built-in Codex subscription (gpt-5.6-luna). Fallback.
  changemodel- all models from providers.json go through the local proxy
               (http://127.0.0.1:4096/v1). The proxy routes each model id to
               its provider (OpenCode Go translation / OpenRouter passthrough).

Usage:
  python generator.py --profile base|changemodel [--config PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

import tomllib

try:
    import tomlkit
except ImportError:
    tomlkit = None

BASE_MODEL = "gpt-5.6-luna"
PROXY_BASE_URL = "http://127.0.0.1:4096/v1"


def project_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


PROVIDERS_FILE = os.path.join(project_dir(), "providers.json")


def load_providers() -> list[dict]:
    with open(PROVIDERS_FILE, encoding="utf-8") as f:
        return json.load(f)["providers"]


def default_model() -> str:
    with open(PROVIDERS_FILE, encoding="utf-8") as f:
        return json.load(f).get("default_model", "")


def default_config_path() -> str:
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(home, "config.toml")


def read_doc(path: str):
    if not os.path.exists(path):
        doc = tomlkit.document()
        doc["model"] = BASE_MODEL
        return doc
    with open(path, "rb") as f:
        return tomlkit.load(f)


def apply_profile(doc, profile: str) -> None:
    if profile == "base":
        doc["model"] = BASE_MODEL
        doc.pop("model_provider", None)
        # Удаляем только свой провайдер; чужие записи model_providers.*
        # (если пользователь настраивал их сам) сохраняем.
        providers = doc.get("model_providers")
        if providers is not None:
            providers.pop("changemodel", None)
            if not providers:
                doc.pop("model_providers", None)
        return

    model = os.environ.get("CHANGE_MODEL_MODEL") or default_model()
    if not model:
        # default_model мог стать пустым (модель по умолчанию удалили в GUI) —
        # берём первую модель первого провайдера, у которого есть модели.
        for p in load_providers():
            if p.get("models"):
                model = p["models"][0]["id"]
                break
    doc["model"] = model or BASE_MODEL
    doc["model_provider"] = "changemodel"
    providers = doc.setdefault("model_providers", tomlkit.table())
    providers.pop("changemodel", None)
    providers["changemodel"] = {
        "name": "Vibix ChangeModel (все провайдеры через прокси)",
        "base_url": PROXY_BASE_URL,
        "env_key": "CHANGE_MODEL_API_KEY",
        "wire_api": "responses",
    }
    return


def validate(text: str) -> None:
    tomllib.loads(text)


def available_profiles() -> list[str]:
    return ["base", "changemodel"]


def write_config(profile: str, config_path: str | None = None) -> tuple[bool, str]:
    """Применяет профиль к конфигу Codex. Возвращает (успех, сообщение).

    Работает и как функция для GUI, и как CLI-инструмент (через main).
    """
    if tomlkit is None:
        return False, "ERROR: tomlkit is required (pip install tomlkit)"

    path = config_path or default_config_path()
    if not os.path.exists(path):
        print(f"WARNING: {path} not found; creating new file", file=sys.stderr)

    backup = path + ".bak"
    if os.path.exists(path) and not os.path.exists(backup):
        shutil.copy2(path, backup)
        print(f"Backup: {backup}", file=sys.stderr)

    doc = read_doc(path)
    apply_profile(doc, profile)
    text = tomlkit.dumps(doc)

    try:
        validate(text)
    except tomllib.TOMLDecodeError as e:
        return False, f"ERROR: generated config is invalid TOML: {e}"

    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise

    lines = [f"Profile '{profile}' applied to {path}", f"model: {doc.get('model')}"]
    if profile != "base":
        lines.append(f"model_provider: {doc.get('model_provider')}")
    return True, "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="ChangeModel config generator")
    parser.add_argument("--profile", required=True, choices=available_profiles())
    parser.add_argument("--config", default=default_config_path())
    args = parser.parse_args()

    ok, message = write_config(args.profile, args.config)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
