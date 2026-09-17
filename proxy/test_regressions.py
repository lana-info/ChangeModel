"""Регрессионные тесты прокси и генератора конфига.

Запуск: python proxy/test_regressions.py

Покрывают исправления:
1. Ключ апстрима берётся из env_key конкретного провайдера, а не только
   OPENCODE_GO_API_KEY (иначе ключ OpenCode Go уходил на апстрим Zen).
2. В стриминге message-item не затирается tool-item'ом (текст, затем вызов
   инструмента).
3. passthrough без base_url возвращает 400, а не падает.
4. _check_auth: Host + Origin (CSRF), IPv6-localhost; /shutdown защищён.
5. generator: профиль base не удаляет чужие model_providers; фолбэк модели
   при пустом default_model; тесты не зависят от providers.json репозитория.
6. Сетевая ошибка до апстрима — 502 с JSON-ошибкой (не HTML-500); обрыв
   стрима посреди ответа — событие response.failed с частичным текстом.
7. Неизвестный kind провайдера — 400, а не молчаливый passthrough.
8. GUI: смена env_key переносит ключ под новое имя; providers.json создаётся
   из providers.example.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path

import httpx
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy import app as app_module  # noqa: E402


# ---------------------------------------------------------------- helpers
class _FakeJsonUpstream:
    def __init__(self, payload, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeStreamUpstream:
    def __init__(self, lines) -> None:
        self.status_code = 200
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aclose(self):
        pass

    async def aread(self):
        return b""


class _FakeHttpClient:
    """Запоминает отправленные заголовки/URL и отдаёт подложный ответ."""

    def __init__(self, response) -> None:
        self.response = response
        self.last_headers = None
        self.last_url = None

    def build_request(self, method, url, headers=None, json=None):
        self.last_headers = headers
        self.last_url = url
        return {"method": method, "url": url, "headers": headers, "json": json}

    async def send(self, req, stream=False):
        return self.response

    async def post(self, url, headers=None, json=None):
        self.last_headers = headers
        self.last_url = url
        return self.response

    async def aclose(self):
        pass


class _FakeHeaders(dict):
    """dict с методами, достаточными для _check_auth."""


class _Patch:
    """Временная подмена атрибутов proxy.app с авто-восстановлением."""

    def __init__(self, **overrides) -> None:
        self.overrides = overrides
        self.saved: dict = {}

    def __enter__(self):
        for key, value in self.overrides.items():
            self.saved[key] = getattr(app_module, key)
            setattr(app_module, key, value)
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            setattr(app_module, key, value)
        return False


def _make_request(body: dict, headers: dict | None = None) -> Request:
    raw = json.dumps(body).encode("utf-8")
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {"host": "127.0.0.1:4096"}).items()]

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/v1/responses", "headers": hdrs, "query_string": b""}
    return Request(scope, receive=receive)


def _collect_stream(resp) -> str:
    async def run() -> bytes:
        out = b""
        async for chunk in resp.body_iterator:
            out += chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        return out

    return asyncio.run(run()).decode("utf-8")


def _sse_events(body: str) -> list[dict]:
    events = []
    for block in body.split("\n\n"):
        block = block.strip()
        if block.startswith("data: ") and block != "data: [DONE]":
            events.append(json.loads(block[len("data: "):]))
    return events


def _chat_payload(text: str = "hello") -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }


ZEN_PROVIDER = {
    "id": "zen-test",
    "name": "Zen Test",
    "kind": "opencode-chat",
    "base_url": "http://upstream.example/v1",
    "env_key": "TEST_ZEN_API_KEY",
    "models": [{"id": "zen-model", "name": "Zen Model"}],
}


# ---------------------------------------------------------------- proxy tests
def test_upstream_key_from_provider_env_key() -> None:
    """Ключ берётся из env_key провайдера (регрессия: всегда брался Go-ключ)."""
    client = _FakeHttpClient(_FakeJsonUpstream(_chat_payload("pong")))
    with _Patch(
        load_providers=lambda: [ZEN_PROVIDER],
        get_setting=lambda name: "sk-zen-secret" if name == "TEST_ZEN_API_KEY" else "",
        http_client=lambda: client,
    ):
        resp = asyncio.run(app_module.create_response(_make_request({"model": "zen-model", "input": "hi"})))

    assert resp.status_code == 200
    assert client.last_headers.get("Authorization") == "Bearer sk-zen-secret"
    assert client.last_url.endswith("/chat/completions")
    assert json.loads(resp.body)["output_text"] == "pong"


def test_stream_key_from_provider_env_key() -> None:
    """То же для стриминга: заголовок формируется из переданного ключа."""
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "hi"}}]}),
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]",
    ]
    client = _FakeHttpClient(_FakeStreamUpstream(lines))
    with _Patch(
        load_providers=lambda: [ZEN_PROVIDER],
        get_setting=lambda name: "sk-zen-secret" if name == "TEST_ZEN_API_KEY" else "",
        http_client=lambda: client,
    ):
        resp = asyncio.run(app_module.create_response(_make_request({"model": "zen-model", "stream": True})))
        body = _collect_stream(resp)

    assert client.last_headers.get("Authorization") == "Bearer sk-zen-secret"
    events = _sse_events(body)
    assert events[-1]["type"] == "response.completed"


def test_stream_text_then_tool_call_keeps_message_intact() -> None:
    """Текст, затем вызов инструмента: message-item в ответе содержит текст,
    а не затёрт tool-item'ом (регрессия finish-блока streaming)."""
    text_chunk = {"choices": [{"delta": {"content": "Сначала посмотрю файлы"}}]}
    tool_chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "id": "call_1", "function": {"name": "shell", "arguments": '{"cmd":"ls"}'}}
                    ]
                }
            }
        ]
    }
    finish_chunk = {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
    lines = [
        "data: " + json.dumps(text_chunk, ensure_ascii=False),
        "data: " + json.dumps(tool_chunk),
        "data: " + json.dumps(finish_chunk),
        "data: [DONE]",
    ]
    client = _FakeHttpClient(_FakeStreamUpstream(lines))
    with _Patch(
        load_providers=lambda: [ZEN_PROVIDER],
        get_setting=lambda name: "",
        http_client=lambda: client,
    ):
        resp = asyncio.run(app_module.create_response(_make_request({"model": "zen-model", "stream": True})))
        body = _collect_stream(resp)

    events = _sse_events(body)
    completed = next(e for e in events if e["type"] == "response.completed")["response"]
    output = completed["output"]
    assert [it["type"] for it in output] == ["message", "function_call"]

    message = output[0]
    tool_call = output[1]
    assert message["status"] == "completed"
    assert message["content"][0]["text"] == "Сначала посмотрю файлы"
    assert tool_call["status"] == "completed"
    assert tool_call["arguments"] == '{"cmd":"ls"}'
    assert not tool_call.get("content")
    assert completed["output_text"] == "Сначала посмотрю файлы"

    # output_item.done для message должен нести именно текст, а не tool-item
    msg_done = [
        e for e in events
        if e["type"] == "response.output_item.done" and e.get("item", {}).get("type") == "message"
    ]
    assert msg_done and msg_done[0]["item"]["content"][0]["text"] == "Сначала посмотрю файлы"


def test_stream_multiline_data_frame() -> None:
    """SSE-событие, разбитое на несколько строк data:, склеивается по спецификации."""
    lines = [
        'data: {"choices":[{"delta":',
        'data: {"content":"multi"}}]}',
        "",
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "",
        "data: [DONE]",
        "",
    ]
    client = _FakeHttpClient(_FakeStreamUpstream(lines))
    with _Patch(
        load_providers=lambda: [ZEN_PROVIDER],
        get_setting=lambda name: "",
        http_client=lambda: client,
    ):
        resp = asyncio.run(app_module.create_response(_make_request({"model": "zen-model", "stream": True})))
        body = _collect_stream(resp)

    completed = next(e for e in _sse_events(body) if e["type"] == "response.completed")["response"]
    assert completed["output_text"] == "multi"


def test_models_endpoint_uses_catalog() -> None:
    """/v1/models выполняет сборку каталога (в отдельном потоке) и отдаёт его."""
    with _Patch(catalog_entries=lambda: [{"slug": "x", "display_name": "X"}]):
        resp = asyncio.run(app_module.models(_make_request({}, headers={"host": "127.0.0.1:4096"})))
    assert resp.status_code == 200
    assert json.loads(resp.body)["models"] == [{"slug": "x", "display_name": "X"}]


def test_function_call_null_fields_are_safe() -> None:
    msgs = app_module._input_to_messages(
        [{"type": "function_call", "call_id": "c1", "name": None, "arguments": None}]
    )
    fn = msgs[0]["tool_calls"][0]["function"]
    assert fn["name"] == ""
    assert fn["arguments"] == "{}"


def test_catalog_modalities_are_codex_compatible() -> None:
    """input_modalities в каталоге — только text/image/audio.

    Регрессия: значения video/pdf ломали декодирование ВСЕГО ответа
    /v1/models («unknown variant `video`, expected one of
    `text`, `image`, `audio`»), и в Codex исчезали все модели разом.
    """
    import models_data as md
    import proxy.app as app

    orig_fetch = md._fetch_models_dev
    orig_providers = app.load_providers
    # models.dev отдаёт в modalities.input лишние значения — они не должны просочиться
    md._fetch_models_dev = lambda: (
        {"p": {"m1": {"context": 1000, "output": 100, "input": ["text", "image", "video", "audio", "pdf"]}}},
        {"m1": {"context": 1000, "output": 100, "input": ["text", "image", "video", "audio", "pdf"]}},
    )
    md._limits_cache["at"] = -1e18
    app.load_providers = lambda: [{"id": "p", "name": "P", "models": [{"id": "m1", "name": "M1"}]}]
    try:
        entries = app.catalog_entries()
        assert entries, "каталог не должен быть пустым"
        allowed = {"text", "image", "audio"}
        for e in entries:
            assert set(e["input_modalities"]) <= allowed, e["input_modalities"]
            assert "image" in e["input_modalities"]  # картинки сохраняются
    finally:
        md._fetch_models_dev = orig_fetch
        md._limits_cache["at"] = -1e18
        md._limits_cache["nested"] = None
        md._limits_cache["flat"] = None
        app.load_providers = orig_providers


def test_gui_read_env_key_prefers_dotenv() -> None:
    """GUI читает ключ так же, как прокси: .env важнее переменной окружения."""
    import gui

    with tempfile.TemporaryDirectory() as td:
        proxy_dir = Path(td) / "proxy"
        proxy_dir.mkdir()
        (proxy_dir / ".env").write_text("TEST_GUI_KEY=from-dotenv\n", encoding="utf-8")
        orig_base = gui.BASE_DIR
        gui.BASE_DIR = Path(td)
        os.environ["TEST_GUI_KEY"] = "from-env"
        try:
            assert gui._read_env_key("TEST_GUI_KEY") == "from-dotenv"
            # если ключа нет в .env — берём из окружения
            assert gui._read_env_key("TEST_OTHER_KEY") == ""
            os.environ["TEST_OTHER_KEY"] = "env-only"
            assert gui._read_env_key("TEST_OTHER_KEY") == "env-only"
        finally:
            gui.BASE_DIR = orig_base
            os.environ.pop("TEST_GUI_KEY", None)
            os.environ.pop("TEST_OTHER_KEY", None)


def test_upstream_session_header_sent() -> None:
    """Апстриму OpenCode отправляется x-opencode-session: без него Go отдаёт
    400 MissingSessionID, а free-модели Zen — ошибку free tier."""
    client = _FakeHttpClient(_FakeJsonUpstream(_chat_payload("ok")))
    with _Patch(
        load_providers=lambda: [ZEN_PROVIDER],
        get_setting=lambda name: "",
        http_client=lambda: client,
    ):
        asyncio.run(app_module.create_response(_make_request({"model": "zen-model"})))
    assert client.last_headers.get("x-opencode-session")

    lines = [
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]",
    ]
    client2 = _FakeHttpClient(_FakeStreamUpstream(lines))
    with _Patch(
        load_providers=lambda: [ZEN_PROVIDER],
        get_setting=lambda name: "",
        http_client=lambda: client2,
    ):
        asyncio.run(app_module.create_response(_make_request({"model": "zen-model", "stream": True})))
    assert client2.last_headers.get("x-opencode-session")


