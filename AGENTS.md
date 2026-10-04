# Agent instructions — GoLab Pi / LabDash

## Shared guidance

Before planning, proposing, reviewing, delegating or implementing changes,
read the shared engineering guidance in
`bardphysicslab/engineering-standards`. Your tool does not load it
automatically.

1. Resolve `main` once per task.
   - With a local clone, usually `../engineering-standards` beside this
     repository: run `git -C ../engineering-standards fetch origin main`,
     then `git -C ../engineering-standards rev-parse origin/main`. If the
     fetch fails, use the existing `origin/main` and say it may be stale.
   - Without a clone, run
     `gh api repos/bardphysicslab/engineering-standards/commits/main --jq .sha`.
2. Read these files at that SHA:
   - `README.md`
   - `development-workflow.md`
   - `agent-practice.md`
   - `checkouts-and-worktrees.md`
   - `agent-coordination.md`

   With the clone, use `git -C ../engineering-standards show <sha>:<file>`.
   Without it, use
   `gh api "repos/bardphysicslab/engineering-standards/contents/<file>?ref=<sha>" -H "Accept: application/vnd.github.raw"`.
3. Record `Shared guidance: bardphysicslab/engineering-standards@<sha>` once
   in the task's durable evidence. If a review or proposal produces no such
   artifact, state it once in your response. A task that already recorded
   `bardphysicslab/bardbox@<sha>` keeps that governing commit.

Copies (desktop files, chat project sources, memory) do not substitute for
the resolved SHA. If a required file cannot be read at the resolved SHA, do
read-only investigation only, and report it. Do not implement, commit, push,
deploy or end checkouts unless the maintainer explicitly says to proceed
without it.

This is a BardBox project: also read bardbox's root `AGENTS.md` and
`ARCHITECTURE.md` at one resolved `main` commit of `bardphysicslab/bardbox`,
and the detailed standards relevant to the task.

## Project context
This project runs on a Raspberry Pi used for a Bard GoLab remote monitoring system.

Current deployment status:
- Raspberry Pi hostname: `golab-pi`
- Static Ethernet IP: `10.60.10.59`
- Access is through Bard internal network or Bard VPN
- SSH shortcut from Mac: `ssh golab`
- FastAPI app is deployed as a systemd service
- Service name: `labdash`
- App is currently served by uvicorn on `0.0.0.0:8000`

## Important paths
- Project root: `~/golab-monitor`
- App source: `~/golab-monitor/raspi`
- Virtual environment: `~/golab-monitor/venv`
- App entry point: `main:app` (in `raspi/main.py`)
- Systemd service file: `/etc/systemd/system/labdash.service`

## Current service behavior
The app is already running successfully as a background service.
Use these commands for inspection:

```bash
systemctl status labdash --no-pager
journalctl -u labdash -n 100 --no-pager
journalctl -u labdash -f
```

## Repo structure
- `raspi/` — FastAPI app running on the Pi
- `arduino/` — Arduino sensor node firmware and protocol docs
- `venv/` — Python virtual environment (not committed)
- `AGENTS.md` — these agent instructions (`CLAUDE.md` loads it)

## BardBox governance

GoLab is a working BardBox project repo. The canonical standards live in
`bardbox`; the reusable reference implementation lives in
`bardbox-project-template`.

BardBox standards are living standards. When a platform-level improvement is
made here, update:

1. This GoLab repo.
2. `bardbox-project-template`.

If the change alters the BardBox platform/framework standard, update `bardbox`
as well.

Follow BardBox dashboard and stale-data rules unless the user explicitly says
they are changing the standard: never show stale readings as live, use clear
offline/stale states, preserve current entrypoint conventions for this project,
and keep reusable layout fixes synchronized back to the template.
