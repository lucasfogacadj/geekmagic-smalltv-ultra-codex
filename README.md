# smalltv-dashboard

Animated 240x240 dashboard for GeekMagic SmallTV showing Codex usage windows:

- 05-hour remaining usage
- weekly remaining usage
- each window reset time

The container renders a GIF and sends it to SmallTV album `/image/dashboard.gif`, then applies theme `3`.

## Requirements

- Docker and Docker Compose
- Access to your local Codex data files (`~/.codex`)
- A running `systemd --user` if you want automatic `/status` refresh

## Environment configuration

The project is designed to be configured through environment variables.
Copy the example file and edit it before running:

```bash
cp .env.example .env
```

Minimum required variables:

- `SMALLTV_HOST`
- `CODEX_HOST_LOG_PATH`
- `CODEX_HOST_LOG_WAL_PATH`
- `CODEX_HOST_LOG_SHM_PATH`
- `CODEX_HOST_SESSIONS_PATH`

`CODEX_HOST_STATUS_PATH` is optional and defaults to `./data/codex-status.json` if not set.

### Run

```bash
docker compose up -d --build
```

Current GIF output is written to `out/dashboard.gif` (ignored from git).

## Environment variables

Container:
- `SMALLTV_HOST` (required): SmallTV IP.
- `CODEX_LOG_PATH` (container): path for `logs_2.sqlite` inside container. Default `/var/lib/codex/logs_2.sqlite`.
- `CODEX_SESSIONS_PATH` (container): path for `sessions` inside container. Default `/var/lib/codex/sessions`.
- `CODEX_STATUS_PATH` (container): path for `/status` snapshot inside container. Default `/var/lib/codex/codex-status.json`.
- `CODEX_STATUS_MAX_AGE_SECONDS`: how long `/status` is preferred before fallback. Default `420`.
- `CODEX_LOOKBACK_DAYS`: rolling day count for history section. Default `7`.
- `DISPLAY_TZ`: timezone for local rendering. Default `America/Sao_Paulo`.
- `REFRESH_SECONDS`: GIF refresh interval. Default `30`.
- `FRAME_MS`: frame duration in ms. Default `700`.
- `FRAMES_PER_PAGE`: frames per page. Default `8`.
- `MAX_GIF_BYTES`: maximum payload bytes accepted by SmallTV. Default `450000`.
- `UPLOAD_ENABLED`: set to `false` for local rendering only. Default `true`.
- `RUN_ONCE`: set to `true` for one-shot execution. Default `false`.
- `GIF_FILENAME`: container-side filename, default `dashboard.gif`.
- `OUTPUT_PATH`: where GIF is written in the container, default `/var/cache/smalltv-dashboard/dashboard.gif`.

Host mount bindings:
- `CODEX_HOST_LOG_PATH`
- `CODEX_HOST_LOG_WAL_PATH`
- `CODEX_HOST_LOG_SHM_PATH`
- `CODEX_HOST_SESSIONS_PATH`
- `CODEX_HOST_STATUS_PATH` (optional, default `./data/codex-status.json`)

Build-time:
- `CODEX_ICON_URL` (default `https://persistent.oaistatic.com/codex/icon-gif.mp4`) for Codex icon frames.

## Refreshing quota state using `/status`

For real-time usage values, run the host-side status refresh script every few minutes.

### Install user timer

```bash
mkdir -p ~/.config/systemd/user
mkdir -p ~/.config/smalltv-dashboard
cp systemd/codex-status-refresh.env.example ~/.config/smalltv-dashboard/codex-status-refresh.env
cp systemd/codex-status-refresh.service ~/.config/systemd/user/
cp systemd/codex-status-refresh.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-status-refresh.timer
```

Edit `~/.config/smalltv-dashboard/codex-status-refresh.env` and point it to your local paths.

Optional:
- `CODEX_STATUS_CODEX_BIN` for a non-standard `codex` binary location
- `CODEX_STATUS_TIMEOUT` number of seconds to wait for command output

The container prefers `/var/lib/codex/codex-status.json` when fresh; it falls back to local Codex session JSONL events if stale or missing.
