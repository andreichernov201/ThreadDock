# Разработка

## Окружение

Для тестов Python нужны Python 3.12+ и [uv](https://docs.astral.sh/uv/getting-started/installation/).
Команды одинаковы в PowerShell, Linux и macOS:

```sh
git clone https://github.com/andreichernov201/ThreadDock.git
cd ThreadDock
uv sync --locked
uv run python scripts/check.py
```

Активация `.venv` не нужна. Тесты используют временные базы и подставные ответы
сервисов. Токен Telegram и доступ к VPS не требуются. При отсутствии FFmpeg,
yt-dlp или Deno соответствующие интеграционные тесты пропускаются с указанием причины.

Полная среда описана в `.devcontainer/`: открой проект через **Dev Containers:
Reopen in Container** в VS Code или создай GitHub Codespace. Она содержит Python,
uv, FFmpeg, Deno, Node.js и Chromium; приложение автоматически не запускается.
Docker нужен только для локального Dev Container и сборки production-образа.

Для полной проверки на своей машине установи FFmpeg/ffprobe, Deno 2 и Node.js 24:

```sh
uv sync --locked --extra media
npm ci
npx playwright install chromium
uv run python scripts/check.py --full
```

На Linux системные зависимости Chromium устанавливаются командой
`npx playwright install-deps chromium`. Снимки интерфейса сохраняются в
`.deploy-work/threaddock-ui/`, в CI — в артефакте `archive-ui`.

## Изменения

Создай ветку от актуальной `main`. Перед PR запусти `scripts/check.py`, для
изменений медиа или интерфейса — с `--full`. Укажи воспроизводимый сценарий,
результат и выполненные проверки. Сетевую приёмку перечисляй отдельно: локальная
фикстура не проверяет доступность TikTok или Telegram.

Python форматируется через `uv run ruff format .`, JavaScript интерфейса — через
`deno fmt --no-config archive/web tests/web_test.js`. Тексты интерфейса хранятся
отдельно от обработчиков. Контракты модулей: [docs/architecture.md](docs/architecture.md).

После изменения Python-зависимостей выполни `uv lock`, затем `uv sync --locked`.
Зависимости загрузчика находятся в extra `media`; Docker использует тот же lock.
При изменении Playwright обнови `package.json` и `package-lock.json` через npm.

## CI и данные

CI проверяет Python 3.12–3.15 на Linux, а также Python 3.14 на Windows и macOS.
Linux отдельно проверяет браузер, медиа, Compose и production-образ.
PR не публикует образ и не получает секреты production. Push в `main` публикует
образ и при `DEPLOY_ENABLED=true` запускает [деплой](docs/deployment.md).

Не коммить `.env`, cookies, `data/`, резервные копии или содержимое переписок.
Для ручного запуска скопируй `.env.example` в `.env` и заполни свой токен.
Используй отдельный `DATA_DIR`, если рабочий экземпляр уже запущен.