def test_stream_usage_after_finish() -> None:
    """Кадр с usage приходит ПОСЛЕ finish — поток не закрывается раньше
    времени, счётчик токенов попадает в response.completed."""
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "hi"}}]}),
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}}),
        "data: [DONE]",
    ]
    client = _FakeHttpClient(_FakeStreamUpstream(lines))
    with _Patch(
        load_providers=lambda: [ZEN_PROVIDER],
        get_setting=lambda name: "",
        http_client=lambda: client,
    ):
        resp = asyncio.run(app_module.create_response(_make_request({"model": "zen-model", "stream": True})))
        body = _collect_stream(resp)

    completed = next(e for e in _sse_events(body) if e["type"] == "response.completed")["response"]
    assert completed["usage"] == {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12}
    # stream_options запрашивается у апстрима
    assert client.last_url  # запрос ушёл
    events = _sse_events(body)
    # завершение пришло ровно один раз
    assert sum(1 for e in events if e["type"] == "response.completed") == 1


def test_stream_requests_include_usage_option() -> None:
    """В стримящемся запросе к апстриму выставляется stream_options.include_usage."""
    client = _FakeHttpClient(_FakeStreamUpstream(["data: [DONE]"]))
    captured = {}

    def fake_build_request(method, url, headers=None, json=None):
        captured.update({"url": url, "headers": headers, "json": json})
        return {"method": method, "url": url, "headers": headers, "json": json}

    client.build_request = fake_build_request
    with _Patch(
        load_providers=lambda: [ZEN_PROVIDER],
        get_setting=lambda name: "",
        http_client=lambda: client,
    ):
        asyncio.run(app_module.create_response(_make_request({"model": "zen-model", "stream": True})))

    assert captured["json"]["stream_options"] == {"include_usage": True}


