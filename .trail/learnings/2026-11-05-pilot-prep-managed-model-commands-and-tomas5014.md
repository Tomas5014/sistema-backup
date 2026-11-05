---
title: "Pilot prep: managed model commands and SFTP config shape"
author: Tomas5014
date: 2026-11-05
branch: docs/pilot-mvp-linux-sftp
touches:
  - atualiza\303\247\303\265es.txt
  - tarefa-teste.json
---

## Decided
- Managed model: portal publishes task revision -> pull-tasks applies (writes tasks.d/<id>/current.json + units, enables timer per central schedule). Do NOT run schedule/enable manually on managed tasks.
- SFTP destination JSON: kind "sftp", sftp{host,port,user,remote_path,identity_file,known_hosts_file,...}; password_environment names the env var; restic repo ref becomes sftp:user@host:/remote_path. No "repository" field for sftp.
- Recovery work needs --policy AND --queue AND --central; --limit defaults 1.
- central.json: url, agent_id, token_environment (names var, never value); "token" field rejected; HTTPS required off-localhost.

## Watch out
- completed:1 of worker != succeeded of recovery operation; success in agent history is what counts.
- timers --user die at logout without loginctl enable-linger — agenda pilots need it declared.
- systemd-cgls --user-unit exists; systemd-analyze --user verify validates OnCalendar.
