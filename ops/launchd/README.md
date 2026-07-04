# ops/launchd — Cairn LaunchAgents (M3)

Two agents replace the four legacy `com.masato.brain-sync.*` jobs
(DESIGN.md §5.7):

| Agent | Schedule | Command |
|---|---|---|
| `com.masato.cairn.sync` | hourly + at load | `cairn sync all`（4 ソース同期 + 90 Auto 一覧） |
| `com.masato.cairn.weekly` | 日曜 18:00 + at load | `cairn review weekly`（M4 で実装。それまでは exit 1） |

- Templates: `*.plist.template` — absolute paths and per-host settings are
  `{{VARS}}`, rendered by `install.sh`.
- `install.sh` renders + copies to `~/Library/LaunchAgents` but does **not**
  load anything: `launchctl bootstrap` / `bootout` は人間の明示操作
  （AGENTS.md 不変条件 8）。
- API keys are read from the Keychain at runtime (D8) — the plists carry
  only non-secret settings (URLs, IDs, paths).
- Logs: `~/Library/Logs/cairn/{sync,weekly}[-error].log`
- 移行手順（旧 4 本の unload・削除・ロールバック）: `MIGRATION.md`
