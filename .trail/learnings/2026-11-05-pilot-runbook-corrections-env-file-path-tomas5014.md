---
title: "Pilot runbook corrections: env file path, no-activate, unit names"
author: Tomas5014
date: 2026-11-05
branch: fix/pilot-runbook-review
touches:
  - atualiza\303\247\303\265es.txt
  - tarefa-teste.json
---

## Decided
- Generated units load EnvironmentFile=-$MACIP_CONFIG/agent.env — the single credential file must live at that path, not $AREA/agent.env.
- pull-tasks --no-activate stages config-<gen>.json + units in tasks.d/<id>/staged/, writes NO current.json, does NOT confirm — controlled first backup runs `agent` on the candidate config, activation is a later normal pull-tasks.
- Generated unit names: macip-backup-managed-<task_id>, macip-backup-pull-<hash16>, macip-backup-sync-<hash16>, macip-backup-agent-<task_id>. Inventory by markers (install=, managed-task=, pull-id=, sync-id=), never glob-disable.
- SFTP remote_path: leading "/" = absolute remote path; no "/" = relative to SFTP home. internal-sftp is NOT a chroot.
- pull-unblock=distribution confirmations; outbox-rearm=backup outbox; NEITHER rearms recovery.db — no public recovery rearm exists.

## Watch out
- unit_directory() = Path.home()/.config/systemd/user — for disposable tests set HOME, not XDG_CONFIG_HOME.
- schedule dict shape: {"kind":"daily","hour":h,"minute":m,"timezone":"IANA"}; kind "manual"/None => no timer.