def test_is_incompatible_only_responses_wire() -> None:
    """MiniMax и Qwen-max больше не блокируются (ходят по chat-проводу),
    блокируются только Responses-модели, которым прокси-трансляция не нужна."""
    import gui

    assert gui._is_incompatible("grok-4.5")
    assert gui._is_incompatible("GPT-5.6-LUNA")
    assert not gui._is_incompatible("minimax-m3")
    assert not gui._is_incompatible("minimax-m2.5")
    assert not gui._is_incompatible("qwen3.8-max")
    assert not gui._is_incompatible("qwen/qwen3.8-27b")


def test_collect_free_items() -> None:
    """Сбор бесплатных моделей: фильтрует платные и несовместимые, помечает
    уже добавленные, для OpenRouter не применяет фильтр провода."""
    import gui

    def fake_fetcher(prov, wire_filter=True):
        if prov["id"] == "opencode-zen":
            assert wire_filter is True
            return [
                {"id": "mimo-v2.5-free", "name": "MiMo free", "free": True, "price_in": None, "price_out": None, "incompatible": False},
                {"id": "glm-5.3", "name": "GLM paid", "free": False, "price_in": 1.0, "price_out": 2.0, "incompatible": False},
                {"id": "some-wire-free", "name": "Wire", "free": True, "price_in": None, "price_out": None, "incompatible": True},
            ]
        if prov["id"] == "openrouter":
            assert wire_filter is False
            return [
                {"id": "x/x:free", "name": "X free", "free": True, "price_in": 0.0, "price_out": 0.0, "incompatible": False},
                {"id": "y/y", "name": "Y paid", "free": False, "price_in": 3.0, "price_out": 4.0, "incompatible": False},
            ]
        raise AssertionError("unexpected provider " + prov["id"])

    providers = [
        {"id": "opencode-zen", "name": "Zen", "kind": "opencode-chat", "base_url": "https://z/v1", "env_key": "Z", "models": [{"id": "mimo-v2.5-free", "name": "m"}]},
        {"id": "openrouter", "name": "OR", "kind": "passthrough", "base_url": "https://o/v1", "env_key": "O", "models": []},
        {"id": "opencode", "name": "Go", "kind": "opencode-chat", "base_url": "https://g/v1", "env_key": "G", "models": []},
    ]
    items, errors = gui.collect_free_items(providers, fetcher=fake_fetcher)
    assert errors == []
    assert [i["id"] for i in items] == ["mimo-v2.5-free", "x/x:free"]
    by_id = {i["id"]: i for i in items}
    assert by_id["mimo-v2.5-free"]["added"] is True
    assert by_id["mimo-v2.5-free"]["provider_id"] == "opencode-zen"
    assert by_id["x/x:free"]["added"] is False
    assert by_id["x/x:free"]["provider_id"] == "openrouter"


