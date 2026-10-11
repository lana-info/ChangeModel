"""Vibix ChangeModel GUI: управление провайдерами и моделями для Codex.

Окно позволяет:
- просматривать список провайдеров и их модели (providers.json);
- добавлять/редактировать/удалять провайдера (в т.ч. менять API-ключ);
- добавлять/удалять модели у провайдера;
- выбирать модель по умолчанию (применяется ко всем чатам Codex через прокси);
- запускать/останавливать локальный прокси;
- сбрасывать к встроенным моделям Codex (Luna).

Запуск: python gui.py (или gui.bat двойным кликом).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import codex_accounts
import generator
import models_data

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR
PROVIDERS_FILE = BASE_DIR / "providers.json"

# Порт прокси: один источник для health-check'ов и кнопок управления.
# Должен совпадать с PROXY_PORT прокси и base_url, который пишет generator.
PROXY_PORT = int(os.environ.get("PROXY_PORT", "4096"))
PROXY_HTTP = f"http://127.0.0.1:{PROXY_PORT}"

APP_VERSION = "1.5.0"
GITHUB_REPO = "lana-info/ChangeModel"
GITHUB_URL = f"https://github.com/{GITHUB_REPO}"
RELEASES_URL = f"{GITHUB_URL}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


# ---------- внешний вид ----------
# Только оформление: шрифты, отступы, акцентная кнопка и индикатор статуса.
# Логика работы окна здесь не меняется.
_FONT_BASE = ("Segoe UI", 10)
_FONT_TITLE = ("Segoe UI", 10, "bold")
_ACCENT_BG = "#2563eb"
_ACCENT_FG = "white"
_OK_COLOR = "#16a34a"
_OFF_COLOR = "#dc2626"
_NEUTRAL_COLOR = "#999999"
_PROVIDER_TINTS = {"opencode-zen": "#e8f0fe", "openrouter": "#e6f4ea"}
_PROVIDER_SHORT = {"opencode-zen": "Zen", "openrouter": "OpenRouter"}


def _apply_visual_theme(root: tk.Tk) -> None:
    """Настраивает внешний вид окна. Безопасно, если темы или шрифта нет."""
    try:
        style = ttk.Style(root)
        available = set(style.theme_names())
        # Только clam: тема vista игнорирует свой background у кнопок,
        # из-за чего белый текст становился нечитаемым на светлой кнопке.
        if "clam" in available:
            style.theme_use("clam")
        style.configure("TButton", padding=(10, 5), font=_FONT_BASE)
        style.configure("TLabel", font=_FONT_BASE)
        style.configure("TCheckbutton", font=_FONT_BASE)
        style.configure("TEntry", padding=(4, 2), font=_FONT_BASE)
        style.configure("TCombobox", padding=(4, 2), font=_FONT_BASE)
        style.configure("Title.TLabel", font=_FONT_TITLE)
        style.configure("Status.TLabel", font=_FONT_BASE)
        style.configure("News.TLabel", font=_FONT_BASE, foreground="#374151")
        style.configure("Accent.TButton", padding=(12, 6))
        style.map(
            "Accent.TButton",
            foreground=[("!disabled", _ACCENT_FG)],
            background=[("!disabled", _ACCENT_BG), ("active", "#1d4ed8"), ("pressed", "#1e40af")],
        )
    except Exception:
        pass


# ---------- вставка в поля ввода ----------
# Две проблемы: 1) в Tkinter нет меню по правой кнопке — добавляем своё
# (Вырезать/Копировать/Вставить/Выделить всё) для всех полей ввода;
# 2) при русской раскладке Ctrl+C/V/X не работают (Tk ищет латинские
# keysym'ы, а клавиши дают Cyrillic_*), поэтому добавляем русские сочетания
# в виртуальные события <<Copy>>/<<Paste>>/<<Cut>> и Ctrl+Ф для выделения.
_ENTRY_RU_KEYS = {
    "<<Copy>>": ("<Control-Cyrillic_es>", "<Control-Cyrillic_ES>"),
    "<<Paste>>": ("<Control-Cyrillic_em>", "<Control-Cyrillic_EM>"),
    "<<Cut>>": ("<Control-Cyrillic_che>", "<Control-Cyrillic_CHE>"),
}


def _entry_context_menu(entry: tk.Widget) -> tk.Menu:
    """Меню по правой кнопке для поля ввода."""
    menu = tk.Menu(entry, tearoff=0)
    menu.add_command(label="Вырезать", command=lambda: entry.event_generate("<<Cut>>"))
    menu.add_command(label="Копировать", command=lambda: entry.event_generate("<<Copy>>"))
    menu.add_command(label="Вставить", command=lambda: entry.event_generate("<<Paste>>"))
    menu.add_separator()
    menu.add_command(
        label="Выделить всё",
        command=lambda: (entry.focus_set(), entry.select_range(0, tk.END)),
    )
    return menu


def _show_entry_menu(event) -> str:
    """Показ меню по правой кнопке. Только удобство, логика не меняется."""
    widget = event.widget
    try:
        widget.focus_set()
    except Exception:
        pass
    try:
        _entry_context_menu(widget).tk_popup(event.x_root, event.y_root)
    except Exception:
        pass
    return "break"


def _fix_entry_keys(root: tk.Tk) -> None:
    """Чинит вставку в поля ввода (меню + русские Ctrl-сочетания). Безопасно."""
    try:
        for virtual, sequences in _ENTRY_RU_KEYS.items():
            for seq in sequences:
                root.event_add(virtual, seq)
        root.bind_class("TEntry", "<Button-3>", _show_entry_menu)
        if sys.platform == "darwin":
            # на macOS правая кнопка — это Button-2
            root.bind_class("TEntry", "<Button-2>", _show_entry_menu)

        def _select_all(event) -> str:
            try:
                event.widget.select_range(0, tk.END)
            except Exception:
                pass
            return "break"

        root.bind_class("TEntry", "<Control-Cyrillic_ef>", _select_all)
        root.bind_class("TEntry", "<Control-Cyrillic_EF>", _select_all)
    except Exception:
        pass


def ensure_data_file() -> None:
    """Если providers.json отсутствует, создаём его из шаблона
    providers.example.json (в exe — из встроенных ресурсов, в исходниках —
    из корня проекта). Работает и в frozen-, и в исходном режиме."""
    if PROVIDERS_FILE.exists():
        return
    if getattr(sys, "frozen", False):
        src = Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "providers.example.json"
    else:
        src = BASE_DIR / "providers.example.json"
    try:
        if src.exists():
            import shutil

            shutil.copy2(src, PROVIDERS_FILE)
        else:
            PROVIDERS_FILE.write_text('{\n  "default_model": "",\n  "providers": []\n}\n', encoding="utf-8")
    except OSError:
        pass


def recover_data_file() -> bool:
    """Восстанавливает нечитаемый providers.json из шаблона; битая копия
    сохраняется рядом (providers.json.broken-<дата-время>)."""
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        PROVIDERS_FILE.replace(PROVIDERS_FILE.with_name(f"providers.json.broken-{stamp}"))
    except OSError:
        pass
    ensure_data_file()
    return PROVIDERS_FILE.exists()


def load_data() -> dict:
    with open(PROVIDERS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict) -> None:
    # Атомарная запись: сбой посреди записи не оставит битый providers.json.
    fd, tmp = tempfile.mkstemp(dir=str(PROVIDERS_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PROVIDERS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------- каталог моделей провайдеров ----------
# Раньше minimax-*/qwen*-max/plus говорили на Anthropic-проводе (messages),
# который прокси не транслирует; сейчас они отвечают по chat/completions
# (проверено живыми запросами), поэтому не блокируем. grok-4.5 и gpt-5.6-luna
# работают по Responses-проводу напрямую и через прокси не добавляются.
_RESPONSES_WIRE = {"grok-4.5", "gpt-5.6-luna"}


def _is_incompatible(model_id: str) -> bool:
    return model_id.lower() in _RESPONSES_WIRE


def _read_env_key(env_key: str) -> str:
    """Ключ провайдера так же, как его видит прокси (get_setting в app.py):
    сначала proxy/.env, затем переменная окружения."""
    if not env_key:
        return ""
    env_file = BASE_DIR / "proxy" / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line.startswith(f"{env_key}="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
        except Exception:
            pass
    return os.environ.get(env_key, "")


def fetch_catalog(provider: dict, wire_filter: bool = True) -> list[dict]:
    """Список моделей провайдера: {id, name, free, price_in, price_out, incompatible}.

    Для opencode-chat используется base_url провайдера (zen/go или zen/v1),
    для passthrough — base_url провайдера (например, OpenRouter). Формат
    OpenAI-совместимый: {"data": [{"id", "name", "pricing": {...}, ...}]}.
    wire_filter=False не помечает модели «несовместимыми» — используется для
    OpenRouter, куда запросы идут без трансляции (провод — забота провайдера).
    """
    base = provider["base_url"].rstrip("/")
    if provider.get("kind") == "opencode-chat" and re.search(r"//(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/v1/?$", base):
        # base_url, указывающий на сам прокси, — конфигурационная петля:
        # каталога моделей там нет, берём настоящий апстрим OpenCode Go.
        base = models_data.UPSTREAM_BASE_URL.rstrip("/")
    url = base + "/models"
    key = _read_env_key(provider.get("env_key", ""))
    headers = {"User-Agent": "Mozilla/5.0 (ChangeModel catalog picker)"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)

    out: list[dict] = []
    for it in data.get("data") or []:
        mid = (it.get("id") or "").strip()
        if not mid:
            continue
        pricing = it.get("pricing") or {}
        price_in = None
        price_out = None
        has_pricing = False
        try:
            p = pricing.get("prompt")
            c = pricing.get("completion")
            if p is not None or c is not None:
                has_pricing = True
                price_in = float(p or 0) * 1_000_000
                price_out = float(c or 0) * 1_000_000
        except (TypeError, ValueError):
            has_pricing, price_in, price_out = False, None, None
        is_free = (
            (has_pricing and price_in == 0 and price_out == 0)
            or (not has_pricing and mid.endswith("-free"))
            or (not has_pricing and provider.get("id") == "opencode")
        )
        out.append(
            {
                "id": mid,
                "name": it.get("name") or mid,
                "free": is_free,
                "price_in": price_in if has_pricing else None,
                "price_out": price_out if has_pricing else None,
                "incompatible": _is_incompatible(mid) if wire_filter else False,
            }
        )
    out.sort(key=lambda m: (m["incompatible"], not m["free"], m["name"].lower()))
    return out


# Источники для «Бесплатные сегодня»: (id провайдера в providers.json, подпись)
FREE_SOURCES = (
    ("opencode-zen", "OpenCode Zen"),
    ("openrouter", "OpenRouter"),
)


def collect_free_items(providers: list[dict], fetcher=None) -> tuple[list[dict], list[str]]:
    """Бесплатные модели из живых каталогов OpenCode Zen и OpenRouter.

    Возвращает (items, errors). Каждый item: {id, name, free, price_in,
    price_out, incompatible, provider_id, source, added}; added=True, если
    модель уже есть в списке пользователя. Ошибки — по одному источнику.
    """
    fetcher = fetcher or fetch_catalog
    by_id = {p.get("id"): p for p in providers}
    added_ids = {m["id"] for p in providers for m in p.get("models", [])}
    items: list[dict] = []
    errors: list[str] = []
    for pid, label in FREE_SOURCES:
        prov = by_id.get(pid)
        if not prov or not prov.get("base_url"):
            errors.append(f"{label}: провайдер не настроен")
            continue
        try:
            catalog = fetcher(prov, wire_filter=(pid != "openrouter"))
        except Exception as e:
            errors.append(f"{label}: {e}")
            continue
        for it in catalog:
            if not it.get("free") or it.get("incompatible"):
                continue
            items.append(
                {
                    "id": it["id"],
                    "name": it["name"],
                    "free": True,
                    "price_in": it.get("price_in"),
                    "price_out": it.get("price_out"),
                    "incompatible": False,
                    "provider_id": pid,
                    "source": label,
                    "added": it["id"] in added_ids,
                }
            )
    return items, errors


def _fmt_price(p: float) -> str:
    return f"{p:.2f}"


# Человекочитаемые названия типов подключения (в providers.json хранится ключ).
KIND_LABELS = {
    "passthrough": "Проброс API (OpenRouter и подобные)",
    "opencode-chat": "OpenCode Chat (формат переводит прокси)",
}
KIND_BY_LABEL = {v: k for k, v in KIND_LABELS.items()}


def _save_env_key(env_key: str, value: str, env_file: Path | None = None) -> None:
    """Сохраняет ключ в proxy/.env: добавляет/заменяет KEY=value,
    при пустом value — удаляет строку. Запись атомарная (tmp + replace)."""
    if not env_key:
        return
    env_file = env_file or (BASE_DIR / "proxy" / ".env")
    lines: list[str] = []
    if env_file.exists():
        try:
            lines = env_file.read_text(encoding="utf-8-sig").splitlines()
        except Exception:
            lines = []
    lines = [line for line in lines if not line.strip().startswith(f"{env_key}=")]
    if value:
        lines.append(f"{env_key}={value}")
    env_file.parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(env_file.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, env_file)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _set_codex_token() -> None:
    """Прописывает CHANGE_MODEL_API_KEY в окружение пользователя: Codex
    требует, чтобы переменная существовала. Прокси токены не проверяет,
    поэтому достаточно постоянного плейсхолдера — настоящие ключи живут
    только в proxy/.env."""
    if not sys.platform.startswith("win"):
        # setx существует только в Windows; на macOS/Linux переменную
        # пользователь задаёт вручную (см. README), здесь просто не мешаем.
        return
    try:
        subprocess.run(
            ["setx", "CHANGE_MODEL_API_KEY", "changemodel-local"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=15,
        )
    except Exception as e:
        try:
            print(f"setx CHANGE_MODEL_API_KEY failed: {e}", file=sys.stderr)
        except Exception:
            pass


def _migrate_env_key(old_key: str, new_key: str, api_key: str) -> None:
    """Сохраняет API-ключ провайдера из диалога редактирования.

    При смене имени переменной (env_key) переносит уже сохранённый ключ под
    новое имя — иначе провайдер молча потерял бы авторизацию (старый ключ
    остался бы под старым именем, а нового не было бы вовсе).
    api_key — новое значение из диалога (пусто = не менять).
    """
    if old_key != new_key:
        value = api_key or _read_env_key(old_key)
        if value and new_key:
            _save_env_key(new_key, value)
        if old_key:
            _save_env_key(old_key, "")  # убрать строку со старым именем
        if value:
            _set_codex_token()
    elif api_key:
        _save_env_key(new_key, api_key)
        _set_codex_token()


class ChangeModelApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title(f"Vibix ChangeModel {APP_VERSION} — модели для Codex")
        root.geometry("1260x780")

        self.providers: list[dict] = []
        self._default_model = ""
        self.free_items: list[dict] = []
        self.free_shown: list[dict] = []
        self._status_thread: threading.Thread | None = None
        self._accounts: list[dict] = []
        self._accounts_thread: threading.Thread | None = None
        self._live_after_id: str | None = None
        ensure_data_file()
        try:
            self.load_providers()
        except Exception as e:
            # Битый providers.json не должен молча убивать окно (в exe
            # console=False traceback никто не увидит) — предлагаем восстановить.
            if not messagebox.askyesno(
                "Vibix ChangeModel",
                f"Файл providers.json повреждён и не читается:\n{e}\n\n"
                "Восстановить его из шаблона?\n(битая копия будет сохранена рядом)",
            ):
                raise SystemExit(1)
            if not recover_data_file():
                messagebox.showerror("Vibix ChangeModel", "Не удалось восстановить providers.json.")
                raise SystemExit(1)
            self.load_providers()

        _apply_visual_theme(root)
        _fix_entry_keys(root)
        self._build_layout()
        try:
            self.refresh_providers()
        except Exception as e:
            # Рисование списков тоже не должно убивать окно (ручная правка
            # providers.json, «не та» схема) — пробуем восстановить из шаблона.
            if not messagebox.askyesno(
                "Vibix ChangeModel",
                f"Не удалось отобразить настройки провайдеров:\n{e}\n\n"
                "Восстановить providers.json из шаблона?\n(битая копия будет сохранена рядом)",
            ):
                raise SystemExit(1)
            if not recover_data_file():
                messagebox.showerror("Vibix ChangeModel", "Не удалось восстановить providers.json.")
                raise SystemExit(1)
            self.refresh_providers()
        self.update_status()
        root.bind("<FocusIn>", self._on_focus_in)
        self.refresh_free_news()

    def _on_focus_in(self, event) -> None:
        # <FocusIn> срабатывает и при переходе фокуса между виджетами окна
        # (bindtags включают toplevel) — обновляем статус только при
        # активизации самого окна, а не на каждый клик по полям.
        if event.widget is self.root:
            self.update_status()

    def on_close(self) -> None:
        """Закрытие окна: останавливаем live-таймер, чтобы тики не дёргали
        мёртвые виджеты и не плодили сетевые запросы после выхода."""
        try:
            self.live_var.set(False)
        except Exception:
            pass
        if self._live_after_id:
            try:
                self.root.after_cancel(self._live_after_id)
            except Exception:
                pass
            self._live_after_id = None
        try:
            self.root.destroy()
        except Exception:
            pass

    def start_proxy(self) -> None:
        """Запускает прокси в фоне, если он ещё не работает."""

        def work() -> None:
            running = False
            try:
                with urllib.request.urlopen(f"{PROXY_HTTP}/healthz", timeout=2) as r:
                    running = r.status == 200
            except Exception:
                pass
            if not running:
                if getattr(sys, "frozen", False):
                    args = [sys.executable, "--proxy"]
                else:
                    args = [sys.executable, str(BASE_DIR / "proxy" / "run_proxy.py")]
                # Убираем переменные _PYI_* / _MEIPASS2: с ними дочерний onefile-
                # процесс переиспользует нашу временную папку _MEI вместо создания
                # своей, и PyInstaller не сможет удалить её при выходе GUI.
                # close_fds=True дополнительно блокирует наследование дескрипторов.
                env = {
                    k: v for k, v in os.environ.items()
                    if not k.startswith("_PYI_") and k != "_MEIPASS2"
                }
                self._ui_call(lambda: self.status_var.set("Запускаю прокси..."))
                subprocess.Popen(
                    args,
                    cwd=BASE_DIR,
                    env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    close_fds=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for _ in range(20):  # ждём готовности до 10 секунд
                    time.sleep(0.5)
                    try:
                        with urllib.request.urlopen(f"{PROXY_HTTP}/healthz", timeout=1) as r:
                            if r.status == 200:
                                running = True
                                break
                    except Exception:
                        pass
            if not running:
                self._ui_call(lambda: self.status_var.set("Не удалось запустить прокси"))
                return
            # «Прокси включен» означает: Codex переключён на модели прокси.
            try:
                ok, message = generator.write_config("changemodel")
            except Exception as e:
                self._ui_call(lambda: self.status_var.set(f"Прокси работает, но Codex не переключён: {e}"))
                return
            if not ok:
                self._ui_call(lambda: self.status_var.set(f"Прокси работает, но Codex не переключён: {message}"))
                return
            self._ui_call(self.update_status)

        threading.Thread(target=work, daemon=True).start()

    def stop_proxy(self) -> None:
        """Останавливает фоновый прокси (в фоне, не блокируя интерфейс)."""

        def work() -> None:
            try:
                with urllib.request.urlopen(f"{PROXY_HTTP}/healthz", timeout=2) as r:
                    if r.status != 200:
                        self._ui_call(lambda: self.status_var.set("Прокси и не запущен"))
                        return
            except Exception:
                self._ui_call(lambda: self.status_var.set("Прокси и не запущен"))
                return

            # 1) Плавная остановка: прокси завершается сам и убирает свою
            #    временную папку _MEI.
            stopped = False
            try:
                req = urllib.request.Request(f"{PROXY_HTTP}/shutdown", data=b"", method="POST")
                with urllib.request.urlopen(req, timeout=2) as r:
                    if r.status == 200:
                        for _ in range(10):
                            time.sleep(0.5)
                            if not self.is_proxy_running():
                                stopped = True
                                break
            except Exception:
                pass

            # 2) Запасной путь: PID-файл, который прокси пишет при старте.
            #    Убиваем только если PID принадлежит нашему процессу.
            #    Только когда плавная остановка не удалась: после успеха
            #    процесс уже мёртв, а его номер мог достаться другому процессу.
            if not stopped:
                try:
                    pid = int((BASE_DIR / "proxy" / "proxy.pid").read_text(encoding="utf-8").strip())
                except Exception:
                    pid = None
                if pid:
                    script = (
                        "$p = Get-Process -Id " + str(pid) + " -ErrorAction SilentlyContinue; "
                        "if ($p -and $p.Id -ne " + str(os.getpid()) + " -and ($p.Name -eq 'ChangeModel' -or $p.Name -like 'python*')) "
                        "{ Stop-Process -Id " + str(pid) + " -Force; 'stopped' } else { 'notfound' }"
                    )
                    try:
                        r = subprocess.run(
                            ["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True, timeout=15,
                        )
                        stopped = "stopped" in (r.stdout or "")
                    except Exception:
                        stopped = False

            # 3) Последний запасной вариант: только наш exe с флагом --proxy или
            #    python с run_proxy.py; символьные классы не дают скрипту сматчить
            #    собственную командную строку.
            if not stopped:
                script = (
                    "Get-CimInstance Win32_Process | Where-Object { "
                    "($_.Name -ieq 'ChangeModel.exe' -and $_.CommandLine -match '(^|\\s)--proxy(\\s|$)') -or "
                    "($_.Name -imatch '^python' -and $_.CommandLine -match 'run[_]proxy\\.py') } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
                )
                try:
                    subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=15)
                except Exception:
                    pass

            # «Прокси выключен» означает: Codex возвращается к встроенным моделям.
            try:
                generator.write_config("base")
            except Exception:
                pass
            self._ui_call(self.update_status)

        self.status_var.set("Останавливаю прокси...")
        threading.Thread(target=work, daemon=True).start()

    # ---------- autostart ----------
    @staticmethod
    def _autostart_command() -> str:
        """Команда запуска прокси при входе в Windows."""
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}" --proxy'
        return f'"{sys.executable}" "{Path(__file__).resolve()}" --proxy'

    def is_autostart_enabled(self) -> bool:
        try:
            r = subprocess.run(
                ["reg", "query", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run", "/v", "ChangeModelProxy"],
                capture_output=True, text=True, timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False

    def toggle_autostart(self) -> None:
        enabled = self.autostart_var.get()
        try:
            if enabled:
                subprocess.run(
                    ["reg", "add", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run", "/v", "ChangeModelProxy",
                     "/t", "REG_SZ", "/d", self._autostart_command(), "/f"],
                    check=True, capture_output=True, timeout=10,
                )
                self.status_var.set("Автозапуск прокси включён")
            else:
                subprocess.run(
                    ["reg", "delete", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run", "/v", "ChangeModelProxy", "/f"],
                    check=True, capture_output=True, timeout=10,
                )
                self.status_var.set("Автозапуск прокси выключен")
        except Exception as e:
            self.autostart_var.set(self.is_autostart_enabled())
            messagebox.showerror("Ошибка", f"Не удалось изменить автозапуск:\n{e}")

    # ---------- data ----------
    def load_providers(self) -> list[dict]:
        data = load_data()
        providers = data.get("providers", [])
        if not isinstance(providers, list):
            providers = []
        # защита от ручной правки providers.json: мусорные записи пропускаем
        self.providers = [p for p in providers if isinstance(p, dict) and p.get("id")]
        self._default_model = data.get("default_model", "") if isinstance(data, dict) else ""
        return self.providers

    def save_providers(self) -> None:
        # Сохраняем поверх загруженного документа, чтобы не затереть
        # посторонние ключи верхнего уровня (base_instructions и др.).
        try:
            data = load_data()
        except Exception:
            data = {}  # файл повреждён — перезаписываем текущим состоянием
        data["default_model"] = self.default_model()
        data["providers"] = self.providers
        save_data(data)

    def default_model(self) -> str:
        return self._default_model

    # ---------- layout ----------
    def _build_layout(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(8, 6))
        toolbar.pack(fill=tk.X)

        row1 = ttk.Frame(toolbar)
        row1.pack(fill=tk.X)
        ttk.Button(row1, text="Запустить прокси", command=self.start_proxy, style="Accent.TButton").pack(side=tk.LEFT)
        ttk.Button(row1, text="Остановить прокси", command=self.stop_proxy).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(row1, text="Сбросить настройки Codex по умолчанию", command=self.apply_base).pack(side=tk.RIGHT)

        row2 = ttk.Frame(toolbar)
        row2.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(row2, text="О программе", command=self.show_about).pack(side=tk.RIGHT)
        self.autostart_var = tk.BooleanVar(value=self.is_autostart_enabled() if sys.platform.startswith("win") else False)
        if sys.platform.startswith("win"):
            ttk.Checkbutton(row2, text="Автозапуск прокси при включении Windows", variable=self.autostart_var, command=self.toggle_autostart).pack(side=tk.LEFT, padx=(12, 0))

        # Режим Codex в одном месте: GPT-подписка (встроенные модели Codex,
        # лимиты выбранного GPT-аккаунта) или сторонние модели через прокси
        # (ключи провайдеров, GPT-аккаунт не используется).
        modebox = ttk.Frame(self.root, padding=(8, 0))
        modebox.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(modebox, text="Режим Codex:", style="Title.TLabel").pack(side=tk.LEFT)
        self.mode_gpt_btn = ttk.Button(modebox, text="GPT-подписка", command=self.set_mode_gpt)
        self.mode_gpt_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.mode_proxy_btn = ttk.Button(modebox, text="Сторонние модели (прокси)", command=self.set_mode_proxy)
        self.mode_proxy_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.mode_var = tk.StringVar()
        ttk.Label(modebox, textvariable=self.mode_var, style="News.TLabel").pack(side=tk.LEFT, padx=(8, 0))

        # Вкладки: GPT-аккаунты отдельно, модели прокси отдельно.
        # Переключатель режима — строкой выше, общий для обеих вкладок.
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        tab_gpt = ttk.Frame(self.notebook, padding=4)
        tab_proxy = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(tab_gpt, text="GPT-аккаунты")
        self.notebook.add(tab_proxy, text="Модели прокси")

        # Три колонки с зафиксированной шириной (без ползунка): провайдеры,
        # модели и бесплатные. Веса задают добор ширины: панель «Бесплатно
        # сегодня» получает больше всех — она почти вдвое шире исходной.
        cols = ttk.Frame(tab_proxy)
        cols.pack(fill=tk.BOTH, expand=True)
        cols.grid_columnconfigure(0, weight=1)
        cols.grid_columnconfigure(1, weight=2)
        cols.grid_columnconfigure(2, weight=4)
        cols.grid_rowconfigure(0, weight=1)

        # left: providers list
        left = ttk.Frame(cols, padding=(0, 0, 6, 0))
        left.grid(row=0, column=0, sticky="nsew")
        ttk.Label(left, text="Провайдеры", style="Title.TLabel").pack(anchor=tk.W, pady=(0, 2))
        self.providers_list = tk.Listbox(left, exportselection=False, font=_FONT_BASE, activestyle="none", selectbackground=_ACCENT_BG, selectforeground=_ACCENT_FG, highlightthickness=1, highlightbackground="#d1d5db", relief="solid", borderwidth=1)
        self.providers_list.pack(fill=tk.BOTH, expand=True)
        self.providers_list.bind("<<ListboxSelect>>", self.on_select_provider)
        btns_l = ttk.Frame(left)
        btns_l.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btns_l, text="Добавить", command=self.add_provider).pack(side=tk.LEFT)
        ttk.Button(btns_l, text="Редактировать", command=self.edit_provider).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btns_l, text="Удалить", command=self.remove_provider).pack(side=tk.LEFT, padx=(6, 0))

        # right: models of selected provider
        right = ttk.Frame(cols, padding=(6, 0, 0, 0))
        right.grid(row=0, column=1, sticky="nsew")
        ttk.Label(right, text="Модели", style="Title.TLabel").pack(anchor=tk.W, pady=(0, 2))
        self.models_list = tk.Listbox(right, exportselection=False, font=_FONT_BASE, activestyle="none", selectbackground=_ACCENT_BG, selectforeground=_ACCENT_FG, highlightthickness=1, highlightbackground="#d1d5db", relief="solid", borderwidth=1)
        self.models_list.pack(fill=tk.BOTH, expand=True)
        # Две строки кнопок вместо одной: так колонка моделей требует меньше
        # ширины, и она перераспределяется в пользу панели бесплатных моделей.
        btns_r = ttk.Frame(right)
        btns_r.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btns_r, text="Добавить", command=self.add_model).pack(side=tk.LEFT)
        ttk.Button(btns_r, text="Редактировать", command=self.edit_model).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btns_r, text="Удалить", command=self.remove_model).pack(side=tk.LEFT, padx=(6, 0))
        btns_r2 = ttk.Frame(right)
        btns_r2.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btns_r2, text="Подобрать", command=self.pick_models).pack(side=tk.LEFT)
        ttk.Button(btns_r2, text="Модель по умолчанию", command=self.set_default_model).pack(side=tk.LEFT, padx=(6, 0))

        # right-most: бесплатные модели видны сразу, без кнопки
        free = ttk.Frame(cols, padding=(6, 0, 0, 0))
        free.grid(row=0, column=2, sticky="nsew")
        ttk.Label(free, text="Бесплатно сегодня", style="Title.TLabel").pack(anchor=tk.W, pady=(0, 2))
        ttk.Label(free, text="Поиск:").pack(anchor=tk.W)
        self.free_search = ttk.Entry(free)
        self.free_search.pack(fill=tk.X, pady=(2, 4))
        self.free_search.bind("<KeyRelease>", lambda _e: self._refresh_free_panel())
        self.free_list = tk.Listbox(free, selectmode=tk.EXTENDED, exportselection=False, font=_FONT_BASE, activestyle="none", selectbackground=_ACCENT_BG, selectforeground=_ACCENT_FG, highlightthickness=1, highlightbackground="#d1d5db", relief="solid", borderwidth=1)
        self.free_list.pack(fill=tk.BOTH, expand=True)
        btns_f = ttk.Frame(free)
        btns_f.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btns_f, text="Добавить", command=self.add_selected_free).pack(side=tk.LEFT)
        ttk.Button(btns_f, text="Обновить", command=self.refresh_free_news).pack(side=tk.RIGHT)

        self._build_accounts_panel(tab_gpt)

        self.status_var = tk.StringVar()
        statusbar = ttk.Frame(self.root)
        statusbar.pack(fill=tk.X, side=tk.BOTTOM)
        self.proxy_dot = tk.Canvas(statusbar, width=14, height=14, highlightthickness=0)
        self.proxy_dot.pack(side=tk.LEFT, padx=(8, 0), pady=4)
        self._proxy_dot_id = self.proxy_dot.create_oval(2, 2, 12, 12, fill=_NEUTRAL_COLOR, outline="")
        self.status = ttk.Label(statusbar, textvariable=self.status_var, relief=tk.SUNKEN, padding=(8, 4), style="Status.TLabel")
        self.status.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # Строка «новостей»: сколько бесплатных моделей доступно сегодня
        self.news_var = tk.StringVar()
        self.news = ttk.Label(self.root, textvariable=self.news_var, padding=(8, 3), style="News.TLabel")
        self.news.pack(fill=tk.X, side=tk.BOTTOM)
        # Минимальный размер окна — по содержимому (кнопки всегда видны целиком).
        self.root.update_idletasks()
        self.root.minsize(self.root.winfo_reqwidth(), 500)
        self.update_status()

    # ---------- режим Codex: GPT-подписка / сторонние модели ----------
    @staticmethod
    def _read_codex_config() -> dict:
        """config.toml Codex: путь берём из codex_accounts (единый источник)."""
        import tomllib
        cfg = codex_accounts.codex_home() / "config.toml"
        try:
            with open(cfg, "rb") as f:
                return tomllib.load(f) or {}
        except (OSError, ValueError):
            return {}

    def current_mode(self) -> str:
        """'proxy' если Codex смотрит на прокси, иначе 'gpt'."""
        if self._read_codex_config().get("model_provider") == "changemodel":
            return "proxy"
        return "gpt"

    def refresh_mode_panel(self) -> None:
        try:
            mode = self.current_mode()
        except Exception:
            mode = "gpt"
        try:
            label, _key = codex_accounts.active_identity()
        except Exception:
            label = ""
        try:
            active_model = self.read_active_model()
        except Exception:
            active_model = None
        if mode == "gpt":
            self.mode_gpt_btn.state(["disabled"])
            self.mode_proxy_btn.state(["!disabled"])
            self.mode_var.set(f"сейчас: GPT ({label or 'аккаунт?'}) — переключение ниже")
        else:
            self.mode_proxy_btn.state(["disabled"])
            self.mode_gpt_btn.state(["!disabled"])
            self.mode_var.set(f"сейчас: прокси ({active_model or '?'}) — GPT-аккаунт не используется")

    def set_mode_gpt(self) -> None:
        """Режим GPT-подписки: встроенные модели Codex, лимиты активного аккаунта."""
        if self._apply_profile("base"):
            self.refresh_mode_panel()
            self.update_status()

    def set_mode_proxy(self) -> None:
        """Режим сторонних моделей: всё через прокси (нужен запущенный прокси)."""
        if self.is_proxy_running():
            if self._apply_profile("changemodel"):
                self.refresh_mode_panel()
                self.update_status()
        else:
            # start_proxy сам применит профиль changemodel после запуска
            self.start_proxy()
            self.root.after(11000, self.refresh_mode_panel)

    # ---------- Codex-аккаунты (личный пул GPT-подписок) ----------
    # Меняется только ~/.codex/auth.json; config.toml, плагины и skills
    # не трогаются. После переключения Codex нужно перезапустить.
    def _build_accounts_panel(self, parent) -> None:
        box = ttk.Frame(parent, padding=(4, 4))
        box.pack(fill=tk.BOTH, expand=True)
        head = ttk.Frame(box)
        head.pack(fill=tk.X)
        ttk.Label(head, text="Codex-аккаунты (GPT-подписки)", style="Title.TLabel").pack(side=tk.LEFT)
        self.accounts_hint = ttk.Label(
            head, text="переключение меняет только auth.json · после — перезапустите Codex",
            style="News.TLabel")
        self.accounts_hint.pack(side=tk.RIGHT)

        self.accounts_list = tk.Listbox(box, height=10, exportselection=False, font=_FONT_BASE,
                                        activestyle="none", selectbackground=_ACCENT_BG,
                                        selectforeground=_ACCENT_FG, highlightthickness=1,
                                        highlightbackground="#d1d5db", relief="solid", borderwidth=1)
        self.accounts_list.pack(fill=tk.BOTH, expand=True, pady=(2, 4))

        row = ttk.Frame(box)
        row.pack(fill=tk.X)
        ttk.Button(row, text="Добавить текущий", command=self.accounts_add_current).pack(side=tk.LEFT)
        ttk.Button(row, text="Импорт из файла…", command=self.accounts_import).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(row, text="Переключить", command=self.accounts_switch).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(row, text="Обновить лимиты", command=self.accounts_refresh_quotas).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(row, text="Продлить вход", command=self.accounts_renew).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(row, text="Удалить", command=self.accounts_remove).pack(side=tk.LEFT, padx=(6, 0))
        self.live_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Live", variable=self.live_var,
                        command=self.accounts_toggle_live).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Label(row, text="каждые").pack(side=tk.LEFT, padx=(6, 2))
        self.live_interval = ttk.Entry(row, width=5)
        self.live_interval.insert(0, "60")
        self.live_interval.pack(side=tk.LEFT)
        ttk.Label(row, text="сек").pack(side=tk.LEFT, padx=(2, 0))
        ttk.Button(row, text="Псевдоним", command=self.accounts_set_alias).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(row, text="Почистить", command=self.accounts_clean).pack(side=tk.LEFT, padx=(6, 0))
        self.refresh_accounts_list()

    @staticmethod
    def _fmt_resets(resets) -> str:
        if resets is None:
            return ""
        try:
            if isinstance(resets, (int, float)) and resets > 0:
                # Маленькие значения — секунды до сброса (resets_in_seconds),
                # большие — epoch. Граница 1e8: сейчас epoch ~1.7e9.
                stamp = time.time() + resets if resets < 1e8 else resets
                return time.strftime("%H:%M", time.localtime(stamp))
            if isinstance(resets, str) and resets:
                return resets[:16].replace("T", " ")
        except Exception:
            pass
        return ""

    def _window_line(self, win: dict) -> str:
        """'5ч: ост. 88% (сброс 14:30)' — подпись по длине окна в секундах."""
        left = max(0.0, 100.0 - float(win["used_percent"]))
        secs = win.get("window_seconds")
        tag = "нед" if isinstance(secs, (int, float)) and secs > 6 * 3600 else "5ч"
        s = f"{tag}: ост. {left:.0f}%"
        r = self._fmt_resets(win.get("resets_at"))
        if r:
            s += f" (сброс {r})"
        return s

    def _account_line(self, acc: dict, active: bool) -> str:
        name = codex_accounts.account_label(acc)
        mark = "● " if active else "○ "
        usage = acc.get("usage") or {}
        if (usage.get("source") or "none") == "none":
            return f"{mark}{name} — лимиты не запрашивались"
        parts = []
        prim = usage.get("primary")
        if isinstance(prim, dict) and prim.get("used_percent") is not None:
            parts.append(self._window_line(prim))
        sec = usage.get("secondary")
        if isinstance(sec, dict) and sec.get("used_percent") is not None:
            parts.append(self._window_line(sec))
        credits = usage.get("credits") or {}
        if isinstance(credits, dict) and credits.get("balance") is not None:
            parts.append(f"кредиты: {credits['balance']:g}")
        rc = usage.get("reset_credits")
        if isinstance(rc, dict):
            rc = rc.get("available_count")
        if isinstance(rc, (int, float)):
            parts.append(f"доп. сбросы: {int(rc)}")
        if acc.get("plan"):
            parts.append(str(acc["plan"]))
        refresh = usage.get("refresh") or {}
        if refresh.get("status") not in ("ok", "not_requested", None):
            err = refresh.get("http_status") or refresh.get("error_code") or "ошибка"
            parts.append(f"[! {err}, показан кэш]")
        body = " · ".join(parts) if parts else "нет данных"
        upd = usage.get("updated_at") or 0
        age = ""
        if upd:
            age = " · замер " + time.strftime("%H:%M", time.localtime(upd))
        return f"{mark}{name} — {body}{age}"

    def refresh_accounts_list(self) -> None:
        try:
            accounts, active = codex_accounts.list_accounts()
        except Exception:
            accounts, active = [], ""
        self._accounts = accounts
        self.accounts_list.delete(0, tk.END)
        for acc in accounts:
            self.accounts_list.insert(tk.END, self._account_line(acc, acc.get("account_key") == active))
        if not accounts:
            self.accounts_list.insert(tk.END, "Пусто — нажмите «Добавить текущий» после codex login")

    def _selected_account_key(self) -> str | None:
        sel = self.accounts_list.curselection()
        if not sel or sel[0] >= len(self._accounts):
            messagebox.showinfo("Codex-аккаунты", "Сначала выберите аккаунт в списке.")
            return None
        return self._accounts[sel[0]].get("account_key", "")

    def accounts_add_current(self) -> None:
        try:
            ok, message = codex_accounts.add_current()
        except Exception as e:
            messagebox.showerror("Codex-аккаунты", str(e))
            return
        if not ok:
            messagebox.showerror("Codex-аккаунты", message)
            return
        self.status_var.set(message)
        self.refresh_accounts_list()

    def accounts_import(self) -> None:
        path = filedialog.askopenfilename(title="Выберите auth.json",
                                          filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")])
        if not path:
            return
        try:
            ok, message = codex_accounts.import_file(path)
        except Exception as e:
            messagebox.showerror("Codex-аккаунты", str(e))
            return
        if not ok:
            messagebox.showerror("Codex-аккаунты", message)
            return
        self.status_var.set(message)
        self.refresh_accounts_list()

    @staticmethod
    def _codex_running() -> list[str]:
        """Имена запущенных процессов Codex (для предупреждения перед свитчем)."""
        found: list[str] = []
        try:
            if sys.platform.startswith("win"):
                r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                   text=True, timeout=10)
                for line in (r.stdout or "").splitlines():
                    name = line.split('",')[0].strip('"').lower()
                    if "codex" in name and name not in found:
                        found.append(name)
            else:
                r = subprocess.run(["pgrep", "-l", "-i", "codex"], capture_output=True,
                                   text=True, timeout=10)
                for line in (r.stdout or "").splitlines():
                    parts = line.split(None, 1)
                    if len(parts) == 2 and parts[1] not in found:
                        found.append(parts[1])
        except Exception:
            pass
        return found

    def accounts_switch(self) -> None:
        key = self._selected_account_key()
        if not key:
            return
        running = self._codex_running()
        if running and not messagebox.askyesno(
                "Codex-аккаунты",
                "Codex сейчас запущен (" + ", ".join(running) + ").\n"
                "Новый аккаунт вступит в силу только после перезапуска.\n\n"
                "Переключить всё равно?"):
            return
        try:
            ok, message = codex_accounts.switch(key)
        except Exception as e:
            messagebox.showerror("Codex-аккаунты", str(e))
            return
        if not ok:
            messagebox.showerror("Codex-аккаунты", message)
            return
        messagebox.showinfo("Codex-аккаунты", message)
        self.status_var.set(message)
        self.refresh_accounts_list()
        try:
            self.refresh_mode_panel()
        except Exception:
            pass
        try:
            if self.current_mode() == "proxy":
                messagebox.showinfo(
                    "Codex-аккаунты",
                    "Аккаунт переключён, но сейчас активен режим прокси —\n"
                    "GPT-аккаунт вступит в силу после перехода в режим «GPT-подписка».")
        except Exception:
            pass

    def accounts_remove(self) -> None:
        key = self._selected_account_key()
        if not key:
            return
        if not messagebox.askyesno("Codex-аккаунты", "Удалить аккаунт из пула?"):
            return
        try:
            ok, message = codex_accounts.remove(key)
        except Exception as e:
            messagebox.showerror("Codex-аккаунты", str(e))
            return
        if not ok:
            messagebox.showerror("Codex-аккаунты", message)
            return
        self.status_var.set(message)
        self.refresh_accounts_list()

    def accounts_set_alias(self) -> None:
        """Удобное имя аккаунта (функция codex_accounts.set_alias)."""
        key = self._selected_account_key()
        if not key:
            return
        current = next((a.get("alias", "") for a in self._accounts
                        if a.get("account_key") == key), "") or ""
        dlg = _TextPrompt(self.root, "Псевдоним аккаунта",
                          "Запоминающееся имя (например «работа» или «личный»):",
                          current)
        alias = dlg.show()
        if alias is None:
            return
        try:
            ok = codex_accounts.set_alias(key, alias.strip())
        except Exception as e:
            messagebox.showerror("Codex-аккаунты", str(e))
            return
        if ok:
            self.status_var.set("Псевдоним сохранён")
            self.refresh_accounts_list()

    def accounts_clean(self) -> None:
        """Чистка бэкапов и записей без слепков (codex_accounts.clean)."""
        try:
            message = codex_accounts.clean()
        except Exception as e:
            messagebox.showerror("Codex-аккаунты", str(e))
            return
        self.status_var.set(f"Почищено: {message}")
        self.refresh_accounts_list()

    def accounts_renew(self) -> None:
        """Продление входа аккаунта через refresh_token — без браузера
        и без повторной проверки номера (endpoint Codex)."""
        key = self._selected_account_key()
        if not key:
            return
        if not messagebox.askyesno(
                "Продлить вход",
                "Продлить вход аккаунта автоматически?\n\n"
                "Обновляются только токены входа (как это делает сам Codex). "
                "Браузер и проверка номера не нужны."):
            return

        def work() -> None:
            try:
                ok, message = codex_accounts.renew_account(key)
            except Exception as e:
                ok, message = False, str(e)
            self._ui_call(lambda: self._after_renew(ok, message))

        self.status_var.set("Продлеваю вход…")
        threading.Thread(target=work, daemon=True).start()

    def _after_renew(self, ok: bool, message: str) -> None:
        if ok:
            self.status_var.set(message)
        else:
            messagebox.showerror("Продлить вход", message)
            self.status_var.set("Не удалось продлить вход")
        self.refresh_accounts_list()
        self.accounts_refresh_quotas()

    def accounts_refresh_quotas(self) -> None:
        if self._accounts_thread and self._accounts_thread.is_alive():
            return

        def work() -> None:
            try:
                codex_accounts.refresh_all()
                try:
                    codex_accounts.refresh_names()
                except Exception:
                    pass
            finally:
                self._ui_call(self.refresh_accounts_list)

        self.status_var.set("Запрашиваю лимиты…")
        self._accounts_thread = threading.Thread(target=work, daemon=True)
        self._accounts_thread.start()

    def accounts_toggle_live(self) -> None:
        if self.live_var.get():
            try:
                interval = max(30, int(self.live_interval.get() or "60"))
            except ValueError:
                interval = 60
                self.live_interval.delete(0, tk.END)
                self.live_interval.insert(0, "60")
            self.status_var.set(f"Live-обновление лимитов каждые {interval} сек")
            self.accounts_refresh_quotas()
            self._schedule_live(interval)
        elif self._live_after_id:
            try:
                self.root.after_cancel(self._live_after_id)
            except Exception:
                pass
            self._live_after_id = None
            self.status_var.set("Live-обновление выключено")

    def _schedule_live(self, interval: int) -> None:
        if not self.live_var.get():
            return

        def tick() -> None:
            self._live_after_id = None
            if not self.live_var.get():
                return
            self.accounts_refresh_quotas()
            self._schedule_live(interval)

        try:
            self._live_after_id = self.root.after(interval * 1000, tick)
        except Exception:
            pass

    # ---------- helpers ----------
    def current_provider_index(self) -> int:
        sel = self.providers_list.curselection()
        return sel[0] if sel else -1

    def current_provider(self) -> dict | None:
        idx = self.current_provider_index()
        if idx < 0 or idx >= len(self.providers):
            return None
        return self.providers[idx]

    def refresh_providers(self) -> None:
        self.load_providers()
        self.providers_list.delete(0, tk.END)
        for p in self.providers:
            # providers.json могут править руками — битые записи не должны
            # ронять окно (прокси в той же ситуации просто пропускает их).
            if not isinstance(p, dict) or not p.get("id"):
                continue
            self.providers_list.insert(tk.END, f"{p.get('name') or p.get('id')}  [{p['id']}]")
        self.refresh_models()
        self.update_status()

    def refresh_models(self) -> None:
        self.models_list.delete(0, tk.END)
        prov = self.current_provider()
        default = self.default_model()
        if prov:
            for m in prov.get("models", []):
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                mid = m["id"]
                marker = " (по умолчанию)" if mid == default else ""
                free = " · бесплатная" if str(mid).lower().endswith(("-free", ":free")) else ""
                self.models_list.insert(tk.END, f"{m.get('name') or mid}  [{mid}]{marker}{free}")

    def on_select_provider(self, _event=None) -> None:
        self.refresh_models()

    def _ui_call(self, fn) -> None:
        """Безопасно выполняет fn в главном потоке (окно может быть уже закрыто)."""
        try:
            self.root.after(0, fn)
        except Exception:
            pass

    def _set_proxy_dot(self, color: str) -> None:
        """Цвет индикатора статуса прокси. Только оформление."""
        try:
            dot = getattr(self, "proxy_dot", None)
            if dot is not None:
                dot.itemconfig(self._proxy_dot_id, fill=color)
        except Exception:
            pass

    def update_status(self) -> None:
        """Обновляет строку статуса в фоне: сетевой запрос не блокирует UI."""

        if self._status_thread and self._status_thread.is_alive():
            return

        def work() -> None:
            proxy_on = self.is_proxy_running()
            try:
                active = self.read_active_model()
            except Exception:
                active = None

            def apply() -> None:
                proxy_text = "Прокси включен" if proxy_on else "Прокси выключен"
                self._set_proxy_dot(_OK_COLOR if proxy_on else _OFF_COLOR)
                if active:
                    self.status_var.set(f"{proxy_text}  |  Активна модель: {active}")
                else:
                    self.status_var.set(f"{proxy_text}  |  Встроенные модели Codex (Luna)")
                try:
                    self.refresh_mode_panel()
                except Exception:
                    pass

            self._ui_call(apply)

        self._status_thread = threading.Thread(target=work, daemon=True)
        self._status_thread.start()

    def is_proxy_running(self) -> bool:
        try:
            with urllib.request.urlopen(f"{PROXY_HTTP}/healthz", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def read_active_model(self) -> str | None:
        d = self._read_codex_config()
        if not d:
            return None
        if d.get("model") == generator.BASE_MODEL and not d.get("model_provider"):
            return None
        return f"{d.get('model_provider', '?')} / {d.get('model')}"

    # ---------- actions ----------
    def _apply_profile(self, profile: str) -> bool:
        try:
            ok, message = generator.write_config(profile)
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return False
        if not ok:
            messagebox.showerror("Ошибка", message)
            return False
        return True

    def apply_base(self) -> None:
        if self._apply_profile("base"):
            self.update_status()

    def add_provider(self) -> None:
        dlg = ProviderDialog(self.root, title="Новый провайдер")
        if not dlg.result:
            return
        prov = dlg.result
        api_key = prov.pop("api_key", "")
        if any(p["id"] == prov["id"] for p in self.providers):
            messagebox.showerror("Vibix ChangeModel", f"Провайдер с id '{prov['id']}' уже есть.")
            return
        self.providers.append(prov)
        self.save_providers()
        if api_key:
            _save_env_key(prov.get("env_key", ""), api_key)
            _set_codex_token()
        idx = next(i for i, p in enumerate(self.providers) if p["id"] == prov["id"])
        self.providers_list.selection_set(idx)
        self.refresh_models()

    def edit_provider(self) -> None:
        prov = self.current_provider()
        if not prov:
            messagebox.showinfo("Vibix ChangeModel", "Сначала выберите провайдера в списке.")
            return
        dlg = ProviderDialog(self.root, title=f"Редактировать провайдера «{prov['name']}»", initial=prov)
        if not dlg.result:
            return
        new = dlg.result
        api_key = new.pop("api_key", "")
        if new["id"] != prov["id"] and any(p["id"] == new["id"] for p in self.providers):
            messagebox.showerror("Vibix ChangeModel", f"Провайдер с id '{new['id']}' уже есть.")
            return
        new["models"] = prov.get("models", [])
        # Мержим поверх старой записи, чтобы не потерять посторонние поля
        # (wire_api, default_model провайдера и т.п.).
        merged = dict(prov)
        merged.update(new)
        idx = self.current_provider_index()
        self.providers[idx] = merged
        self.save_providers()
        _migrate_env_key(prov.get("env_key", ""), merged.get("env_key", ""), api_key)
        self.providers_list.selection_set(idx)
        self.refresh_models()

    def remove_provider(self) -> None:
        prov = self.current_provider()
        if not prov:
            return
        if not messagebox.askyesno("Vibix ChangeModel", f"Удалить провайдера «{prov['name']}»?"):
            return
        ids = {m["id"] for m in prov.get("models", [])}
        self.providers.remove(prov)
        if self.default_model() in ids:
            self._default_model = ""
        self.save_providers()
        self.refresh_providers()

    def add_model(self) -> None:
        prov = self.current_provider()
        if not prov:
            messagebox.showinfo("Vibix ChangeModel", "Сначала выберите провайдера.")
            return
        dlg = ModelDialog(self.root, title="Новая модель")
        if not dlg.result:
            return
        m = dlg.result
        if any(mm["id"] == m["id"] for mm in prov.get("models", [])):
            messagebox.showerror("Vibix ChangeModel", "Такая модель уже есть у этого провайдера.")
            return
        prov.setdefault("models", []).append(m)
        self.save_providers()
        self.refresh_models()

    def edit_model(self) -> None:
        prov = self.current_provider()
        if not prov:
            messagebox.showinfo("Vibix ChangeModel", "Сначала выберите провайдера.")
            return
        sel = self.models_list.curselection()
        if not sel:
            messagebox.showinfo("Vibix ChangeModel", "Сначала выберите модель в списке.")
            return
        models = prov.get("models", [])
        midx = sel[0]
        if midx >= len(models):
            return
        m = models[midx]
        dlg = ModelDialog(self.root, title=f"Редактировать модель «{m['name']}»", initial=m)
        if not dlg.result:
            return
        new = dlg.result
        if any(i != midx and mm["id"] == new["id"] for i, mm in enumerate(models)):
            messagebox.showerror("Vibix ChangeModel", "Такая модель уже есть у этого провайдера.")
            return
        if m["id"] == self.default_model() and new["id"] != m["id"]:
            self._default_model = new["id"]
        models[midx] = new
        self.save_providers()
        self.refresh_models()

    def remove_model(self) -> None:
        prov = self.current_provider()
        if not prov:
            return
        sel = self.models_list.curselection()
        if not sel:
            return
        midx = sel[0]
        models = prov.get("models", [])
        if midx >= len(models):
            return
        m = models[midx]
        if not messagebox.askyesno("Vibix ChangeModel", f"Удалить модель «{m['name']}»?"):
            return
        del models[midx]
        if m["id"] == self.default_model():
            self._default_model = ""
        self.save_providers()
        self.refresh_models()

    def pick_models(self) -> None:
        """Загружает каталог провайдера и добавляет выбранные модели."""
        prov = self.current_provider()
        if not prov:
            messagebox.showinfo("Vibix ChangeModel", "Сначала выберите провайдера.")
            return
        if prov.get("kind") != "opencode-chat" and not prov.get("base_url"):
            messagebox.showerror("Vibix ChangeModel", "У провайдера не указан Base URL.")
            return
        self.status_var.set("Загружаю каталог моделей...")

        def work() -> None:
            try:
                items = fetch_catalog(prov)
            except Exception as e:
                self._ui_call(lambda: (
                    self.status_var.set(""),
                    messagebox.showerror("Vibix ChangeModel", f"Не удалось загрузить каталог:\n{e}"),
                ))
                return

            def show() -> None:
                self.status_var.set("")
                # Провайдера ищем заново: пока грузился каталог, список мог измениться.
                current = next((p for p in self.providers if p.get("id") == prov["id"]), None)
                if current is None:
                    return
                if not items:
                    messagebox.showinfo("Vibix ChangeModel", "Каталог провайдера пуст.")
                    return
                dlg = CatalogPickerDialog(self.root, title=f"Каталог — {current['name']}", items=items)
                if not dlg.result:
                    return
                existing = {m["id"] for m in current.get("models", [])}
                added = 0
                for it in dlg.result:
                    if it["id"] in existing:
                        continue
                    current.setdefault("models", []).append({"id": it["id"], "name": it["name"]})
                    existing.add(it["id"])
                    added += 1
                self.save_providers()
                self.refresh_models()
                self.status_var.set(f"Добавлено моделей: {added}")

            self._ui_call(show)

        threading.Thread(target=work, daemon=True).start()

    # ---------- бесплатные модели («новости») ----------
    def refresh_free_news(self) -> None:
        """Обновляет строку «бесплатно сегодня» в фоне (Zen + OpenRouter)."""

        def work() -> None:
            items, _errors = collect_free_items(self.providers)
            zen = sum(1 for i in items if i["provider_id"] == "opencode-zen")
            orouter = sum(1 for i in items if i["provider_id"] == "openrouter")
            fresh = sum(1 for i in items if not i["added"])

            def apply() -> None:
                self.free_items = items
                self._refresh_free_panel()
                if items:
                    text = f"Бесплатно сегодня: Zen — {zen}, OpenRouter — {orouter}"
                    if fresh:
                        text += f" · новых для вас: {fresh}"
                    text += "  (список справа)"
                    self.news_var.set(text)
                else:
                    self.news_var.set("")

            self._ui_call(apply)

        threading.Thread(target=work, daemon=True).start()

    def _refresh_free_panel(self) -> None:
        """Перерисовывает правую панель бесплатных моделей. Только отображение."""
        try:
            q = self.free_search.get().strip().lower()
            self.free_shown = [
                it for it in self.free_items
                if not q or q in it["id"].lower() or q in str(it.get("name", "")).lower()
            ]
            self.free_list.delete(0, tk.END)
            for it in self.free_shown:
                self.free_list.insert(tk.END, _free_line(it))
            _paint_free_rows(self.free_list, self.free_shown)
        except Exception:
            pass

    def _add_free_items(self, chosen: list[dict]) -> int:
        """Добавляет выбранные free-модели к их провайдерам. Возвращает число добавленных."""
        added = 0
        for it in chosen:
            prov = next((p for p in self.providers if p.get("id") == it.get("provider_id")), None)
            if prov is None:
                continue
            existing = {m["id"] for m in prov.get("models", [])}
            if it["id"] in existing:
                continue
            prov.setdefault("models", []).append({"id": it["id"], "name": it["name"]})
            added += 1
        return added

    def add_selected_free(self) -> None:
        """Кнопка «Добавить» на панели «Бесплатно сегодня»."""
        sel = self.free_list.curselection()
        if not sel:
            messagebox.showinfo("Vibix ChangeModel", "Отметьте модели в списке «Бесплатно сегодня».")
            return
        chosen = [self.free_shown[i] for i in sel if i < len(self.free_shown)]
        if not chosen:
            return
        added = self._add_free_items(chosen)
        if added:
            self.save_providers()
            self.refresh_models()
            self.refresh_free_news()
            self.status_var.set(f"Добавлено моделей: {added}")
        else:
            have = {p.get("id") for p in self.providers}
            if any(it.get("provider_id") not in have for it in chosen):
                messagebox.showinfo("Vibix ChangeModel", "Сначала добавьте нужного провайдера в список слева.")
            else:
                self.status_var.set("Выбранные модели уже добавлены")

    def show_about(self) -> None:
        """Окно «О программе»: версия, ссылки, проверка обновлений."""
        AboutDialog(self.root)

    def set_default_model(self) -> None:
        sel = self.models_list.curselection()
        if not sel:
            messagebox.showinfo("Vibix ChangeModel", "Сначала выберите модель в списке.")
            return
        prov = self.current_provider()
        if not prov:
            return
        models = prov.get("models", [])
        midx = sel[0]
        if midx >= len(models):
            return
        model_id = models[midx]["id"]
        self._default_model = model_id
        self.save_providers()
        if self._apply_profile("changemodel"):
            self.refresh_models()
            self.update_status()


class _TextPrompt:
    """Простой модальный запрос одной строки текста."""

    def __init__(self, parent, title: str, label: str, initial: str = "") -> None:
        self.result: str | None = None
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.grab_set()
        self.top.transient(parent)
        body = ttk.Frame(self.top, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=label).pack(anchor=tk.W)
        self.entry = ttk.Entry(body, width=48)
        self.entry.insert(0, initial or "")
        self.entry.pack(fill=tk.X, pady=(4, 8))
        self.entry.focus_set()
        self.entry.select_range(0, tk.END)
        btns = ttk.Frame(body)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Сохранить", command=self._ok).pack(side=tk.LEFT)
        ttk.Button(btns, text="Отмена", command=self.top.destroy).pack(side=tk.LEFT, padx=(6, 0))
        self.top.bind("<Return>", lambda _e: self._ok())
        self.top.bind("<Escape>", lambda _e: self.top.destroy())

    def _ok(self) -> None:
        self.result = self.entry.get()
        self.top.destroy()

    def _wait(self) -> None:
        self.top.wait_window(self.top)

    def show(self) -> str | None:
        self.top.wait_window(self.top)
        return self.result


class ProviderDialog:
    """Модальное окно создания/редактирования провайдера."""

    def __init__(self, parent: tk.Tk, title: str, initial: dict | None = None) -> None:
        self.result: dict | None = None
        initial = initial or {}
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.grab_set()
        self.top.transient(parent)

        self.entries: dict[str, tk.Entry] = {}
        body = ttk.Frame(self.top, padding=16)
        body.pack(fill=tk.BOTH, expand=True)

        for label, key in [
            ("id (латиницей, без пробелов)", "id"),
            ("Название", "name"),
            ("Base URL (для OpenRouter: https://openrouter.ai/api/v1)", "base_url"),
        ]:
            ttk.Label(body, text=label).pack(anchor=tk.W, pady=(4, 0))
            ent = ttk.Entry(body, width=52)
            ent.pack(fill=tk.X)
            self.entries[key] = ent

        ttk.Label(body, text="Тип подключения").pack(anchor=tk.W, pady=(4, 0))
        kind = initial.get("kind", "passthrough")
        self.kind_var = tk.StringVar(value=KIND_LABELS.get(kind, kind))
        cmb = ttk.Combobox(body, textvariable=self.kind_var, values=list(KIND_LABELS.values()), state="readonly")
        cmb.pack(fill=tk.X)
        cmb.bind("<<ComboboxSelected>>", self._on_kind)
        ttk.Label(
            body,
            text="Проброс — провайдер сам понимает запросы Codex (OpenRouter). "
                 "OpenCode Chat — формат для моделей OpenCode переводит прокси.",
            foreground="#666",
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        for label, key, show in [
            ("Переменная API-ключа (шаблон; обычно оставляйте как есть)", "env_key", ""),
            ("API-ключ — вставьте ключ (Ctrl+V или правая кнопка → «Вставить»; сохранится в proxy/.env)", "api_key", "•"),
        ]:
            ttk.Label(body, text=label).pack(anchor=tk.W, pady=(4, 0))
            ent = ttk.Entry(body, width=52, show=show)
            ent.pack(fill=tk.X)
            self.entries[key] = ent
        saved = _read_env_key(initial.get("env_key", ""))
        if saved:
            hint = f"Ключ сохранён: ••••••••{saved[-4:]} — оставьте поле пустым, чтобы не менять"
        else:
            hint = "Ключ не задан"
        ttk.Label(body, text=hint, foreground="#666", wraplength=520, justify=tk.LEFT).pack(anchor=tk.W)

        self.entries["id"].insert(0, initial.get("id", ""))
        self.entries["name"].insert(0, initial.get("name", ""))
        self.entries["base_url"].insert(0, initial.get("base_url", ""))
        self.entries["env_key"].insert(0, initial.get("env_key", ""))
        self._on_kind()

        btns = ttk.Frame(self.top, padding=16)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Сохранить", command=self.on_ok, style="Accent.TButton").pack(side=tk.RIGHT)
        ttk.Button(btns, text="Отмена", command=self.top.destroy).pack(side=tk.RIGHT, padx=(0, 6))

        self.top.bind("<Return>", lambda _e: self.on_ok())
        self.top.update_idletasks()
        w = max(500, self.top.winfo_reqwidth() + 40)
        x = parent.winfo_rootx() + 80
        y = parent.winfo_rooty() + 80
        self.top.geometry(f"{w}x{self.top.winfo_reqheight()}+{x}+{y}")
        parent.wait_window(self.top)

    def _on_kind(self, _event=None) -> None:
        """Подставляет шаблоны Base URL и переменной ключа, меняя тип подключения."""
        env = self.entries["env_key"]
        cur = env.get().strip()
        if self.kind_var.get() == KIND_LABELS["passthrough"]:
            if cur in ("", "OPENCODE_GO_API_KEY"):
                env.delete(0, tk.END)
                env.insert(0, "OPENROUTER_API_KEY")
            base = self.entries["base_url"]
            if base.get().strip() in ("", f"{PROXY_HTTP}/v1"):
                base.delete(0, tk.END)
                base.insert(0, "https://openrouter.ai/api/v1")
        else:
            if cur in ("", "OPENROUTER_API_KEY"):
                env.delete(0, tk.END)
                env.insert(0, "OPENCODE_GO_API_KEY")

    def on_ok(self) -> None:
        vals = {k: v.get().strip() for k, v in self.entries.items()}
        kind = KIND_BY_LABEL.get(self.kind_var.get(), "")
        if not vals["id"] or not vals["name"]:
            messagebox.showerror("Ошибка", "Заполните id и название.", parent=self.top)
            return
        if " " in vals["id"]:
            messagebox.showerror("Ошибка", "id не должен содержать пробелы.", parent=self.top)
            return
        if not kind:
            messagebox.showerror("Ошибка", "Выберите тип подключения.", parent=self.top)
            return
        self.result = {
            "id": vals["id"],
            "name": vals["name"],
            "kind": kind,
            "base_url": vals["base_url"],
            "env_key": vals["env_key"],
            "api_key": vals["api_key"],
            "models": [],
        }
        self.top.destroy()


class ModelDialog:
    """Модальное окно добавления/редактирования модели."""

    def __init__(self, parent: tk.Tk, title: str, initial: dict | None = None) -> None:
        self.result: dict | None = None
        initial = initial or {}
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.grab_set()
        self.top.transient(parent)

        body = ttk.Frame(self.top, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="ID модели (например, cohere/north-mini-code:free)").pack(anchor=tk.W)
        self.ent_id = ttk.Entry(body, width=52)
        self.ent_id.pack(fill=tk.X)
        ttk.Label(body, text="Название (для отображения)").pack(anchor=tk.W, pady=(8, 0))
        self.ent_name = ttk.Entry(body, width=52)
        self.ent_name.pack(fill=tk.X)
        self.ent_id.insert(0, initial.get("id", ""))
        self.ent_name.insert(0, initial.get("name", ""))

        btns = ttk.Frame(self.top, padding=16)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Сохранить", command=self.on_ok, style="Accent.TButton").pack(side=tk.RIGHT)
        ttk.Button(btns, text="Отмена", command=self.top.destroy).pack(side=tk.RIGHT, padx=(0, 6))

        self.top.bind("<Return>", lambda _e: self.on_ok())
        self.top.update_idletasks()
        w = max(500, self.top.winfo_reqwidth() + 40)
        x = parent.winfo_rootx() + 80
        y = parent.winfo_rooty() + 80
        self.top.geometry(f"{w}x{self.top.winfo_reqheight()}+{x}+{y}")
        parent.wait_window(self.top)

    def on_ok(self) -> None:
        mid = self.ent_id.get().strip()
        name = self.ent_name.get().strip() or mid
        if not mid:
            messagebox.showerror("Ошибка", "Укажите ID модели.", parent=self.top)
            return
        if " " in mid:
            messagebox.showerror("Ошибка", "ID не должен содержать пробелы.", parent=self.top)
            return
        self.result = {"id": mid, "name": name}
        self.top.destroy()


class CatalogPickerDialog:
    """Модальное окно выбора моделей из каталога провайдера."""

    def __init__(self, parent: tk.Tk, title: str, items: list[dict]) -> None:
        self.result: list[dict] | None = None
        self.items = items
        self.filtered: list[dict] = list(items)

        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.grab_set()
        self.top.transient(parent)

        body = ttk.Frame(self.top, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="Поиск:").pack(anchor=tk.W)
        self.ent_search = ttk.Entry(body)
        self.ent_search.pack(fill=tk.X, pady=(2, 6))
        self.ent_search.bind("<KeyRelease>", lambda _e: self._apply_filter())

        self.list = tk.Listbox(body, selectmode=tk.EXTENDED, exportselection=False, height=20, font=_FONT_BASE, activestyle="none", selectbackground=_ACCENT_BG, selectforeground=_ACCENT_FG, highlightthickness=1, highlightbackground="#d1d5db", relief="solid", borderwidth=1)
        self.list.pack(fill=tk.BOTH, expand=True)
        self.count_var = tk.StringVar()
        ttk.Label(body, textvariable=self.count_var).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(
            body,
            text="«Не совместима» — провод, который прокси не транслирует; такие модели не добавляются.",
            foreground="#666",
            wraplength=520,
        ).pack(anchor=tk.W, pady=(2, 0))

        btns = ttk.Frame(self.top, padding=16)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Добавить выбранные", command=self.on_add, style="Accent.TButton").pack(side=tk.RIGHT)
        ttk.Button(btns, text="Отмена", command=self.top.destroy).pack(side=tk.RIGHT, padx=(0, 6))

        self._apply_filter()
        self.top.update_idletasks()
        w = max(620, self.top.winfo_reqwidth() + 40)
        h = min(640, self.top.winfo_reqheight() + 20)
        x = parent.winfo_rootx() + 60
        y = parent.winfo_rooty() + 40
        self.top.geometry(f"{w}x{h}+{x}+{y}")
        self.ent_search.focus_set()
        # Ждём закрытия диалога, иначе результат не будет прочитан.
        parent.wait_window(self.top)

    @staticmethod
    def _line(it: dict) -> str:
        marks = []
        if it["free"]:
            marks.append("free")
        if it["incompatible"]:
            marks.append("не совместима")
        prefix = f"[{'/'.join(marks)}] " if marks else ""
        price = ""
        if it["price_in"] is not None and not it["free"]:
            price = f"  ${_fmt_price(it['price_in'])}/${_fmt_price(it['price_out'])} за 1M"
        return f"{prefix}{it['name']} — {it['id']}{price}"

    def _apply_filter(self) -> None:
        q = self.ent_search.get().strip().lower()
        self.filtered = [
            it for it in self.items
            if not q or q in it["id"].lower() or q in it["name"].lower()
        ]
        self.list.delete(0, tk.END)
        for it in self.filtered:
            self.list.insert(tk.END, self._line(it))
        self.count_var.set(f"Моделей: {len(self.filtered)} из {len(self.items)}")

    def on_add(self) -> None:
        sel = self.list.curselection()
        if not sel:
            messagebox.showinfo("Vibix ChangeModel", "Отметьте модели в списке.", parent=self.top)
            return
        chosen = [self.filtered[i] for i in sel]
        compat = [it for it in chosen if not it["incompatible"]]
        skipped = len(chosen) - len(compat)
        if not compat:
            messagebox.showinfo("Vibix ChangeModel", "Все выбранные модели несовместимы с прокси.", parent=self.top)
            return
        if skipped:
            messagebox.showinfo("Vibix ChangeModel", f"Несовместимые модели пропущены: {skipped}.", parent=self.top)
        self.result = compat
        self.top.destroy()


def _free_line(it: dict) -> str:
    """Строка бесплатной модели: пометка [новая] и короткий тег провайдера."""
    prefix = "[новая] " if not it["added"] else ""
    tag = _PROVIDER_SHORT.get(it.get("provider_id", ""), it.get("source", ""))
    return f"{prefix}[{tag}] {it['name']} — {it['id']}"


def _paint_free_rows(lst: tk.Listbox, items: list[dict]) -> None:
    """Подсветка строк бесплатных моделей: фон по провайдеру
    (Zen — голубой, OpenRouter — зелёный). Новые помечены текстом [новая]."""
    for i, it in enumerate(items):
        tint = _PROVIDER_TINTS.get(it.get("provider_id", ""))
        if not tint:
            continue
        try:
            lst.itemconfig(i, background=tint)
        except Exception:
            pass


def _parse_version(text: str) -> tuple[int, ...]:
    """Версия вида 'v1.2.3' -> (1, 2, 3). Нечисловые части игнорируются."""
    return tuple(int(p) for p in re.findall(r"\d+", text or "")[:3])


def is_newer_version(latest: str, current: str) -> bool:
    """True, если latest новее current. Сравниваются числовые компоненты."""
    def norm(v: tuple[int, ...]) -> tuple[int, int, int]:
        return (v + (0, 0, 0))[:3]
    return norm(_parse_version(latest)) > norm(_parse_version(current))


def _parse_atom_latest(data: str) -> tuple[str | None, str | None]:
    """Разбор ленты releases.atom: (tag, html_url) первой записи."""
    root = ET.fromstring(data)
    ns = "{http://www.w3.org/2005/Atom}"
    entry = root.find(f"{ns}entry")
    if entry is None:
        return None, None
    link = entry.find(f"{ns}link[@rel='alternate']")
    href = (link.get("href") if link is not None else "") or ""
    tag = href.rsplit("/tag/", 1)[-1].strip() if "/tag/" in href else ""
    return (tag or None), (href or None)


def _latest_from_atom(timeout: int = 10) -> tuple[str | None, str | None]:
    """Запасной путь: публичная лента releases.atom (без авторизации)."""
    req = urllib.request.Request(
        f"https://github.com/{GITHUB_REPO}/releases.atom",
        headers={"User-Agent": "ChangeModel"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read().decode("utf-8", "replace")
    return _parse_atom_latest(data)


def fetch_latest_release(timeout: int = 10) -> tuple[str | None, str | None]:
    """(tag, html_url) последнего релиза на GitHub. Ошибки — исключением.

    Сначала API; если он недоступен без авторизации (например, GitHub ещё
    не разнёс смену приватности репозитория), читаем публичную ленту
    releases.atom.
    """
    try:
        req = urllib.request.Request(
            LATEST_RELEASE_API,
            headers={"User-Agent": "Vibix ChangeModel", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        tag = (data.get("tag_name") or "").strip() or None
        url = (data.get("html_url") or "").strip() or None
        if tag:
            return tag, url
    except Exception:
        pass
    tag, url = _latest_from_atom(timeout)
    if tag:
        return tag, url
    raise RuntimeError("не удалось получить данные о релизах")


class AboutDialog:
    """Окно «О программе»: версия, ссылки, проверка обновлений на GitHub."""

    def __init__(self, parent: tk.Tk) -> None:
        self.top = tk.Toplevel(parent)
        self.top.title(f"О программе — Vibix ChangeModel {APP_VERSION}")
        self.top.grab_set()
        self.top.transient(parent)

        body = ttk.Frame(self.top, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=f"Vibix ChangeModel {APP_VERSION}", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(
            body,
            text="Окно управления моделями для Codex и локальный прокси.",
            foreground="#666666",
            wraplength=420,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(body, text="Страница проекта:").pack(anchor=tk.W, pady=(8, 0))
        link = ttk.Label(body, text=GITHUB_URL, foreground="#2563eb", cursor="hand2")
        link.pack(anchor=tk.W)
        link.bind("<Button-1>", lambda _e: webbrowser.open(GITHUB_URL))

        self.update_var = tk.StringVar(value="Нажмите «Проверить обновления», чтобы узнать о новом релизе.")
        ttk.Label(body, textvariable=self.update_var, foreground="#666666", wraplength=420, justify=tk.LEFT).pack(anchor=tk.W, pady=(8, 0))

        btns = ttk.Frame(self.top, padding=16)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Проверить обновления", command=self.check_updates, style="Accent.TButton").pack(side=tk.LEFT)
        self.btn_download = ttk.Button(btns, text="Скачать новую версию", command=self.open_releases, state=tk.DISABLED)
        self.btn_download.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btns, text="Закрыть", command=self.top.destroy).pack(side=tk.RIGHT)

        self.top.bind("<Return>", lambda _e: self.top.destroy())
        self.top.update_idletasks()
        w = max(480, self.top.winfo_reqwidth() + 40)
        x = parent.winfo_rootx() + 100
        y = parent.winfo_rooty() + 100
        self.top.geometry(f"{w}x{self.top.winfo_reqheight()}+{x}+{y}")
        parent.wait_window(self.top)

    def open_releases(self) -> None:
        webbrowser.open(RELEASES_URL)

    def check_updates(self) -> None:
        self.update_var.set("Проверяю обновления...")

        def work() -> None:
            try:
                tag, _url = fetch_latest_release()
            except Exception as e:
                err = f"Не удалось проверить обновления: {e}"
                try:
                    self.top.after(0, lambda: self.update_var.set(err))
                except Exception:
                    pass
                return

            def apply() -> None:
                if not tag:
                    self.update_var.set("Не удалось получить данные о релизах.")
                elif is_newer_version(tag, APP_VERSION):
                    self.update_var.set(f"Доступна новая версия: {tag}. Нажмите «Скачать новую версию».")
                    self.btn_download.config(state=tk.NORMAL)
                else:
                    self.update_var.set(f"У вас последняя версия ({APP_VERSION}).")

            try:
                self.top.after(0, apply)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()


def main() -> None:
    if "--proxy" in sys.argv:
        run_proxy_process()
        return
    root = tk.Tk()
    app = ChangeModelApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


def run_proxy_process() -> None:
    """Режим фонового прокси (используется exe при запуске с --proxy)."""
    import traceback

    log_path = APP_DIR / "changemodel_proxy.log"
    try:
        ensure_data_file()
        # PID-файл для кнопки «Остановить прокси».
        try:
            (APP_DIR / "proxy").mkdir(exist_ok=True)
            (APP_DIR / "proxy" / "proxy.pid").write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
        import uvicorn

        from proxy.app import PORT, app

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=int(os.environ.get("PROXY_PORT", str(PORT))),
            log_level="warning",
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(config)
        app.state.uvicorn_server = server
        server.run()
    except Exception:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()