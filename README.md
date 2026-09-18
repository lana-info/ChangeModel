# Vibix ChangeModel — любые модели для Codex

Vibix ChangeModel — маленькое приложение и локальный прокси, которые позволяют
использовать в Codex CLI модели любых внешних провайдеров — OpenCode Go/Zen,
OpenRouter, GMI Cloud, Agent Router и других, — а не только встроенные.

Все добавленные модели сразу видны в Codex в выпадающем списке; переключение
происходит внутри Codex, без перезапусков. Прокси сам маршрутизирует каждый
запрос к нужному провайдеру по id модели.

**English summary below.**

## Как это устроено

- **Окно Vibix ChangeModel** (`gui.py`) — список провайдеров и моделей, ключи,
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
2. Запустите — рядом появится `providers.json` (создастся сам из встроенного шаблона).
3. Нажмите **«Добавить провайдера»**, впишите Base URL и API-ключ.
4. Добавьте модели (вручную или кнопкой **«Подобрать»** из каталога провайдера).
5. Выберите модель → **«Сделать моделью по умолчанию»** → **«Запустить прокси»**.

Сборка exe из исходников: `pip install pyinstaller && pyinstaller ChangeModel.spec`.

### Из исходников (Windows/Linux/macOS)

```bash
git clone https://github.com/lana-info/ChangeModel.git
cd ChangeModel
python -m venv .venv && .venv/bin/pip install -r proxy/requirements.txt tomlkit   # Windows: .venv\Scripts\pip
cp providers.example.json providers.json   # рабочий конфиг (в git не входит)
cp proxy/.env.example proxy/.env    # впишите свои ключи
python gui.py                       # окно (или python proxy/run_proxy.py — только прокси)
python generator.py --profile changemodel   # переключить Codex на прокси
```

`python gui.py` при первом запуске сам создаст `providers.json` из шаблона,
если вы его ещё не скопировали.

Откат Codex на встроенные модели: `python generator.py --profile base`.

## Совместимость