def test_collect_free_items_missing_providers() -> None:
    import gui

    items, errors = gui.collect_free_items([], fetcher=lambda p, wire_filter=True: [])
    assert items == []
    assert len(errors) == 2  # Zen и OpenRouter не настроены


def test_parse_atom_latest() -> None:
    import gui

    xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <link rel="alternate" type="text/html" href="https://github.com/lana-info/ChangeModel/releases/tag/v1.2.0"/>
    <title>ChangeModel v1.2.0</title>
  </entry>
</feed>"""
    assert gui._parse_atom_latest(xml) == ("v1.2.0", "https://github.com/lana-info/ChangeModel/releases/tag/v1.2.0")
    assert gui._parse_atom_latest("<feed xmlns='http://www.w3.org/2005/Atom'></feed>") == (None, None)


def test_robust_junk_input() -> None:
    """Мусор в input (null, числа) пропускается, а не роняет прокси с 500."""
    from proxy.app import _input_to_messages

    msgs = _input_to_messages(
        [None, 42, {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    )
    assert len(msgs) == 1 and msgs[0]["content"] == "hi"


def test_robust_junk_tools() -> None:
    from proxy.app import _tool_choice_to_chat, _tools_to_chat

    tools = _tools_to_chat(["x", None, {"type": "function", "name": "f"}])
    assert len(tools) == 1 and tools[0]["function"]["name"] == "f"
    assert _tool_choice_to_chat({"function": "name-as-string"}) == "auto"


def test_robust_junk_upstream_message() -> None:
    from proxy.app import _chat_message_to_items

    items, _text = _chat_message_to_items(
        {"content": None, "tool_calls": [None, "x", {"id": "c1", "function": {"name": "f", "arguments": "{}"}}]}
    )
    assert len(items) == 1 and items[0]["type"] == "function_call"


def test_find_model_skips_broken_entries() -> None:
    import proxy.app as app

    orig = app.load_providers
    app.load_providers = lambda: [{"id": "p", "models": [{"name": "NoId"}, "junk", {"id": "m1", "name": "M1"}]}]
    try:
        prov, model = app.find_model("m1")
        assert prov is not None and model["id"] == "m1"
        assert app.find_model("нет-такой") == (None, None)
    finally:
        app.load_providers = orig


def test_stream_skips_malformed_chunks() -> None:
    """Битые чанки апстрима (choices [null], tool_calls с мусором) не рвут стрим."""
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "hi"}}]}),
        "data: " + json.dumps({"choices": [None]}),
        "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [None, "x"]}}]}),
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]",
    ]
    client = _FakeHttpClient(_FakeStreamUpstream(lines))
    with _Patch(
        load_providers=lambda: [ZEN_PROVIDER],
        get_setting=lambda name: "",
        http_client=lambda: client,
    ):
        resp = asyncio.run(app_module.create_response(_make_request({"model": "zen-model", "stream": True})))
        body = _collect_stream(resp)

    completed = next(e for e in _sse_events(body) if e["type"] == "response.completed")["response"]
    assert completed["output_text"] == "hi"


def test_catalog_skips_broken_entries() -> None:
    import models_data as md
    import proxy.app as app

    orig_fetch = md._fetch_models_dev
    orig_providers = app.load_providers
    md._fetch_models_dev = lambda: None
    md._limits_cache["at"] = -1e18
    app.load_providers = lambda: [{"id": "p", "name": "P", "models": [{"name": "NoId"}, "junk", {"id": "m1", "name": "M1"}]}]
    try:
        entries = app.catalog_entries()
        assert [e["slug"] for e in entries] == ["m1"]
    finally:
        md._fetch_models_dev = orig_fetch
        md._limits_cache["at"] = -1e18
        md._limits_cache["nested"] = None
        md._limits_cache["flat"] = None
        app.load_providers = orig_providers


def test_passthrough_without_base_url_returns_400() -> None:
    provider = {
        "id": "no-base",
        "name": "NoBase",
        "kind": "passthrough",
        "env_key": "NO_BASE_KEY",
        "models": [{"id": "m1", "name": "M1"}],
    }
    with _Patch(load_providers=lambda: [provider], get_setting=lambda name: "k"):
        resp = asyncio.run(app_module.create_response(_make_request({"model": "m1"})))

    assert resp.status_code == 400
    assert "base_url" in json.loads(resp.body)["error"]["message"]


def test_check_auth_host_and_origin() -> None:
    """Host — только localhost; Origin (его шлют браузеры) — только сам прокси."""
    req = lambda headers: type("R", (), {"headers": _FakeHeaders(headers)})()

    assert app_module._check_auth(req({"host": "127.0.0.1:4096"})) is None
    assert app_module._check_auth(req({"host": "localhost:4096"})) is None
    assert app_module._check_auth(req({"host": "[::1]:4096"})) is None
    assert app_module._check_auth(req({"host": "evil.com"})) is not None

    # Origin чужой страницы — CSRF-защита; без Origin (Codex/GUI) — пропускаем
    assert app_module._check_auth(req({"host": "127.0.0.1:4096", "origin": "http://evil.com"})) is not None
    assert app_module._check_auth(req({"host": "127.0.0.1:4096", "origin": "http://127.0.0.1:4096"})) is None
    assert app_module._check_auth(req({"host": "127.0.0.1:4096", "origin": "http://127.0.0.1:4096/"})) is None
    assert app_module._check_auth(req({"host": "localhost:4096"})) is None


def test_shutdown_endpoint_requires_local_host() -> None:
    resp = asyncio.run(app_module.shutdown_proxy(_make_request({}, headers={"host": "evil.com"})))
    assert resp.status_code == 401

    # Локальный Host без запущенного сервера — 500 «server not available», без падения
    resp = asyncio.run(app_module.shutdown_proxy(_make_request({}, headers={"host": "127.0.0.1:4096"})))
    assert resp.status_code == 500


def test_unknown_kind_returns_400() -> None:
    """Опечатка в kind провайдера — понятная 400, а не молчаливый passthrough."""
    provider = {
        "id": "typo",
        "name": "Typo",
        "kind": "pass-through",  # опечатка
        "base_url": "http://upstream.example/v1",
        "env_key": "K",
        "models": [{"id": "m1", "name": "M1"}],
    }
    with _Patch(load_providers=lambda: [provider], get_setting=lambda name: "k"):
        resp = asyncio.run(app_module.create_response(_make_request({"model": "m1"})))
    assert resp.status_code == 400
    assert "unknown kind" in json.loads(resp.body)["error"]["message"]


class _RaisingHttpClient:
    """Клиент, у которого сеть до апстрима недоступна."""

    def build_request(self, method, url, headers=None, json=None):
        return {"method": method, "url": url, "headers": headers, "json": json}

    async def send(self, req, stream=False):
        raise httpx.ConnectError("connection refused")

    async def post(self, url, headers=None, json=None):
        raise httpx.ConnectError("connection refused")


def test_upstream_network_error_returns_502_json() -> None:
    """Провайдер лёг: прокси отдаёт 502 с JSON-ошибкой, а не HTML-500."""
    with _Patch(
        load_providers=lambda: [ZEN_PROVIDER],
        get_setting=lambda name: "",
        http_client=lambda: _RaisingHttpClient(),
    ):
        resp = asyncio.run(app_module.create_response(_make_request({"model": "zen-model"})))
    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert body["error"]["type"] == "invalid_request_error"
    assert "Upstream request failed" in body["error"]["message"]


def test_stream_open_network_error_returns_502_json() -> None:
    """То же для стриминга: ошибка сети на открытии апстрима — 502 JSON."""
    with _Patch(
        load_providers=lambda: [ZEN_PROVIDER],
        get_setting=lambda name: "",
        http_client=lambda: _RaisingHttpClient(),
    ):
        resp = asyncio.run(app_module.create_response(_make_request({"model": "zen-model", "stream": True})))
    assert resp.status_code == 502
    assert "Upstream request failed" in json.loads(resp.body)["error"]["message"]


class _InterruptibleStreamUpstream:
    """Стрим, который обрывается посреди передачи."""

    status_code = 200

    async def aiter_lines(self):
        yield "data: " + json.dumps({"choices": [{"delta": {"content": "частичный"}}]})
        raise httpx.ReadError("connection reset by peer")

    async def aclose(self):
        pass


def test_stream_interrupted_emits_response_failed() -> None:
    """Обрыв апстрима посреди стрима: клиент получает response.failed
    с частичным текстом, а не молчаливо оборванный поток."""
    client = _FakeHttpClient(_InterruptibleStreamUpstream())
    with _Patch(
        load_providers=lambda: [ZEN_PROVIDER],
        get_setting=lambda name: "",
        http_client=lambda: client,
    ):
        resp = asyncio.run(app_module.create_response(_make_request({"model": "zen-model", "stream": True})))
        body = _collect_stream(resp)

    events = _sse_events(body)
    failed = [e for e in events if e["type"] == "response.failed"]
    assert failed, "ожидалось событие response.failed"
    assert failed[0]["response"]["status"] == "failed"
    assert failed[0]["response"]["error"]["code"] == "upstream_error"
    assert failed[0]["response"]["output_text"] == "частичный"
    # response.completed при обрыве не отправляется
    assert not [e for e in events if e["type"] == "response.completed"]


def test_gui_migrate_env_key_renames_key() -> None:
    """Смена env_key переносит сохранённый ключ под новое имя."""
    import gui

    with tempfile.TemporaryDirectory() as td:
        proxy_dir = Path(td) / "proxy"
        proxy_dir.mkdir()
        env_file = proxy_dir / ".env"
        env_file.write_text("OLD_KEY=sk-secret\nOTHER=keep-me\n", encoding="utf-8")
        orig_base = gui.BASE_DIR
        gui.BASE_DIR = Path(td)
        try:
            # 1) переименование без нового ключа: значение переезжает
            gui._migrate_env_key("OLD_KEY", "NEW_KEY", "")
            text = env_file.read_text(encoding="utf-8")
            assert "NEW_KEY=sk-secret" in text
            assert "OLD_KEY" not in text
            assert "OTHER=keep-me" in text
            # 2) переименование с новым ключом: пишется новое значение
            gui._migrate_env_key("NEW_KEY", "THIRD_KEY", "sk-fresh")
            text = env_file.read_text(encoding="utf-8")
            assert "THIRD_KEY=sk-fresh" in text
            assert "NEW_KEY" not in text
            # 3) то же имя + новый ключ: просто обновление
            gui._migrate_env_key("THIRD_KEY", "THIRD_KEY", "sk-updated")
            text = env_file.read_text(encoding="utf-8")
            assert "THIRD_KEY=sk-updated" in text
        finally:
            gui.BASE_DIR = orig_base


def test_gui_ensure_data_file_from_example() -> None:
    """providers.json создаётся из providers.example.json при первом запуске."""
    import gui

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        (td_path / "providers.example.json").write_text(
            '{"default_model": "tpl-model", "providers": []}', encoding="utf-8"
        )
        orig_file, orig_base = gui.PROVIDERS_FILE, gui.BASE_DIR
        gui.PROVIDERS_FILE = td_path / "providers.json"
        gui.BASE_DIR = td_path
        try:
            gui.ensure_data_file()
            assert gui.PROVIDERS_FILE.exists()
            data = json.loads(gui.PROVIDERS_FILE.read_text(encoding="utf-8"))
            assert data["default_model"] == "tpl-model"
            # повторный вызов не затирает существующий файл
            gui.PROVIDERS_FILE.write_text('{"default_model": "user", "providers": []}', encoding="utf-8")
            gui.ensure_data_file()
            assert json.loads(gui.PROVIDERS_FILE.read_text(encoding="utf-8"))["default_model"] == "user"
        finally:
            gui.PROVIDERS_FILE, gui.BASE_DIR = orig_file, orig_base


# ---------------------------------------------------------------- generator tests
CONFIG_WITH_FOREIGN = """\
model = "old-model"
model_provider = "changemodel"

