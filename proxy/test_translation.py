"""Регрессионный тест трансляции Responses -> Chat (запуск: python proxy/test_translation.py).

Воспроизводит баг: Codex отправляет function_call_output.output массивом
content-частей [{"type":"input_text","text":...}], апстрим GLM/Zhipu требует
строку (ошибка 1214 "messages[N].content[0].type type error").
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.app import _content_text, _input_to_messages, _message_content, _tool_output_to_text  # noqa: E402


def test_tool_output_array_to_string() -> None:
    """Реальная форма из rollout Codex: output — список input_text частей."""
    item = {
        "type": "function_call_output",
        "call_id": "call_123",
        "output": [
            {"type": "input_text", "text": "Script failed\nWall time 0.0s\n"},
            {"type": "input_text", "text": "Script error:\nexec cell init not found"},
        ],
    }
    msgs = _input_to_messages([item])
    assert len(msgs) == 1
    assert msgs[0]["role"] == "tool"
    assert msgs[0]["tool_call_id"] == "call_123"
    assert isinstance(msgs[0]["content"], str)
    assert msgs[0]["content"].startswith("Script failed")
    assert "exec cell init not found" in msgs[0]["content"]


def test_tool_output_string_unchanged() -> None:
    msgs = _input_to_messages([{"type": "function_call_output", "call_id": "c1", "output": "ok"}])
    assert msgs[0]["content"] == "ok"


def test_tool_output_dict() -> None:
    assert _tool_output_to_text({"text": "hello"}) == "hello"
    assert _tool_output_to_text({"other": 1}) == '{"other": 1}'


def test_tool_call_and_output_roundtrip() -> None:
    """Цепочка вызова инструмента целиком сохраняет порядок и связность."""
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "запусти скрипт"}]},
        {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": "{\"cmd\":\"ls\"}"},
        {"type": "function_call_output", "call_id": "call_1", "output": [{"type": "input_text", "text": "file.txt"}]},
    ]
    msgs = _input_to_messages(items)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[1]["tool_calls"][0]["id"] == "call_1"
    assert msgs[2]["tool_call_id"] == "call_1"
    assert msgs[2]["content"] == "file.txt"


def test_content_text_variants() -> None:
    assert _content_text([{"type": "input_text", "text": "a"}]) == "a"
    assert _content_text([{"type": "output_text", "text": "b"}]) == "b"
    assert _content_text([{"type": "text", "text": "c"}]) == "c"  # раньше терялось
    assert _content_text(["d", {"type": "input_image", "url": "x"}]) == "d"
    assert _content_text(None) == ""


def test_image_message_to_chat_parts() -> None:
    """Codex присылает input_image с плоским image_url (data-URI) —
    должна получиться chat-часть {"type":"image_url","image_url":{"url":...}}
    рядом с текстом, чтобы мультимодальные модели увидели картинку."""
    data_uri = "data:image/png;base64,iVBORw0KGgo="
    items = [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "что на картинке?"},
                {"type": "input_image", "image_url": data_uri},
            ],
        }
    ]
    msgs = _input_to_messages(items)
    assert len(msgs) == 1 and msgs[0]["role"] == "user"
    content = msgs[0]["content"]
    assert isinstance(content, list)
    types = [p["type"] for p in content]
    assert types == ["text", "image_url"]
    assert content[0]["text"] == "что на картинке?"
    assert content[1]["image_url"]["url"] == data_uri


def test_text_only_message_stays_string() -> None:
    """Без картинок контент остаётся строкой (максимальная совместимость с GLM)."""
    msgs = _input_to_messages(
        [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "привет"}]}]
    )
    assert msgs[0]["content"] == "привет"


def test_image_nested_url_and_http() -> None:
    """image_url может прийти и словарём, и обычным http-URL."""
    assert _message_content([{"type": "input_image", "image_url": {"url": "http://x/y.png"}}])[0]["image_url"]["url"] == "http://x/y.png"
    assert _message_content([{"type": "input_image", "image_url": ""}]) == ""  # пустой url -> нет картинки, строка
    assert _message_content([{"type": "input_image"}]) == ""  # без url вообще


def test_model_limits_from_index() -> None:
    """Лимиты контекста берутся из models.dev-индекса (сеть подменена стабом):
    сначала ищем в указанном провайдере, потом глобально, неизвестное — дефолт."""
    import models_data as md

    orig = md._fetch_models_dev
    md._fetch_models_dev = lambda: (
        {"opencode-go": {"glm-5.3-flash": {"context": 1000000, "output": 131072}}},
        {"glm-5.3-flash": {"context": 1000000, "output": 131072}},
    )
    md._limits_cache["at"] = -1e18
    try:
        assert md.model_limits("glm-5.3-flash", "opencode-go") == (1000000, 131072)
        assert md.model_limits("glm-5.3-flash") == (1000000, 131072)
        assert md.model_limits("нет-такой-модели") == (md.DEFAULT_CONTEXT_WINDOW, md.DEFAULT_MAX_OUTPUT_TOKENS)
        entry = md._catalog_entry("glm-5.3-flash", "GLM", "desc", None, "opencode-go")
        assert entry["context_window"] == 1000000
        assert entry["max_context_window"] == 1000000
        unknown = md._catalog_entry("нет-такой-модели", "X", "desc")
        assert unknown["context_window"] == md.DEFAULT_CONTEXT_WINDOW
    finally:
        md._fetch_models_dev = orig
        md._limits_cache["at"] = -1e18
        md._limits_cache["nested"] = None
        md._limits_cache["flat"] = None


def test_vision_modalities_and_mark() -> None:
    """Модели с картинками в models.dev получают input_modalities ["text","image"]
    и метку [img]; текстовые — ["text"] без метки."""
    import models_data as md

    orig = md._fetch_models_dev
    md._fetch_models_dev = lambda: (
        {
            "opencode-go": {
                # реальная форма models.dev: modalities.input — список
                "glm-5.3-flash": {"context": 1000000, "output": 131072, "input": ["text", "image", "video", "pdf"]},
                # подстраховка: понимаем и строковую форму
                "hy3": {"context": 256000, "output": 64000, "input": "text"},
                "no-mod-field": {"context": 100000, "output": 32000},
            }
        },
        {},
    )
    md._limits_cache["at"] = -1e18
    try:
        assert md.model_input_modalities("glm-5.3-flash", "opencode-go") == ["text", "image", "video", "pdf"]
        assert md.vision_mark("glm-5.3-flash", "opencode-go") == " [img]"
        assert md.model_input_modalities("hy3", "opencode-go") == ["text"]
        assert md.vision_mark("hy3", "opencode-go") == ""
        assert md.model_input_modalities("no-mod-field", "opencode-go") == ["text"]  # нет поля modalities
        assert md.model_input_modalities("нет-такой", "opencode-go") == ["text"]  # неизвестная модель
        entry = md._catalog_entry("glm-5.3-flash", "GLM", "desc", None, "opencode-go")
        assert entry["input_modalities"] == ["text", "image", "video", "pdf"]
    finally:
        md._fetch_models_dev = orig
        md._limits_cache["at"] = -1e18
        md._limits_cache["nested"] = None
        md._limits_cache["flat"] = None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\nPassed: {len(fns)}")