- **Windows 10 / 11 (64-bit)** — готовый `ChangeModel.exe` из раздела
  [Releases](https://github.com/lana-info/ChangeModel/releases): скачали,
  запустили, пользуетесь. Python не нужен.
- **Linux (VPS/сервер)** — прокси и генератор работают из исходников
  (`python proxy/run_proxy.py`, `python generator.py`); окна там нет,
  настройка — через файлы. Подробнее: [VDS.md](VDS.md).
- **macOS (Apple Silicon)** — файл `ChangeModel-macOS.dmg` из раздела
  [Releases](https://github.com/lana-info/ChangeModel/releases): откройте
  образ и перетащите `ChangeModel.app` в Программы. При первом запуске
  macOS заблокирует программу без подписи Apple: кликните по ней правой
  кнопкой → «Открыть» → «Открыть». Также работает запуск из исходников
  (`python3 gui.py`; нужен Python с tkinter).
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

### Где взять API-ключи

Ключ вставляется в окне ChangeModel: выберите провайдера → «Редактировать» →
поле «API-ключ» → Сохранить (или задайте переменную окружения с тем же именем).
Где получить сами ключи:

- **OpenRouter** — регистрация на openrouter.ai → раздел Keys
  (`openrouter.ai/settings/keys`) → «Create Key». Модели с суффиксом `:free`
  бесплатны; для платных нужно пополнить баланс (Credits).
- **OpenCode Go / Zen** — ключи выдаются в личном кабинете OpenCode
  (opencode.ai): отдельный ключ для Go (`OPENCODE_GO_API_KEY`) и для Zen
  (`OPENCODE_ZEN_API_KEY`). Модели Zen с суффиксом `-free` доступны сразу;
  платные требуют баланса. Модели семейства Muse Spark дополнительно требуют
  согласия на использование данных — оно оформляется в настройках вашего
  workspace OpenCode, без него сервис отвечает ошибкой.

  **Как получить бесплатный ключ Zen:**
  1. Перейдите на [opencode.ai/auth](https://opencode.ai/auth) и
     зарегистрируйтесь (через GitHub или Google — без банковской карты).
  2. В личном кабинете выберите **OpenCode Zen** → **API Keys**.
  3. Нажмите **«Create Key»** → скопируйте ключ (формат `sk-...`).
  4. В окне ChangeModel: выберите провайдер **OpenCode Zen** →
     «Редактировать» → вставьте ключ в поле «API-ключ» → Сохранить.
     Либо пропишите в `proxy/.env`:
     ```
     OPENCODE_ZEN_API_KEY=sk-ваш-ключ
     ```
  5. Бесплатные модели (с суффиксом `-free` и `big-pickle`) работают
     сразу — баланс пополнять не нужно.

  **Лимит:** бесплатные модели делят квоту ~200 запросов в сутки на IP.
  Для больше — OpenCode Go ($5/мес).
- **GMI Cloud** — регистрация в консоли console.gmicloud.ai → Settings →
  API Keys (подробности: docs.gmicloud.ai, раздел Quickstart).
- **Agent Router** — ключ выдаётся на сайте сервиса (agentrouter.org).

Если ключ неверный или пустой, модели провайдера вернут ошибку авторизации —
проверьте ключ и попробуйте ещё раз.

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

Релизы (`v*`, Windows exe + macOS DMG) собираются автоматически workflow
[.github/workflows/release.yml](.github/workflows/release.yml). Перед тегом
обновите `APP_VERSION` в `gui.py` (проверяется скриптом
`scripts/check_version.py`).

## Безопасность

- Прокси слушает только `127.0.0.1`; запросы с чужим `Host`/`Origin`
  (CSRF/DNS-rebinding из браузера) отклоняются.
- Токена у прокси нет: обратиться к нему (включая трату ключей провайдеров
  и `/shutdown`) может **любой локальный процесс** — осознанный компромисс
  ради простоты. Проверка `Host`/`Origin` закрывает атаки из браузера,
  но не локальные программы; не запускайте прокси на машинах, которым не доверяете.
- Ключи не логируются и не попадают в конфиги; файлы `proxy/.env` и
  `providers.json` исключены из репозитория (см. `.gitignore` и шаблоны
  `proxy/.env.example`, `providers.example.json`).

Замечание про `CHANGE_MODEL_API_KEY`: Codex требует, чтобы такая переменная
окружения существовала (прокси её не проверяет). На Windows GUI прописывает
плейсхолдер сам (`setx`); на macOS/Linux добавьте вручную, например в
`~/.zshrc`: `export CHANGE_MODEL_API_KEY=changemodel-local`.

## Лицензия

MIT — см. [LICENSE](LICENSE).

---

# Vibix ChangeModel (English)

Vibix ChangeModel is a small GUI app plus a local proxy that lets Codex CLI use
models from any external provider — OpenCode Go/Zen, OpenRouter, GMI Cloud,
Agent Router and other OpenAI-compatible services — instead of only the
built-in ones. All your models appear in Codex's model picker; the proxy
routes each request to the right provider by model id (Responses↔Chat
translation or passthrough).

Quick start from source: `pip install -r proxy/requirements.txt tomlkit`,
copy `proxy/.env.example` to `proxy/.env`, add your keys, run `python gui.py`
(or deploy as a systemd service — see [VDS.md](VDS.md)). API keys stay
local and are never committed. Python 3.11+. MIT license.

Releases (`v*`, Windows exe + macOS DMG) are built automatically by the
[release workflow](.github/workflows/release.yml).

## Compatibility

- **Windows 10 / 11 (64-bit)** — ready-made `ChangeModel.exe` from
  [Releases](https://github.com/lana-info/ChangeModel/releases): download,
  run, done. No Python needed.
- **Linux (VPS/server)** — proxy and generator run from source
  (`python proxy/run_proxy.py`, `python generator.py`); no GUI there,
  file-based setup. See [VDS.md](VDS.md).
- **macOS (Apple Silicon)** — `ChangeModel-macOS.dmg` from
  [Releases](https://github.com/lana-info/ChangeModel/releases): open the
  image and drag `ChangeModel.app` to Applications. On first launch macOS
  blocks the unsigned app: right-click it → Open → Open. Running from
  source (`python3 gui.py`) also works with a tkinter-enabled Python.
- Everywhere you additionally need: a separately installed **Codex CLI**,
  internet access, and your own provider API keys (stored locally only).

### Where to get API keys

Paste a key in the ChangeModel window: select a provider → Edit → the
“API key” field → Save (or set an environment variable with the same name).
Where the keys come from:

- **OpenRouter** — sign up at openrouter.ai → the Keys section
  (`openrouter.ai/settings/keys`) → “Create Key”. Models with the `:free`
  suffix are free; paid ones need Credits balance.
- **OpenCode Go / Zen** — keys are issued in your OpenCode account
  (opencode.ai): a separate key for Go (`OPENCODE_GO_API_KEY`) and for Zen
  (`OPENCODE_ZEN_API_KEY`). Zen models with the `-free` suffix work right
  away; paid ones need balance. Muse Spark models additionally require
  data-usage consent in your OpenCode workspace settings, otherwise the
  service returns an error.

  **How to get a free Zen key:**
  1. Go to [opencode.ai/auth](https://opencode.ai/auth) and sign up
     (via GitHub or Google — no credit card required).
  2. In your dashboard, select **OpenCode Zen** → **API Keys**.
  3. Click **"Create Key"** → copy the key (format `sk-...`).
  4. In the ChangeModel window: select the **OpenCode Zen** provider →
     Edit → paste the key in the "API key" field → Save.
     Or set it in `proxy/.env`:
     ```
     OPENCODE_ZEN_API_KEY=sk-your-key-here
     ```
  5. Free models (with `-free` suffix and `big-pickle`) work immediately
     — no balance top-up needed.

  **Limit:** free models share a quota of ~200 requests per day per IP.
  For more — OpenCode Go ($5/month).
- **GMI Cloud** — sign up in the console at console.gmicloud.ai → Settings →
  API Keys (see docs.gmicloud.ai, Quickstart).
- **Agent Router** — the key is issued on the service website (agentrouter.org).

If a key is wrong or empty, that provider's models return an authorization
error — double-check the key and retry.
