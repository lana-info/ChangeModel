"""ChangeModel proxy: единая точка входа Codex -> модели провайдеров.

Codex настраивается на один адрес http://127.0.0.1:4096/v1. Прокси
маршрутизирует каждый запрос к нужному провайдеру по id модели:

  kind == "opencode-chat"  -> трансляция Responses в Chat Completions
                              (OpenCode Go, upstream OPENCODE_GO_BASE_URL)
  kind == "passthrough"    -> проброс Responses-запроса как есть на
                              base_url провайдера (например OpenRouter)

Каталог /v1/models собирается из всех моделей всех провайдеров в
providers.json. display_name вида "Провайдер — Модель".

Endpoints:
  GET  /v1/models        -> Codex-compatible model catalog
  POST /v1/responses     -> Responses API (трансляция или проброс)
  GET  /healthz          -> статус для кнопок GUI

Run: python run_proxy.py
Env: PROXY_PORT (default 4096), OPENCODE_GO_BASE_URL
     (default https://opencode.ai/zen/go/v1).
     API-ключи провайдеров берутся по их env_key из providers.json
     (например OPENCODE_GO_API_KEY, OPENCODE_ZEN_API_KEY, OPENROUTER_API_KEY)
     — из proxy/.env или из переменных окружения.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import models_data
from models_data import _catalog_entry, warm_limits  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("changemodel-proxy")

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # lifespan вместо удалённого в новых версиях FastAPI add_event_handler:
    # работает и на старых, и на новых версиях. warm_limits не блокирует —
    # он лишь запускает фоновый поток прогрева кэша лимитов.
    warm_limits()
    yield
    await _close_http_client()


app = FastAPI(title="ChangeModel proxy", lifespan=_lifespan)

PORT = int(os.environ.get("PROXY_PORT", "4096"))
# Апстрим по умолчанию — единая константа из models_data (ею же пользуется GUI).
OPENCODE_UPSTREAM = os.environ.get("OPENCODE_GO_BASE_URL", models_data.UPSTREAM_BASE_URL)

# Апстримы OpenCode требуют заголовок x-opencode-session для маршрутизации:
# без него Go отвечает 400 MissingSessionID, а free-модели Zen — ошибкой
# «free tier can only be used in OpenCode». Значение может быть любым
# стабильным идентификатором — генерируем один на запуск прокси.
OPENCODE_SESSION = "cm-" + uuid.uuid4().hex

# Бесплатные модели Zen требуют заголовков идентификации клиента
# (User-Agent + x-opencode-*), иначе сервер отвечает 429 FreeUsageLimitError.
KNOWN_FREE_MODELS = {
    "deepseek-v4-flash-free",
    "mimo-v2.5-free",
    "big-pickle",
    "hy3-free",
    "nemotron-3-ultra-free",
    "nemotron-3.5-lightning-free",
    "muse-spark-1.3-contributor-free",
    "ling-3.0-flash-fin-free",
}


def is_free_model(model_id: str) -> bool:
    """True, если модель бесплатная и требует заголовков OpenCode-клиента."""
    if model_id in KNOWN_FREE_MODELS:
        return True
    return model_id.endswith("-free")


def project_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    # app.py лежит в proxy/, корень проекта — на уровень выше
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROVIDERS_FILE = os.path.join(project_dir(), "providers.json")


def _load_env_file(path: Path | None = None) -> None:
    """Подтягивает proxy/.env (KEY=value) в окружение, не перекрывая реальные env."""
    for key, val in _read_env_file(path or Path(project_dir()) / "proxy" / ".env").items():
        os.environ.setdefault(key, val)


def _read_env_file(path: Path) -> dict:
    try:
        # utf-8-sig: терпит BOM, который ставят некоторые редакторы
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except Exception:
        return {}
    data: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        data[key.strip()] = val.strip().strip('"').strip("'")
    return data


# Кэш proxy/.env по (mtime_ns, размер): новые ключи подхватываются без
# перезапуска прокси. Пара значений надёжнее одного mtime: правки в пределах
# одной гранулярности mtime (FAT/сетевые ФС) не пройдут незамеченными.
_env_cache: tuple[tuple[int, int] | None, dict] = (None, {})


def _env_from_file(path: Path | None = None) -> dict:
    global _env_cache
    path = path or Path(project_dir()) / "proxy" / ".env"
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _env_cache[0] == stamp:
        return _env_cache[1]
    data = _read_env_file(path)
    _env_cache = (stamp, data)
    return data


def get_setting(name: str) -> str:
    """Ключ/настройка: proxy/.env имеет приоритет (изменения подхватываются
    живьём), затем переменная окружения."""
    val = _env_from_file().get(name, "")
    if val:
        return val
    return os.environ.get(name, "")


_load_env_file()


# ---------------------------------------------------------------- providers
# Кэш providers.json по (mtime_ns, размер): файл перечитывается только после
# изменения, а не при каждом запросе (_check_auth, find_model, base_instructions).
_data_cache: tuple[tuple[int, int] | None, dict] = (None, {})


def load_data() -> dict:
    global _data_cache
    try:
        st = os.stat(PROVIDERS_FILE)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return _data_cache[1]  # файл недоступен — отдаём последний хороший конфиг
    if _data_cache[0] == stamp and _data_cache[1]:
        return _data_cache[1]
    try:
        with open(PROVIDERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        log.exception("failed to load providers.json")
        return _data_cache[1]
    _data_cache = (stamp, data)
    return data


def load_providers() -> list[dict]:
    return load_data().get("providers", [])


def find_model(model_id: str) -> tuple[dict | None, dict | None]:
    """Возвращает (провайдер, модель) по id модели или (None, None)."""
    for p in load_providers():
        for m in p.get("models", []):
            if isinstance(m, dict) and m.get("id") == model_id:
                return p, m
    return None, None


def _is_self_url(url: str) -> bool:
    """True, если url указывает на сам прокси (петля вместо апстрима)."""
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        return False
    return host in ("127.0.0.1", "localhost", "::1") and port == PORT


def base_instructions() -> str:
    """Глобальная инструкция из providers.json или стандартная."""
    data = load_data()
    inst = data.get("base_instructions", "").strip()
    if inst:
        return inst
    return (
        "You are Codex, an agent that collaborates with the user in their workspace. "
        "You work autonomously to fulfill the user's request, use the provided tools, "
        "and report results concisely."
    )


def catalog_entries() -> list[dict]:
    out = []
    inst = base_instructions()
    for p in load_providers():
        if not isinstance(p, dict):
            continue
        p_id = p.get("id", "")
        provider_hint = models_data.PROVIDER_MAP.get(p_id, p_id)
        for m in p.get("models", []):
            if not isinstance(m, dict) or not m.get("id"):
                continue  # битая запись в providers.json — пропускаем, а не роняем весь каталог
            mid = m["id"]
            name = f"{p.get('name', '?')} — {m.get('name', mid)}" + models_data.vision_mark(mid, provider_hint)
            out.append(_catalog_entry(mid, name, f"{p.get('name', '?')} model", inst, provider_hint))
    return out


# ---------------------------------------------------------------- auth
def _check_auth(request: Request) -> str | None:
    """Прокси слушает только 127.0.0.1, токены не проверяем. Но проверяем
    Host и Origin: страница из интернета может отправить «простой» POST-запрос
    на localhost прямо из браузера (CSRF/DNS-rebinding) — отклоняем.
    Не-браузерные клиенты (Codex CLI, GUI) Origin не шлют — им ничто не мешает."""
    host = (request.headers.get("host") or "").lower()
    if not (
        host.startswith("127.0.0.1:")
        or host.startswith("localhost:")
        or host.startswith("[::1]:")
        or host in ("127.0.0.1", "localhost", "[::1]")
    ):
        return "Invalid Host header"
    # Браузеры всегда прикладывают Origin к кросс-доменному POST; допустим
    # только «сам» прокси, чтобы страница не могла им управлять.
    origin = (request.headers.get("origin") or "").rstrip("/").lower()
    if origin:
        allowed = (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", f"http://[::1]:{PORT}")
        if origin not in allowed:
            return "Invalid Origin header"
    return None


# ---------------------------------------------------------------- catalog
@app.get("/v1/models")
async def models(request: Request):
    err = _check_auth(request)
    if err:
        return JSONResponse({"error": {"type": "authentication_error", "message": err}}, status_code=401)
    # Сборка каталога может сходить в сеть за лимитами (models.dev) — уводим
    # в поток, чтобы не блокировать event loop и активные SSE-стримы.
    return JSONResponse({"models": await asyncio.to_thread(catalog_entries)})


# ---------------------------------------------------------------- http client
# Один общий клиент с keep-alive: без него на каждый запрос создаётся новое
# TCP+TLS соединение с апстримом, что добавляет сотни миллисекунд задержки.
_http_client: httpx.AsyncClient | None = None


def http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(600.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
            # Браузерный User-Agent: иначе Cloudflare у части провайдеров
            # блокирует запросы по подписи клиента (ошибка 1010).
            headers={"User-Agent": "Mozilla/5.0 (ChangeModel proxy)"},
        )
    return _http_client


async def _close_http_client() -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


# ---------------------------------------------------------------- passthrough
async def _passthrough(url: str, key: str, body: dict) -> JSONResponse | StreamingResponse:
    """Проброс Responses-запроса на сторонний сервис без изменений."""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    client = http_client()

    if body.get("stream"):
        # Открываем апстрим до создания ответа, чтобы отдать клиенту
        # настоящий HTTP-статус ошибки, а не «SSE с ошибкой» со статусом 200.
        req = client.build_request("POST", url, headers=headers, json=body)
        try:
            up = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            # сеть до апстрима недоступна — JSON-ошибка вместо HTML-500
            return JSONResponse(_error_body(502, f"Upstream request failed: {e}"), status_code=502)
        if up.status_code != 200:
            raw = await up.aread()
            await up.aclose()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = _error_body(up.status_code, raw.decode("utf-8", "replace")[:500], code=str(up.status_code))
            return JSONResponse(payload, status_code=up.status_code)

        async def gen() -> AsyncIterator[bytes]:
            # httpx.Response не поддерживает «async with» (падало с TypeError
            # и 500 посреди стрима) — закрываем соединение вручную.
            try:
                async for chunk in up.aiter_bytes():
                    yield chunk
            finally:
                await up.aclose()

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as e:
        return JSONResponse(_error_body(502, f"Upstream request failed: {e}"), status_code=502)
    try:
        payload = resp.json()
    except Exception:
        payload = {"error": {"type": "invalid_request_error", "message": resp.text[:500], "code": str(resp.status_code), "param": None}}
    return JSONResponse(payload, status_code=resp.status_code)


# ---------------------------------------------------------------- Responses -> Chat
def _content_text(parts) -> str:
    out = []
    for p in parts or []:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict) and isinstance(p.get("text"), str):
            # любые text-части Responses API: input_text, output_text, text, ...
            out.append(p["text"])
    return "".join(out)


def _tool_output_to_text(output) -> str:
    """Codex отправляет output функции как строку ИЛИ массивом content-частей
    ([{"type": "input_text", "text": ...}, ...]). Апстрим (GLM/Zhipu, код 1214)
    принимает в tool-сообщении только строку — приводим всё к строке."""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        t = output.get("text")
        return t if isinstance(t, str) else json.dumps(output, ensure_ascii=False)
    if isinstance(output, list):
        return "".join(_tool_output_to_text(p) for p in output)
    return str(output)


def _message_content(parts):
    """Контент message-элемента для chat-апстрима.

    Без картинок возвращаем строку (максимально совместимо с GLM/Zhipu).
    При наличии input_image возвращаем массив chat-частей: текст + image_url
    (data-URI или http-URL; Codex присылает плоский image_url, OpenAI-формат
    требует вложенный {"image_url": {"url": ...}}).
    """
    chat_parts = []
    has_image = False
    for p in parts or []:
        if isinstance(p, str):
            chat_parts.append({"type": "text", "text": p})
        elif isinstance(p, dict):
            if p.get("type") == "input_image":
                url = p.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                if isinstance(url, str) and url:
                    has_image = True
                    chat_parts.append({"type": "image_url", "image_url": {"url": url}})
            elif isinstance(p.get("text"), str):
                chat_parts.append({"type": "text", "text": p["text"]})
    if not has_image:
        return _content_text(parts)
    return chat_parts


def _input_to_messages(input_items) -> list[dict]:
    messages = []
    # input может быть простой строкой (простое сообщение пользователя)...
    if isinstance(input_items, str):
        if input_items.strip():
            messages.append({"role": "user", "content": input_items})
        return messages
    # ...или списком элементов, где элемент тоже может быть строкой.
    for item in input_items or []:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue  # мусор в input — пропускаем, а не падаем с 500
        itype = item.get("type", "message")
        if itype == "message":
            role = item.get("role", "user")
            if role == "developer" or role == "system":
                role = "system"
            messages.append({"role": role, "content": _message_content(item.get("content"))})
        elif itype == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": item.get("call_id") or item.get("id"),
                            "type": "function",
                            "function": {
                                "name": item.get("name") or "",
                                "arguments": item.get("arguments") or "{}",
                            },
                        }
                    ],
                }
            )
        elif itype == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id", ""),
                    "content": _tool_output_to_text(item.get("output", "")),
                }
            )
    return messages


def _tools_to_chat(tools: list) -> list[dict]:
    out = []
    for t in tools or []:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        fn = {"name": t.get("name", ""), "description": t.get("description", ""), "parameters": t.get("parameters", {"type": "object"})}
        out.append({"type": "function", "function": fn})
    return out


def _tool_choice_to_chat(tool_choice) -> dict | str | None:
    if tool_choice is None or tool_choice == "auto":
        return "auto"
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function")
        name = tool_choice.get("name") or (fn.get("name") if isinstance(fn, dict) else None)
        if name:
            return {"type": "function", "function": {"name": name}}
    return "auto"


def _responses_to_chat(body: dict) -> dict:
    messages = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})
    messages.extend(_input_to_messages(body.get("input")))

    chat: dict = {"model": body.get("model"), "messages": messages}
    tools = _tools_to_chat(body.get("tools"))
    if tools:
        chat["tools"] = tools
        # tool_choice без непустого tools апстримы отвергают
        # («tool_choice requires a non-empty tools list»).
        chat["tool_choice"] = _tool_choice_to_chat(body.get("tool_choice"))
    if body.get("parallel_tool_calls") is not None:
        chat["parallel_tool_calls"] = bool(body["parallel_tool_calls"])
    if body.get("max_output_tokens") is not None:
        chat["max_tokens"] = body["max_output_tokens"]
    for k in ("temperature", "top_p", "stop", "n"):
        if body.get(k) is not None:
            chat[k] = body[k]
    chat["stream"] = bool(body.get("stream", False))
    if chat["stream"]:
        # просим апстрим прислать расход токенов отдельным кадром после
        # finish (OpenAI-совместимые отдают usage в стриме только с этим флагом)
        chat["stream_options"] = {"include_usage": True}
    return chat


# ---------------------------------------------------------------- Chat -> Responses items
def _chat_message_to_items(msg: dict) -> tuple[list[dict], str]:
    """Convert a non-streaming chat message to Responses output items."""
    items = []
    text = msg.get("content") or ""
    if text:
        items.append(
            {
                "id": "msg_" + uuid.uuid4().hex[:16],
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        if not isinstance(fn, dict):
            fn = {}
        call_id = tc.get("id") or "call_" + uuid.uuid4().hex[:16]
        items.append(
            {
                "id": call_id,
                "type": "function_call",
                "status": "completed",
                "name": fn.get("name", ""),
                "arguments": fn.get("arguments", "{}"),
                "call_id": call_id,
            }
        )
    return items, text


def _usage_to_responses(usage: dict | None) -> dict | None:
    if not usage:
        return None
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def _error_body(status: int, message: str, code: str | None = None) -> dict:
    return {"error": {"type": "invalid_request_error", "code": code, "message": message, "param": None}}


# ---------------------------------------------------------------- non-streaming (opencode-chat)
def _upstream_headers(key: str, model_id: str = "") -> dict:
    """Заголовки для апстрима; без ключа Authorization не отправляем вовсе
    (пустой 'Bearer ' — невалидный заголовок для httpx). x-opencode-session
    нужен апстримам OpenCode для маршрутизации (см. OPENCODE_SESSION).
    Для бесплатных моделей добавляем заголовки идентификации клиента,
    иначе сервер отвечает 429 FreeUsageLimitError."""
    headers: dict = {"x-opencode-session": OPENCODE_SESSION}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if is_free_model(model_id):
        headers["User-Agent"] = "opencode-go-proxy/1.0"
        headers["x-opencode-client"] = "cli"
        headers["x-opencode-project"] = "global"
    return headers


async def _handle_non_stream(chat: dict, upstream: str, key: str, model_id: str = "") -> JSONResponse:
    try:
        resp = await http_client().post(
            f"{upstream}/chat/completions",
            headers=_upstream_headers(key, model_id),
            json=chat,
        )
    except httpx.HTTPError as e:
        # сеть до апстрима недоступна — JSON-ошибка вместо HTML-500 от FastAPI
        return JSONResponse(_error_body(502, f"Upstream request failed: {e}"), status_code=502)
    if resp.status_code != 200:
        try:
            up = resp.json()
        except Exception:
            up = {}
        msg = (up.get("error") or {}).get("message") or resp.text[:500]
        return JSONResponse(_error_body(resp.status_code, msg), status_code=resp.status_code)

    try:
        data = resp.json()
    except Exception:
        return JSONResponse(_error_body(502, "Upstream returned non-JSON response"), status_code=502)
    choice = (data.get("choices") or [{}])[0]
    items, text = _chat_message_to_items(choice.get("message") or {})
    response = {
        "id": "resp_" + uuid.uuid4().hex[:24],
        "object": "response",
        "status": "completed",
        "model": chat.get("model"),
        "output": items,
        "output_text": text,
        "usage": _usage_to_responses(data.get("usage")),
    }
    return JSONResponse(response)


# ---------------------------------------------------------------- streaming (opencode-chat)
@asynccontextmanager
async def _open_stream(up: httpx.Response):
    """Контекст-менеджер для ответа, открытого через send(stream=True)."""
    try:
        yield up
    finally:
        await up.aclose()


async def _handle_stream(chat: dict, upstream: str, key: str, model_id: str = "") -> StreamingResponse | JSONResponse:
    # Открываем апстрим до создания ответа: при ошибке отдаём честный
    # HTTP-статус с текстом, а не «поток с ошибкой» со статусом 200.
    req = http_client().build_request(
        "POST",
        f"{upstream}/chat/completions",
        headers=_upstream_headers(key, model_id),
        json=chat,
    )
    try:
        up = await http_client().send(req, stream=True)
    except httpx.HTTPError as e:
        return JSONResponse(_error_body(502, f"Upstream request failed: {e}"), status_code=502)
    if up.status_code != 200:
        raw = await up.aread()
        await up.aclose()
        try:
            up_json = json.loads(raw)
            msg = (up_json.get("error") or {}).get("message") or raw.decode("utf-8", "replace")
        except Exception:
            msg = raw.decode("utf-8", "replace")
        return JSONResponse(_error_body(up.status_code, str(msg)[:2000]), status_code=up.status_code)

    async def gen() -> AsyncIterator[str]:
        async with _open_stream(up):
            response_id = "resp_" + uuid.uuid4().hex[:24]
            model = chat.get("model")
            text_parts: list[str] = []
            output_items: list[dict] = []
            item_index: dict[str, int] = {}
            tool_state: dict[int, dict] = {}
            msg_item_id = "msg_" + uuid.uuid4().hex[:16]
            msg_open = False
            content_open = False
            output_index = 0
            usage = None
            finished = False

            def base_resp(status: str) -> dict:
                return {"id": response_id, "object": "response", "status": status, "model": model, "output": [], "output_text": ""}

            yield _event({"type": "response.created", "response": base_resp("in_progress")})
            yield _event({"type": "response.in_progress", "response": base_resp("in_progress")})

            # Обрыв соединения с апстримом посреди стрима не должен рвать
            # SSE молча: оборачиваем итератор, чтобы отдать клиенту
            # response.failed с частичным результатом (см. ниже).
            stream_error: Exception | None = None

            async def safe_lines() -> AsyncIterator[str]:
                nonlocal stream_error
                try:
                    async for line in up.aiter_lines():
                        yield line
                except Exception as e:
                    log.warning("upstream stream interrupted: %s", e)
                    stream_error = e

            # SSE-событие может быть разбито на несколько строк data: —
            # по спецификации их значения склеиваются через \n.
            pending = ""
            async for line in safe_lines():
                if not line.startswith("data:"):
                    if line == "":
                        pending = ""
                    continue
                payload = line[len("data:"):].strip()
                if pending:
                    payload = pending + "\n" + payload
                    pending = ""
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except Exception:
                    # возможно, JSON продолжается на следующей строке data:
                    pending = payload
                    continue

                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                if finished:
                    # после finish интересен только кадр с usage (он идёт
                    # отдельным чанком с пустым choices и ловится выше)
                    continue
                first = choices[0] if isinstance(choices[0], dict) else {}
                delta = first.get("delta") or {}

                for tc in delta.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue  # битый чанк апстрима — пропускаем, стрим не рвём
                    tfn = tc.get("function")
                    if not isinstance(tfn, dict):
                        tfn = {}
                    idx = tc.get("index", 0)
                    st = tool_state.get(idx)
                    if st is None:
                        call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:16]}"
                        name = tfn.get("name", "")
                        st = {"call_id": call_id, "name": name, "args": ""}
                        tool_state[idx] = st
                        item = {
                            "id": call_id,
                            "type": "function_call",
                            "status": "in_progress",
                            "name": name,
                            "arguments": "",
                            "call_id": call_id,
                        }
                        output_items.append(item)
                        item_index[call_id] = len(output_items) - 1
                        yield _event({"type": "response.output_item.added", "output_index": output_index, "item": item})
                        yield _event(
                            {
                                "type": "response.function_call_arguments.delta",
                                "item_id": call_id,
                                "output_index": output_index,
                                "delta": "",
                            }
                        )
                        output_index += 1
                    else:
                        call_id = st["call_id"]
                    arg_delta = tfn.get("arguments") or ""
                    if arg_delta:
                        st["args"] += arg_delta
                        yield _event(
                            {
                                "type": "response.function_call_arguments.delta",
                                "item_id": call_id,
                                "output_index": item_index[call_id],
                                "delta": arg_delta,
                            }
                        )

                content = delta.get("content")
                if content is not None and content != "":
                    if not msg_open:
                        item = {
                            "id": msg_item_id,
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        }
                        output_items.append(item)
                        item_index[msg_item_id] = len(output_items) - 1
                        yield _event({"type": "response.output_item.added", "output_index": output_index, "item": item})
                        output_index += 1
                        msg_open = True
                    if not content_open:
                        yield _event(
                            {
                                "type": "response.content_part.added",
                                "item_id": msg_item_id,
                                "output_index": item_index[msg_item_id],
                                "content_index": 0,
                                "part": {"type": "output_text", "text": ""},
                            }
                        )
                        content_open = True
                    text_parts.append(content)
                    yield _event(
                        {
                            "type": "response.output_text.delta",
                            "item_id": msg_item_id,
                            "output_index": item_index[msg_item_id],
                            "content_index": 0,
                            "delta": content,
                        }
                    )

                finish = first.get("finish_reason")
                if finish:
                    finished = True
                    # message-item берём из output_items по его индексу:
                    # локальная переменная item могла быть перезаписана
                    # созданием tool-item (текст, затем вызов инструмента).
                    if msg_open:
                        msg_item = output_items[item_index[msg_item_id]]
                        if content_open:
                            full_text = "".join(text_parts)
                            yield _event(
                                {"type": "response.output_text.done", "item_id": msg_item_id, "output_index": item_index[msg_item_id], "content_index": 0, "text": full_text}
                            )
                            yield _event(
                                {
                                    "type": "response.content_part.done",
                                    "item_id": msg_item_id,
                                    "output_index": item_index[msg_item_id],
                                    "content_index": 0,
                                    "part": {"type": "output_text", "text": full_text, "annotations": []},
                                }
                            )
                            msg_item["content"] = [{"type": "output_text", "text": full_text, "annotations": []}]
                        msg_item["status"] = "completed"
                    for st in tool_state.values():
                        tool_idx = item_index[st["call_id"]]
                        yield _event(
                            {
                                "type": "response.function_call_arguments.done",
                                "item_id": st["call_id"],
                                "output_index": tool_idx,
                                "arguments": st["args"],
                            }
                        )
                        item_ = output_items[tool_idx]
                        item_["status"] = "completed"
                        item_["arguments"] = st["args"]
                        yield _event({"type": "response.output_item.done", "output_index": tool_idx, "item": item_})
                    if msg_open:
                        yield _event({"type": "response.output_item.done", "output_index": item_index[msg_item_id], "item": msg_item})
                    # не выходим из цикла: после finish апстрим присылает
                    # ещё кадр с расходом токенов (include_usage)

            if stream_error is not None:
                # честный response.failed вместо молчаливого обрыва потока
                failed = base_resp("failed")
                failed["output"] = output_items
                failed["output_text"] = "".join(text_parts)
                failed["error"] = {"code": "upstream_error", "message": str(stream_error)[:500]}
                yield _event({"type": "response.failed", "response": failed})
                yield "data: [DONE]\n\n"
                return

            final = {
                "id": response_id,
                "object": "response",
                "status": "completed",
                "model": model,
                "output": output_items,
                "output_text": "".join(text_parts),
                "usage": _usage_to_responses(usage),
            }
            yield _event({"type": "response.completed", "response": final})
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _event(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False, separators=(',', ':'))}\n\n"


# ---------------------------------------------------------------- main endpoint
@app.post("/v1/responses")
async def create_response(request: Request):
    err = _check_auth(request)
    if err:
        return JSONResponse({"error": {"type": "authentication_error", "message": err}}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_error_body(400, "Invalid JSON body"), status_code=400)
    if not isinstance(body, dict) or not body.get("model"):
        return JSONResponse(_error_body(400, "Missing required field: model"), status_code=400)

    provider, model_info = find_model(body["model"])
    if provider is None:
        return JSONResponse(_error_body(404, f"Unknown model: {body['model']}"), status_code=404)

    kind = provider.get("kind", "")
    if kind == "opencode-chat":
        chat = _responses_to_chat(body)
        # base_url провайдера позволяет указать другой апстрим (например,
        # OpenCode Zen с бесплатными моделями вместо Go).
        upstream = (provider.get("base_url") or "").strip() or OPENCODE_UPSTREAM
        if _is_self_url(upstream):
            log.warning(
                "provider %s: base_url %s указывает на сам прокси, использую %s",
                provider.get("id"), upstream, OPENCODE_UPSTREAM,
            )
            upstream = OPENCODE_UPSTREAM
        # Ключ — из env_key конкретного провайдера (OPENCODE_GO_API_KEY,
        # OPENCODE_ZEN_API_KEY, ...), а не всегда от OpenCode Go.
        key = get_setting(provider.get("env_key", ""))
        model_id = body["model"]
        if body.get("stream"):
            return await _handle_stream(chat, upstream, key, model_id)
        return await _handle_non_stream(chat, upstream, key, model_id)

    # Неизвестный kind — ошибка конфигурации; молча считать его passthrough
    # нельзя: запрос ушёл бы не в тот формат и не на тот адрес.
    if kind != "passthrough":
        return JSONResponse(
            _error_body(400, f"Provider '{provider.get('id', '?')}' has unknown kind: {kind!r} (expected 'passthrough' or 'opencode-chat')"),
            status_code=400,
        )

    # passthrough: forward to provider's base_url/responses as-is
    key = get_setting(provider.get("env_key", ""))
    base = (provider.get("base_url") or "").rstrip("/")
    if not base:
        return JSONResponse(
            _error_body(400, f"Provider '{provider.get('id', '?')}' has no base_url"),
            status_code=400,
        )
    return await _passthrough(base + "/responses", key, body)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/shutdown")
async def shutdown_proxy(request: Request):
    """Плавная остановка: GUI просит прокси завершиться самому,
    чтобы он корректно почистил свои временные файлы (_MEI).
    Проверяем Host/Origin, как и остальные эндпоинты: любой сайт может
    отправить «простой» POST на localhost из браузера."""
    err = _check_auth(request)
    if err:
        return JSONResponse({"error": {"type": "authentication_error", "message": err}}, status_code=401)
    server = getattr(app.state, "uvicorn_server", None)
    if server is None:
        return JSONResponse({"error": "server instance not available"}, status_code=500)
    server.should_exit = True
    return {"status": "stopping"}
