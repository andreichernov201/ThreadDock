# GitHub Actions и автоматическое развёртывание

Репозиторий: [andreichernov201/ThreadDock](https://github.com/andreichernov201/ThreadDock).
Каждый push в `main` и каждый PR запускают Python-тесты, проверки стиля,
тесты текстов/деморежима Mini App, проверку Compose и сборку Docker-образа.
На VPS передаётся тот же проверенный SHA коммита. Код из PR не получает SSH-секреты.
Actions закреплены по SHA; Dependabot предлагает обновления отдельными PR.

Пока VPS не подключён, шаг deploy пропускается. Для запуска нужна repository
variable `DEPLOY_ENABLED=true`. До её установки тесты и сборка уже работают.
Команда ручного запуска — Actions → CI → Run workflow, ветка `main`; она тоже
выполняет все проверки перед деплоем.

## Один раз на VPS

Установи Docker Engine с Compose plugin и подготовь пользователя для деплоя.
Он должен иметь доступ к Docker и владеть `/opt/threaddock`:

```sh
sudo mkdir -p /opt/threaddock/{releases,shared,backups}
sudo chown -R "$USER:$USER" /opt/threaddock
chmod 700 /opt/threaddock/shared /opt/threaddock/backups
```

Создай `/opt/threaddock/shared/.env` по примеру `.env.example`: `BOT_TOKEN`,
`ARCHIVE_DOMAIN`, необходимые лимиты. Права файла — `600`. Этот файл остаётся на
сервере; GitHub не читает и не перезаписывает токен бота.
Домен и HTTPS: [vps.md](vps.md).

Создай отдельную SSH-пару для GitHub Actions и добавь публичную часть в
`authorized_keys` пользователя VPS. Закрытую часть храни только как GitHub Secret.
Доступ к Docker фактически даёт административные возможности на сервере:
используй отдельный ключ и не выдавай его участникам проекта.

## Настройки GitHub

В Settings → Environments создай `production`, разреши только ветку `main`.
При необходимости включи обязательное ручное одобрение production-деплоя.
В environment secrets укажи:

| Secret | Значение |
| --- | --- |
| `VPS_HOST` | IPv4 или DNS-имя VPS, без `ssh://` |
| `VPS_USER` | Пользователь с доступом к Docker и `/opt/threaddock` |
| `VPS_PORT` | SSH-порт; можно не задавать для 22 |
| `SSH_PRIVATE_KEY` | Отдельный закрытый SSH-ключ для деплоя |
| `SSH_KNOWN_HOSTS` | Проверенная запись host key сервера для этого адреса/порта |

Сверь fingerprint SSH host key через консоль хостинга или другой доверенный канал.
Не отключай `StrictHostKeyChecking`: workflow использует закреплённый host key.
Самостоятельно полученная непроверенная запись `ssh-keyscan` не подтверждает
подлинность сервера.

После заполнения секретов создай **repository variable** `DEPLOY_ENABLED=true`
в Settings → Secrets and variables → Actions → Variables. Это именно переменная
репозитория: environment variables ещё недоступны при вычислении условия job.

## Что делает деплой

1. Загружает исходники проверенного коммита в `/opt/threaddock/releases/SHA`.
2. Берёт серверный `.env`, проверяет Compose и собирает образ, пока старый бот работает.
3. Останавливает старого бота и сохраняет базу, WAL и архивные файлы в `backups`.
   Пересоздаваемый кеш загрузчика и временные uploads/exports в копию не входят.
4. Запускает контейнеры и ждёт Docker healthcheck до 180 секунд.
5. После успешной проверки обновляет симлинк `/opt/threaddock/current`.

Том данных имеет постоянное имя `threaddock_threaddock-data`. Используй проект
Compose `-p threaddock`, если запускаешь этот же экземпляр вручную. Не переноси
живую установку с другого имени Compose без переноса её тома.

Сборка до остановки оставляет старый бот работающим при ошибке сборки. При ошибке
после остановки новый бот останавливается, резервная копия сохраняется. Откат базы
не выполняется автоматически: бот мог уже принять новые Telegram-сообщения.
Проверь логи, сохрани текущее состояние и восстанови нужную согласованную копию
перед запуском старого кода. Архивы резервных копий содержат приватные переписки;
храни их защищённо. Скрипт не удаляет старые копии — контролируй место на диске.

Состояние:

```sh
cd /opt/threaddock/current
docker compose -p threaddock -f compose.yaml -f compose.vps.yaml ps
docker compose -p threaddock -f compose.yaml -f compose.vps.yaml logs --tail=100 bot
```

Справка: [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax),
[минимальные права Actions](https://docs.github.com/en/actions/reference/security/secure-use).
