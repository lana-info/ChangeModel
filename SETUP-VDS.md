# Задача: настроить ChangeModel-прокси на этом VDS для Codex CLI

Ты (Codex) настраиваешь на этом Linux-сервере фоновый сервис ChangeModel —
локальный прокси, через который Codex CLI получает доступ к моделям
внешних провайдеров (OpenRouter, OpenCode Go) по единому адресу
`http://127.0.0.1:4096/v1`. Прокси маршрутизирует каждый запрос к нужному
провайдеру по id модели. Codex CLI на этом сервере **уже установлен** —
проверь это (шаг 1), но не устанавливай заново.

Исходные файлы лежат в текущей рабочей папке:

```
proxy/app.py
proxy/run_proxy.py
models_data.py
providers.json
generator.py
```

## Правила

- Не выводи значения API-ключей в консоль, логи и отчёт.
- Ключи API возьми из переменных окружения, если они там есть; иначе
  **попроси у пользователя** и запиши в `.env` (шаг 4).
- Для системных действий используй `sudo`; если sudo требует пароль —
  попроси его у пользователя.
- Не изменяй ничего в `~/.codex/config.toml`, кроме ключей, указанных в шаге 7
  (файл принадлежит Codex CLI и пользователю).

## Шаг 1. Проверка окружения

```bash
python3 --version            # нужно >= 3.11
command -v codex && codex --version
ss -ltn | grep 4096 || echo "порт 4096 свободен"
```

Если Python < 3.11 — остановись и сообщи пользователю.
Если порт 4096 занят — выясни, чем, и сообщи пользователю.

## Шаг 2. Разместить файлы

```bash
sudo mkdir -p /opt/changemodel
sudo cp proxy/app.py proxy/run_proxy.py models_data.py providers.json generator.py /opt/changemodel/
sudo chown -R "$USER":"$USER" /opt/changemodel
```

## Шаг 3. Зависимости (venv)

```bash
cd /opt/changemodel
python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn httpx tomlkit
```

## Шаг 4. API-ключи

У каждого провайдера в `providers.json` есть поле `env_key` — имя переменной
с его ключом (обычно `OPENROUTER_API_KEY`, `OPENCODE_GO_API_KEY`).
Создай файл `/opt/changemodel/proxy/.env`:

```bash
mkdir -p /opt/changemodel/proxy
nano /opt/changemodel/proxy/.env      # или другой редактор
chmod 600 /opt/changemodel/proxy/.env
```

Формат — по одной строке на ключ, без кавычек и инлайн-комментариев:

```ini
OPENROUTER_API_KEY=sk-or-...
OPENCODE_GO_API_KEY=sk-...
```

Прокси перечитывает этот файл автоматически (по времени изменения) —
обновлять ключи можно без перезапуска сервиса.

## Шаг 5. Сервис systemd

Создай `/etc/systemd/system/changemodel.service`:

```ini
[Unit]
Description=ChangeModel proxy
After=network.target

[Service]
WorkingDirectory=/opt/changemodel
ExecStart=/opt/changemodel/.venv/bin/python /opt/changemodel/proxy/run_proxy.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now changemodel
```

## Шаг 6. Проверка прокси

```bash
systemctl is-active changemodel                                  # -> active
curl -s http://127.0.0.1:4096/healthz                            # -> {"status":"ok"}
KEY=$(grep -m1 '^OPENROUTER_API_KEY=' /opt/changemodel/proxy/.env | cut -d= -f2)
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:4096/v1/models | head -c 300
```

Каталог моделей должен вернуться непустым (не выводи `$KEY` в отчёт).

## Шаг 7. Подключить Codex CLI

```bash
cd /opt/changemodel && .venv/bin/python generator.py --profile changemodel
```

Скрипт создаст резервную копию `~/.codex/config.toml.bak` и пропишет в него
только свои ключи (`model`, `model_provider`, `model_providers.changemodel`),
сохранив всё остальное. Проверь результат:

```bash
grep -A5 'model_providers.changemodel' ~/.codex/config.toml
```

Ожидается `base_url = "http://127.0.0.1:4096/v1"`.
`model` в конфиге — модель по умолчанию; в самом Codex её можно сменить
в выпадающем списке (там будут все модели из `providers.json`).

## Шаг 8. Финальная проверка и отчёт

1. `curl -s http://127.0.0.1:4096/healthz` отвечает.
2. `systemctl is-enabled changemodel` -> `enabled` (автозапуск при перезагрузке).
3. В `~/.codex/config.toml` указан провайдер `changemodel`.

Отчёт пользователю: что сделано, какая модель стоит по умолчанию,
сколько моделей в каталоге, где лежат ключи и как их обновлять
(`/opt/changemodel/proxy/.env`, применяется без перезапуска),
как добавить модели (правка `providers.json`, применяется без перезапуска),
как откатить Codex на встроенные модели:
`cd /opt/changemodel && .venv/bin/python generator.py --profile base`.
