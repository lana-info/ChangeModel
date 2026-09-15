# ChangeModel на VDS (Linux)

Прокси работает на сервере как фоновый сервис. Codex CLI на этом же сервере
смотрит на http://127.0.0.1:4096/v1 — прокси раздаёт все модели всех
провайдеров из providers.json. Переключение моделей — прямо в Codex
(выпадающий список), прокси сам маршрутизирует каждую модель к её провайдеру.

## Шаг 1. Скопировать файлы на сервер

Нужны:
```
proxy/app.py
proxy/run_proxy.py
models_data.py
providers.json
generator.py
```

Скопируйте их в папку, например `/opt/changemodel/`:
```bash
scp proxy/app.py proxy/run_proxy.py models_data.py providers.json generator.py user@server:/opt/changemodel/
```

## Шаг 2. Установить зависимости

Нужен Python 3.11+ (`python3 --version`). Зависимости ставим в venv,
чтобы не конфликтовать с системным Python:

```bash
cd /opt/changemodel
python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn httpx tomlkit
```

## Шаг 3. Задать API-ключи

Прокси читает файл `/opt/changemodel/proxy/.env` и подхватывает изменения
без перезапуска:

```bash
nano /opt/changemodel/proxy/.env
```
```ini
OPENROUTER_API_KEY=sk-or-...
OPENCODE_GO_API_KEY=sk-...
```
```bash
chmod 600 /opt/changemodel/proxy/.env   # читать только вам
```

Формат простой: `КЛЮЧ=значение`, по одному в строке, без инлайн-комментариев.
Значения из `.env` имеют приоритет над переменными окружения: если ключ задан
и там, и там, используется `.env` (так работает горячая замена ключей без
перезапуска). Переменные окружения — запасной вариант.

Если какого-то провайдера нет — просто удалите его из `providers.json`
(или оставьте без ключа: его модели будут показываться, но не работать).

## Шаг 4. Запуск как сервис (systemd)

Создайте `/etc/systemd/system/changemodel.service`:

```ini
[Unit]
Description=ChangeModel proxy
After=network.target

[Service]
WorkingDirectory=/opt/changemodel
ExecStart=/opt/changemodel/.venv/bin/python /opt/changemodel/proxy/run_proxy.py
# ключи можно задать здесь вместо .env (используются, если ключа нет в .env):
#Environment=OPENROUTER_API_KEY=sk-or-...
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Включите и запустите:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now changemodel
systemctl status changemodel
curl http://127.0.0.1:4096/healthz   # -> {"status":"ok"}
```

Проверка каталога моделей (замените ключ на любой из ваших):
```bash
curl -H "Authorization: Bearer $OPENROUTER_API_KEY" http://127.0.0.1:4096/v1/models
```

## Шаг 5. Настроить Codex CLI

Если Codex CLI ещё не установлен на сервере — нужен Node.js 22+ и:
```bash
npm install -g @openai/codex
```

Затем подключите Codex к прокси:
```bash
cd /opt/changemodel && .venv/bin/python generator.py --profile changemodel
```

Либо вручную в `~/.codex/config.toml`:
```toml
model = "deepseek-v4-flash"
model_provider = "changemodel"

[model_providers.changemodel]
name = "ChangeModel (все провайдеры через прокси)"
base_url = "http://127.0.0.1:4096/v1"
env_key = "CHANGE_MODEL_API_KEY"
wire_api = "responses"
```

`CHANGE_MODEL_API_KEY` можно не задавать — прокси принимает любые запросы
с локального адреса (для удалённого доступа нужен HTTPS/VPN, см. ниже).
`model` — модель по умолчанию; в самом Codex можно выбрать любую из каталога.

## Как менять список провайдеров/моделей на сервере

GUI-окна на VDS нет, поэтому редактируйте `providers.json` напрямую.
Прокси подхватывает правки `providers.json` и `.env` автоматически (по времени
изменения файла) — перезапуск сервиса нужен только после обновления кода.

Удобный вариант: вести список моделей локально в окне ChangeModel (Windows)
и копировать готовый `providers.json` на сервер:
```bash
scp providers.json user@server:/opt/changemodel/
```
Ключи при этом остаются в `/opt/changemodel/proxy/.env` на сервере.

Структура:
```json
{
  "default_model": "deepseek-v4-flash",
  "base_instructions": "",
  "providers": [
    {
      "id": "openrouter",
      "name": "OpenRouter",
      "kind": "passthrough",
      "base_url": "https://openrouter.ai/api/v1",
      "env_key": "OPENROUTER_API_KEY",
      "wire_api": "responses",
      "models": [
        { "id": "cohere/north-mini-code:free", "name": "North Mini Code (free)" }
      ]
    },
    {
      "id": "opencode",
      "name": "OpenCode Go",
      "kind": "opencode-chat",
      "base_url": "https://opencode.ai/zen/go/v1",
      "env_key": "OPENCODE_GO_API_KEY",
      "wire_api": "responses",
      "models": [
        { "id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash" }
      ]
    }
  ]
}
```

- `kind: "passthrough"` — прокси пробрасывает запрос как есть на `base_url`
  (так работает OpenRouter и любые сервисы с Responses API).
- `kind: "opencode-chat"` — трансляция в Chat Completions на OpenCode Go.
- `default_model` — какая модель подставляется в Codex по умолчанию.
- `base_instructions` — своя системная инструкция для всех моделей
  (пустая строка = стандартная).

## Настройка инструкции для моделей

В `providers.json` на сервере есть поле `base_instructions` (в начале файла).
Заполните его — эта инструкция будет приложена ко всем моделям в каталоге
(как системная инструкция Codex). Пустая строка = стандартная инструкция.

## Удалённый доступ (не обязательно)

Прокси слушает `127.0.0.1` — только локально. Если нужно подключаться к
прокси с другого компьютера, измените в `run_proxy.py` host на `0.0.0.0`
И обязательно защитите доступ (прокси шлёт ваши ключи upstream) —
минимум через VPN или SSH-туннель:
```bash
ssh -L 4096:127.0.0.1:4096 user@server
```