# ChangeModel — любые модели для Codex

ChangeModel — маленькое приложение и локальный прокси, которые позволяют
использовать в Codex CLI модели любых внешних провайдеров — OpenCode Go/Zen,
OpenRouter, GMI Cloud, Agent Router и других, — а не только встроенные.

Все добавленные модели сразу видны в Codex в выпадающем списке; переключение
происходит внутри Codex, без перезапусков. Прокси сам маршрутизирует каждый
запрос к нужному провайдеру по id модели.

**English summary below.**

## Как это устроено

- **Окно ChangeModel** (`gui.py`) — список провайдеров и моделей, ключи,
  кнопки запуска/остановки прокси, подбор моделей из каталогов провайдеров
  и лента «Бесплатные сегодня» (какие модели сегодня бесплатны в OpenCode Zen
  и OpenRouter).
- **Прокси** (`proxy/app.py`) — фоновый сервис на `127.0.0.1:4096`, через
  который Codex ходит к моделям:
  - `kind: opencode-chat` — трансляция Responses API ↔ Chat Completions;
  - `kind: passthrough` — проброс запросов Responses API как есть
    (OpenRouter, Agent Router и любые совместимые сервисы).

Ключи хранятся только локально (`proxy/.env`) и в код запросов не вшиваются.

## Установка

### Готовый exe (Windows)

1. Скопируйте папку с `ChangeModel.exe` куда угодно.
2. Запустите — рядом появится `providers.json` (создастся сам).
3. Нажмите **«Добавить провайдера»**, впишите Base URL и API-ключ.
4. Добавьте модели (вручную или кнопкой **«Подобрать»** из каталога провайдера).
5. Выберите модель → **«Сделать моделью по умолчанию»** → **«Запустить прокси»**.

Сборка exe из исходников: `pip install pyinstaller && pyinstaller ChangeModel.spec`.

### Из исходников (Windows/Linux/macOS)

```bash
git clone https://github.com/lana-info/ChangeModel.git
cd ChangeModel
python -m venv .venv && .venv/bin/pip install -r proxy/requirements.txt tomlkit   # Windows: .venv\Scripts\pip
cp proxy/.env.example proxy/.env    # впишите свои ключи
python gui.py                       # окно (или python proxy/run_proxy.py — только прокси)
python generator.py --profile changemodel   # переключить Codex на прокси
```

Откат Codex на встроенные модели: `python generator.py --profile base`.

## Совместимость

- **Windows 10 / 11 (64-bit)** — готовый `ChangeModel.exe` из раздела
  [Releases](https://github.com/lana-info/ChangeModel/releases): скачали,
  запустили, пользуетесь. Python не нужен.
- **Linux (VPS/сервер)** — прокси и генератор работают из исходников
  (`python proxy/run_proxy.py`, `python generator.py`); окна там нет,
  настройка — через файлы. Подробнее: [VDS.md](VDS.md).
- **macOS** — окно и прокси работают из исходников (`python3 gui.py`);
  нужен Python с tkinter (в сборке с python.org он встроен; для Homebrew
  доустановите пакет `python-tk`). Отдельной сборки `.app` пока нет.
- Везде дополнительно нужны: отдельно установленный **Codex CLI**,
  интернет и ваши собственные API-ключи (хранятся только локально).

## Провайдеры и ключи

Каждый провайдер в `providers.json` — это `base_url`, тип подключения
(`passthrough` / `opencode-chat`) и имя переменной с ключом (`env_key`).
Значения ключей берутся из `proxy/.env` (приоритет) или из переменных
окружения. Прокси перечитывает `.env` и `providers.json` по времени
изменения — обновлять можно без перезапуска.

Примеры провайдеров в комплекте: OpenRouter (passthrough), OpenCode Go/Zen
(трансляция), GMI Cloud (`https://api.gmi-serving.com/v1`), Agent Router
(`https://agentrouter.org/v1`). Для новых достаточно Base URL и ключа —
модели подберёт кнопка **«Подобрать»**.

## Запуск на сервере (VDS)

Прокси работает как systemd-сервис, Codex CLI на том же сервере смотрит на
`http://127.0.0.1:4096/v1`. Пошаговая инструкция: [VDS.md](VDS.md)
(и [SETUP-VDS.md](SETUP-VDS.md) — готовый промпт для агента).

## Разработка

```bash
python proxy/test_translation.py    # тесты трансляции Responses <-> Chat
python proxy/test_regressions.py    # регрессионные тесты прокси и генератора
```

Зависимости: FastAPI, uvicorn, httpx, tomlkit; Python 3.11+.

## Безопасность

- Прокси слушает только `127.0.0.1`; запросы с чужим `Host`/`Origin`
  (CSRF/DNS-rebinding из браузера) отклоняются.
- Ключи не логируются и не попадают в конфиги; файл `proxy/.env` исключён
  из репозитория (см. `.gitignore` и шаблон `proxy/.env.example`).

## Лицензия

MIT — см. [LICENSE](LICENSE).

---

# ChangeModel (English)

ChangeModel is a small GUI app plus a local proxy that lets Codex CLI use
models from any external provider — OpenCode Go/Zen, OpenRouter, GMI Cloud,
Agent Router and other OpenAI-compatible services — instead of only the
built-in ones. All your models appear in Codex's model picker; the proxy
routes each request to the right provider by model id (Responses↔Chat
translation or passthrough).

Quick start from source: `pip install -r proxy/requirements.txt tomlkit`,
copy `proxy/.env.example` to `proxy/.env`, add your keys, run `python gui.py`
(or deploy as a systemd service — see [VDS.md](VDS.md)). API keys stay
local and are never committed. Python 3.11+. MIT license.

## Compatibility

- **Windows 10 / 11 (64-bit)** — ready-made `ChangeModel.exe` from
  [Releases](https://github.com/lana-info/ChangeModel/releases): download,
  run, done. No Python needed.
- **Linux (VPS/server)** — proxy and generator run from source
  (`python proxy/run_proxy.py`, `python generator.py`); no GUI there,
  file-based setup. See [VDS.md](VDS.md).
- **macOS** — GUI and proxy run from source (`python3 gui.py`); needs a
  Python with tkinter (included in python.org builds; Homebrew needs the
  `python-tk` package). No `.app` bundle yet.
- Everywhere you additionally need: a separately installed **Codex CLI**,
  internet access, and your own provider API keys (stored locally only).
