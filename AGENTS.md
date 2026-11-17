# Working on ThreadDock

Python 3.12+ and uv are enough for Python unit tests and linting. No Telegram
token, Docker daemon, VPS access or production data is required for those checks.

```sh
uv sync --locked
uv run python scripts/check.py
```

For media and browser integration checks, use the Dev Container, or install
FFmpeg/ffprobe, Deno 2 and Node.js 24, then run:

```sh
uv sync --locked --extra media
npm ci
npx playwright install chromium
uv run python scripts/check.py --full
```

On Linux, Playwright may also need `npx playwright install-deps chromium`.
The Dev Container installs these dependencies during creation.

- `handlers/` handles Telegram updates and permissions; `main.py` wires services.
- `services/` handles downloads, media processing, queues and delivery.
- `storage/` owns downloader SQLite tables; `archive/` owns archive tables and API.
- Bot text belongs in `core/texts.py`; Mini App text in `archive/web/texts.js`.
- Keep schema migrations compatible with existing data. Test migrations and owner
  isolation when changing the archive repository or API.
- Use `PublicHttp` or `SafeProxy` for external media. FFmpeg consumes local files.
- Do not use `.env`, `secrets/`, `data/` or `.deploy-work/` as test fixtures.
  Tests create temporary databases. Never commit tokens, cookies or message data.
- Keep dependency changes in `pyproject.toml` and `uv.lock` together. JavaScript
  browser tooling is locked in `package-lock.json`.
- Run the relevant checks before committing. State which integration checks were
  skipped; local fixtures do not establish that a social network is accessible.
- Push feature branches and use the repository CI. Merging to `main` can deploy to
  production when `DEPLOY_ENABLED=true`; PR checks do not deploy.

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and
[docs/architecture.md](docs/architecture.md) for service contracts.