[model_providers.openrouter]
name = "OR"
base_url = "https://openrouter.ai/api/v1"
env_key = "OPENROUTER_API_KEY"
wire_api = "responses"

[model_providers.changemodel]
name = "ChangeModel"
base_url = "http://127.0.0.1:4096/v1"
env_key = "CHANGE_MODEL_API_KEY"
wire_api = "responses"
"""


def test_generator_base_keeps_foreign_providers() -> None:
    import generator

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.toml"
        cfg.write_text(CONFIG_WITH_FOREIGN, encoding="utf-8")

        ok, message = generator.write_config("base", str(cfg))
        assert ok, message
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
        assert data["model"] == generator.BASE_MODEL
        assert "model_provider" not in data
        assert set(data["model_providers"]) == {"openrouter"}


def test_generator_base_drops_empty_providers_table() -> None:
    import generator

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.toml"
        cfg.write_text(
            'model = "old"\n'
            'model_provider = "changemodel"\n'
            "\n[model_providers.changemodel]\n"
            'name = "ChangeModel"\n'
            'base_url = "http://127.0.0.1:4096/v1"\n'
            'env_key = "CHANGE_MODEL_API_KEY"\n'
            'wire_api = "responses"\n',
            encoding="utf-8",
        )
        ok, message = generator.write_config("base", str(cfg))
        assert ok, message
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
        assert "model_providers" not in data


def test_generator_changemodel_keeps_foreign_providers() -> None:
    import generator

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.toml"
        cfg.write_text(CONFIG_WITH_FOREIGN, encoding="utf-8")
        # свой providers.json: тест не должен зависеть от конфига в репозитории
        providers = Path(td) / "providers.json"
        providers.write_text(
            json.dumps(
                {
                    "default_model": "test-default",
                    "providers": [{"id": "p", "name": "P", "kind": "passthrough", "base_url": "https://x/v1", "env_key": "K", "models": [{"id": "m1", "name": "M1"}]}],
                }
            ),
            encoding="utf-8",
        )
        orig = generator.PROVIDERS_FILE
        generator.PROVIDERS_FILE = str(providers)
        try:
            ok, message = generator.write_config("changemodel", str(cfg))
        finally:
            generator.PROVIDERS_FILE = orig
        assert ok, message
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
        assert data["model_provider"] == "changemodel"
        assert "changemodel" in data["model_providers"]
        assert "openrouter" in data["model_providers"]
        assert data["model"] == "test-default"


def test_generator_model_fallback() -> None:
    """Пустой default_model -> первая модель провайдера; нет моделей -> BASE_MODEL."""
    import generator

    with tempfile.TemporaryDirectory() as td:
        providers = Path(td) / "providers.json"
        cfg = Path(td) / "config.toml"
        orig = generator.PROVIDERS_FILE
        generator.PROVIDERS_FILE = str(providers)
        try:
            providers.write_text(
                json.dumps(
                    {
                        "providers": [
                            {"id": "p1", "name": "P1", "kind": "passthrough", "base_url": "https://x/v1", "env_key": "K", "models": []},
                            {"id": "p2", "name": "P2", "kind": "passthrough", "base_url": "https://y/v1", "env_key": "K2", "models": [{"id": "first-model", "name": "F"}]},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            ok, message = generator.write_config("changemodel", str(cfg))
            assert ok, message
            data = tomllib.loads(cfg.read_text(encoding="utf-8"))
            assert data["model"] == "first-model"

            providers.write_text(json.dumps({"providers": []}), encoding="utf-8")
            ok, message = generator.write_config("changemodel", str(cfg))
            assert ok, message
            data = tomllib.loads(cfg.read_text(encoding="utf-8"))
            assert data["model"] == generator.BASE_MODEL
        finally:
            generator.PROVIDERS_FILE = orig


def test_free_line_marks_provider_and_new() -> None:
    import gui

    new_zen = {"id": "mimo-v2.5-free", "name": "MiMo", "added": False, "provider_id": "opencode-zen", "source": "OpenCode Zen"}
    old_or = {"id": "x/x:free", "name": "X", "added": True, "provider_id": "openrouter", "source": "OpenRouter"}
    assert gui._free_line(new_zen) == "[новая] [Zen] MiMo — mimo-v2.5-free"
    assert gui._free_line(old_or) == "[OpenRouter] X — x/x:free"


def test_add_free_items() -> None:
    import gui

    providers = [
        {"id": "opencode-zen", "models": [{"id": "a", "name": "A"}]},
        {"id": "openrouter", "models": []},
    ]

    class _Stub:
        pass

    stub = _Stub()
    stub.providers = providers
    chosen = [
        {"id": "a", "name": "A", "provider_id": "opencode-zen"},
        {"id": "b", "name": "B", "provider_id": "opencode-zen"},
        {"id": "c", "name": "C", "provider_id": "openrouter"},
        {"id": "d", "name": "D", "provider_id": "nope"},
    ]
    assert gui.ChangeModelApp._add_free_items(stub, chosen) == 2
    assert [m["id"] for m in providers[0]["models"]] == ["a", "b"]
    assert [m["id"] for m in providers[1]["models"]] == ["c"]


def test_entry_ru_keys_defined() -> None:
    """Русские Ctrl-сочетания зарегистрированы для вставки (без Tk — только данные)."""
    import gui

    assert any("Cyrillic_em" in s for s in gui._ENTRY_RU_KEYS["<<Paste>>"])
    assert any("Cyrillic_es" in s for s in gui._ENTRY_RU_KEYS["<<Copy>>"])
    assert any("Cyrillic_che" in s for s in gui._ENTRY_RU_KEYS["<<Cut>>"])


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\nPassed: {len(fns)}")
