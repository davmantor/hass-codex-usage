# Codex Usage - Home Assistant Integration

A custom Home Assistant integration that monitors your OpenAI Codex subscription usage.

## Sensors

- **Session Usage** - Current primary Codex usage window utilization (%)
- **Session Reset Time** - When the primary usage window resets
- **Weekly Usage** - Current secondary Codex usage window utilization (%)
- **Weekly Usage Pace** - How far weekly usage is ahead of or behind the reset window
- **Weekly Reset Time** - When the weekly usage window resets
- **Credits Balance** - Remaining Codex credits when reported by the API
- **Credits Enabled** - Whether credits are available for the account
- **Rate Limit Reached** - Current backend-reported limit state

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Restart Home Assistant
3. Install "Codex Usage"
4. Go to Settings -> Devices & Services -> Add Integration -> "Codex Usage"
5. Follow the instructions

### Manual

1. Copy `custom_components/hass_codex_usage/` to your HA `custom_components/` directory
2. Restart Home Assistant
3. Add the integration via the UI

## Setup

The integration uses Codex OAuth credentials created by the [Codex CLI](https://github.com/davmantor/codex-cli).

### 1. Generate Auth Token
On the machine where the Codex CLI is installed, run:
```bash
codex login
```
This generates an authentication file at `~/.codex/auth.json`.

### 2. Copy Auth File to Home Assistant
If you are running Home Assistant in **Docker** or **Home Assistant OS**, the integration cannot directly access the host's `~/.codex` directory. Copy the auth file to your Home Assistant `/config` directory once.

**Example Copy Script (`/usr/local/bin/copy-codex-auth.sh`):**
```bash
#!/bin/bash
SOURCE="/root/.codex/auth.json"
DEST="/config/.codex/auth.json"

mkdir -p "$(dirname "$DEST")"
cp "$SOURCE" "$DEST"
chmod 644 "$DEST"
```

The integration reads this file at each poll and refreshes expired or near-expired Codex OAuth access tokens in place using the stored `refresh_token`. You do not need a cron job to keep the access token synchronized. If the refresh token is revoked or expires, run `codex login` again and replace the auth file.

### 3. Add Integration
1. Go to **Settings -> Devices & Services -> Add Integration -> "Codex Usage"**.
2. When prompted for the **Auth File Path**, enter:
   ```text
   /config/.codex/auth.json
   ```

The integration reads this file at each poll. It does not store your access token in the Home Assistant database.

## Options

- **Update interval** - How often to poll the usage API (default: 300 seconds, min: 60, max: 3600).

## Dashboard

A pre-built dashboard is included in the `dashboards/` directory. To use it:

1. Go to Settings -> Dashboards -> Add Dashboard
2. Click the three-dot menu -> "Edit Dashboard"
3. Click the three-dot menu again -> "Raw configuration editor"
4. Copy the contents of `dashboards/codex_usage.yaml` and paste it
5. Click "Save"

Alternatively, you can manually add the cards to any existing dashboard by referencing the YAML file.

## Rate Limit

Codex usage APIs are not documented as a public Home Assistant integration surface. Keep the default 300 second polling interval unless you have a specific reason to change it.

## Development

### Pre-commit Hook

Install the pre-commit hook to automatically format code before committing:

```bash
pip install pre-commit
pre-commit install
```

This will run black, isort, ruff, and other checks before committing.

### Manual Formatting

```bash
pip install black isort ruff
black custom_components/hass_codex_usage/
isort custom_components/hass_codex_usage/
ruff check --fix custom_components/hass_codex_usage/
```

## Credits

This integration is a modified version of [hass-claude-usage](https://github.com/trickv/hass-claude-usage) by [Patrick van Staveren](https://github.com/trickv). It has been adapted to work with the OpenAI Codex backend usage API.

## License

MIT License - see [LICENSE](LICENSE) file for details.
