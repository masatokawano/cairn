# ops/launchd — LaunchAgent templates (placeholder)

At M0 this directory is a placeholder for the plist templates that land in M3.

## Coming in M3 (DESIGN.md §5.7)

Two LaunchAgents replace the four legacy `com.masato.brainsync.*` jobs (that
today live in `~/workspace/brain-sync/`):

- `com.masato.cairn.sync.plist.template` — hourly `cairn sync all`
- `com.masato.cairn.weekly.plist.template` — Sunday 18:00 + at login, runs `cairn review weekly`

Absolute paths (Python interpreter, project root, log directory) are variables
in the templates so the install script (also M3) can materialise them for a
specific host.

Logs will land in `~/Library/Logs/cairn/`.

## Legacy status

The four legacy LaunchAgents at `~/workspace/brain-sync/` (`com.masato.brainsync.*`)
keep running through M3. `cairn sync all` / `cairn review weekly` are stubs at
M0 (see `backend/app/cli.py`), so the legacy jobs remain the source of truth
for external-source sync until M1/M3 land. See DESIGN.md D11 and §9.
