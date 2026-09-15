"""Shared model metadata for the ChangeModel proxy and generator.

OpenCode Go wire formats:
- responses        -> grok-4.5, gpt-5.6-luna (direct, no proxy needed)
- chat/completions -> everything else the proxy serves: glm-*, deepseek-v4-*,
  kimi-*, mimo-*, qwen3.8-*, minimax-*, hy3 (minimax-* и qwen*-max/plus раньше
  говорили на Anthropic messages — сейчас отвечают по chat/completions).

This module lists the chat-completions models the proxy handles.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

UPSTREAM_BASE_URL = "https://opencode.ai/zen/go/v1"

# ---------------------------------------------------------------- limits
# Реальные лимиты контекста берём из models.dev (источник данных самого
# opencode), чтобы Codex не считал все модели 128-контекстными и не
# компактировал чат раньше времени.
MODELS_DEV_URL = "https://models.dev/api.json"

# Fallback для моделей, которых нет в models.dev (например, псевдо-модель
# "openrouter/free") или когда сеть недоступна.
DEFAULT_CONTEXT_WINDOW = 128000
DEFAULT_MAX_OUTPUT_TOKENS = 32768

# id провайдера в providers.json -> id провайдера в models.dev
PROVIDER_MAP = {
    "opencode": "opencode-go",
    "opencode-zen": "opencode",
    "openrouter": "openrouter",
}

_MODELS_DEV_TTL = 6 * 3600.0  # данные моделей меняются редко
_MODELS_DEV_RETRY = 600.0  # после сетевой ошибки ждём 10 минут
_limits_lock = threading.Lock()
_limits_cache: dict = {"at": -1e18, "nested": None, "flat": None}


def _fetch_models_dev() -> tuple[dict, dict] | None:
    """(nested, flat): {provider: {model_id: limits}} и {model_id: limits}."""
    try:
        req = urllib.request.Request(
            MODELS_DEV_URL, headers={"User-Agent": "Mozilla/5.0 (ChangeModel proxy)"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except Exception:
        return None
    nested: dict[str, dict] = {}
    flat: dict[str, dict] = {}
    for prov_id, prov in data.items():
        if not isinstance(prov, dict):
            continue
        for mid, m in (prov.get("models") or {}).items():
            if not isinstance(m, dict):
                continue
            lim = m.get("limit") or {}
            ctx = lim.get("context")
            if not isinstance(ctx, int) or ctx <= 0:
                continue
            entry = {
                "context": ctx,
                "output": lim.get("output") or DEFAULT_MAX_OUTPUT_TOKENS,
                "input": (m.get("modalities") or {}).get("input", "") or "",
            }
            nested.setdefault(prov_id, {})[mid] = entry
            flat.setdefault(mid, entry)
    return nested, flat


def _ensure_limits_cache() -> None:
    """Обновляет кэш models.dev, если он пуст или устарел. Вызывать под _limits_lock."""
    now = time.time()
    if _limits_cache["flat"] is None or now - _limits_cache["at"] > _MODELS_DEV_TTL:
        fetched = _fetch_models_dev()
        if fetched:
            _limits_cache["nested"], _limits_cache["flat"] = fetched
            _limits_cache["at"] = now
        else:
            # не дёргаем сеть на каждый запрос каталога
            _limits_cache["at"] = now - _MODELS_DEV_TTL + _MODELS_DEV_RETRY


def model_limits(model_id: str, provider: str | None = None) -> tuple[int, int]:
    """(context_window, max_output_tokens) модели по данным models.dev.

    provider — id провайдера в терминах models.dev (см. PROVIDER_MAP);
    уточняет поиск при совпадении id моделей у разных провайдеров.
    Кэш на 6 часов; при сетевой ошибке используется последняя копия,
    повторная попытка не раньше чем через 10 минут.
    """
    with _limits_lock:
        _ensure_limits_cache()
        if provider:
            hit = (_limits_cache["nested"] or {}).get(provider, {}).get(model_id)
            if hit:
                return hit["context"], hit["output"]
        hit = (_limits_cache["flat"] or {}).get(model_id)
        if hit:
            return hit["context"], hit["output"]
    return DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_OUTPUT_TOKENS


def warm_limits() -> None:
    """Прогревает кэш лимитов в фоне (вызывается на старте прокси)."""
    threading.Thread(target=model_limits, args=("glm-5.3-flash",), daemon=True).start()


def _cached_model_entry(model_id: str, provider: str | None = None) -> dict | None:
    """Ищет запись модели в кэше models.dev (обновляя его при необходимости):
    сначала в указанном провайдере, затем глобально."""
    with _limits_lock:
        _ensure_limits_cache()
        if provider:
            hit = (_limits_cache["nested"] or {}).get(provider, {}).get(model_id)
            if hit:
                return hit
        return (_limits_cache["flat"] or {}).get(model_id)


def model_input_modalities(model_id: str, provider: str | None = None) -> list[str]:
    """Входные модальности модели по данным models.dev (минимум ["text"]).

    models.dev отдаёт modalities.input списком; на всякий случай понимаем
    и строку вида "text image video".
    """
    entry = _cached_model_entry(model_id, provider)
    inp = (entry or {}).get("input", "")
    if isinstance(inp, str):
        parts = inp.split()
    elif isinstance(inp, (list, tuple)):
        parts = [str(x) for x in inp]
    else:
        parts = []
    mods = ["text"]
    for extra in ("image", "video", "audio", "pdf"):
        if extra in parts:
            mods.append(extra)
    return mods


def vision_mark(model_id: str, provider: str | None = None, mark: str = " [img]") -> str:
    """Метка для отображения у моделей, принимающих картинки."""
    return mark if "image" in model_input_modalities(model_id, provider) else ""

# (id, display name, one-line description)
OPENCODE_CHAT_MODELS = [
    ("glm-5.3", "GLM-5.3", "Zhipu GLM coding model"),
    ("glm-5.2", "GLM-5.2", "Zhipu GLM coding model"),
    ("glm-5.1", "GLM-5.1", "Zhipu GLM coding model"),
    ("kimi-k3", "Kimi K3", "Moonshot Kimi coding model"),
    ("kimi-k2.7-code", "Kimi K2.7 Code", "Moonshot Kimi coding model"),
    ("kimi-k2.6", "Kimi K2.6", "Moonshot Kimi coding model"),
    ("deepseek-v4-pro", "DeepSeek V4 Pro", "DeepSeek coding model"),
    ("deepseek-v4-flash", "DeepSeek V4 Flash", "DeepSeek coding model"),
    ("mimo-v2.5", "MiMo-V2.5", "Xiaomi MiMo coding model"),
    ("mimo-v2.5-pro", "MiMo-V2.5-Pro", "Xiaomi MiMo coding model"),
    ("hy3", "Hy3", "OpenCode tested coding model"),
]

OPENCODE_RESPONSES_MODELS = [
    ("grok-4.5", "Grok 4.5", "xAI Grok via OpenCode Go (Responses wire)"),
    ("gpt-5.6-luna", "GPT 5.6 Luna", "OpenAI GPT via OpenCode Go (Responses wire)"),
]


def _catalog_entry(
    slug: str,
    display_name: str,
    description: str,
    base_instructions_text: str | None = None,
    provider: str | None = None,
) -> dict:
    """Build a Codex-compatible model catalog entry (see `codex debug models`).

    context_window берётся из models.dev по id модели; для неизвестных
    моделей — DEFAULT_CONTEXT_WINDOW.
    """
    instructions = base_instructions_text or (
        "You are Codex, an agent that collaborates with the user in their workspace. "
        "You work autonomously to fulfill the user's request, use the provided tools, "
        "and report results concisely."
    )
    context_window, _ = model_limits(slug, provider)
    input_modalities = model_input_modalities(slug, provider)
    return {
        "slug": slug,
        "display_name": display_name,
        "description": description,
        "base_instructions": instructions,
        "default_reasoning_level": "low",
        "supported_reasoning_levels": [
            {"effort": "none", "description": "No reasoning"},
            {"effort": "low", "description": "Fast responses with lighter reasoning"},
            {"effort": "medium", "description": "Balances speed and reasoning depth for everyday tasks"},
            {"effort": "high", "description": "Greater reasoning depth for complex problems"},
        ],
        "default_reasoning_summary": "none",
        "default_verbosity": "low",
        "support_verbosity": True,
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 100,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "apply_patch_tool_type": "freeform",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "supports_parallel_tool_calls": True,
        "supports_image_detail_original": False,
        "supports_search_tool": False,
        "context_window": context_window,
        "max_context_window": context_window,
        "comp_hash": "0",
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": input_modalities,
        "use_responses_lite": False,
        "node_repl_auto_review_required": False,
        "node_repl_disabled": False,
        "tool_mode": "code_mode_only",
        "multi_agent_version": "v2",
        "upgrade": None,
    }


def chat_catalog() -> list[dict]:
    return [_catalog_entry(*m) for m in OPENCODE_CHAT_MODELS]


def responses_catalog() -> list[dict]:
    return [_catalog_entry(*m) for m in OPENCODE_RESPONSES_MODELS]