# Homologação operacional relatada — recuperação Linux

Evidências fornecidas pelo operador em 04/11/2026, fuso
America/Sao_Paulo. Não foram reexecutadas pelo agente que registra este
documento. Código: `ea74d5d5bf8792b374addbc6addf98345c3327a7`.
Distribuição experimental 0.4.0dev1; agente 0.2.1; PG migrações 1–7;
SQLite principal v7; fila independente v3. Não constitui aceite de produção
ou conclusão do MVP, nem homologação Windows/SMB/SFTP/S3.

## Ambiente e isolamento informados

- VM `srvdev1` (10.10.20.239), usuário `estagio01`, área
  `~/macip-homolog-recuperacao-ea74d5d`, Compose
  `macip-recuperacao-ea74d5d`, HTTPS `127.0.0.1:8445`. API/portal com imagens
  identificadas pelo SHA completo; PostgreSQL/volume exclusivos. Migrações
  conferidas, API saudável, portal HTTP 200. Central 8443 e piloto preservados.
- PC `estagiario01-Aspire-A315-53`, usuário `estagiario01`, Python 3.14.4,
  Restic 0.18.1; área `/home/estagiario01/macip-homolog-recuperacao-ea74d5d`.
  Subdiretórios: programa, bin, config, estado, cache, locks, origem,
  repositorio, recuperacoes. Fila `estado/recovery.db`, política
  `config/recovery.json`. Instalação pelo wheel congelado, commit/lock
  conferidos em install.json. Python instalado + macip_backup.manual, sem
  PYTHONPATH substituindo o código instalado.
- URL no PC `https://localhost:18445`, túnel SSH para loopback 8445 da VM.
  CA pública `/home/estagiario01/macip-recuperacao-acesso-ea74d5d/lab-ca.pem`.
  Credenciais locais: **nomes apenas** MACIP_RECOVERY_API_TOKEN,
  MACIP_RECOVERY_RESTIC_PASSWORD e SSL_CERT_FILE. Nenhum valor registrado.
- Cliente `0d5fc6ff-4457-4662-a163-afe105cc7b05`; agente
  `dd7d5bae-4717-408c-9e57-b9f4db63aed2`; tarefa
  `df9f5cee-66ea-438d-9656-224b41be4a95`; bond abreviado `b73f475a0294f140`;
  destino `local-recuperacao`; revisão 1, geração aplicada 1, agenda manual,
  exclusão `*.tmp`.

## Ciclo informado

1. Backup inicial `26d91f05-db60-4311-a0d6-4ebb9d06d683`: success, 2 arquivos,
   resultado enviado e check --read-data aprovado. Snapshot
   `306760b7ca869c6997d3dfbd5f9eb629ed06843bd47008834fe8aa57941040ef`.
2. Consulta/solicitação pelo portal; recovery-poll/work/poll manuais.
   Restore `67017743-da96-410c-b87d-3ee6736bb6df` confirmado no portal;
   diff aprovado, ignorar.tmp ausente.
3. Systemd --user real: `macip-recuperacao-ea74d5d-poll.service/.timer` e
   `macip-recuperacao-ea74d5d-work.service/.timer`. Poll a cada 30 s; worker
   a cada 30 s, deslocado 15 s; worker --limit 1. Type=oneshot, UMask=0077,
   KillMode=control-group, TimeoutStartSec=infinity, TimeoutStopSec=45s,
   wrapper privado `<área>/executar.sh`. Consulta/restore automáticos:
   `d63e16f4-db9f-48ba-a5c9-a48fedee2b02`, portal/diff/exclusão aprovados.
4. Só o túnel 18445 foi interrompido: poll retry:1. Reconexão permitiu
   consulta, execução e envio automáticos, sem bloqueios definitivos.
5. Resultado durável: timers pausados, restore local concluído, sem poll
   posterior; túnel interrompido antes do envio. Offline: received:0, sent:0,
   blocked:0, retry:2. Reconectado: received:0, sent:1, blocked:0, retry:0.
   Nenhuma chamada adicional ao worker. Operação
   `923ce6be-2ef8-47fa-abc2-d3ef52848695`: portal/diff/exclusão aprovados.
   **retry:2 não significa duas restaurações**: poll pode contar falhas de
   envio e de descoberta na mesma rodada.
6. Acrescentado origem/volume-interrupcao.bin, sintético, 536870912 bytes
   (512 MiB). Backup `53bcab8a-8375-4fe5-bd72-73093b436a32`: success,
   3 arquivos, 537919538 bytes processados, resultado enviado. Snapshot
   `350165be0175f8549d4517259075cb937a397b4ff8d4ba42db55b361eb7c21f0`.
7. Restore `6b9390a5-585b-4957-ae50-b4bc8ecea159`: restic restore detectado
   no cgroup do worker; parada por systemctl --user stop aproximadamente
   16:39:31. Cgroup sem processos após parada, pasta preservada. SQLite
   state=interrupted, sent=0; poll posterior sent=1, blocked=0, retry=0.
   Portal: “Interrompida — não concluída”, com aviso de pasta parcial.
   Parada controlada, **não** SIGKILL, queda de energia ou reboot. Detectar
   o processo não mede bytes restaurados. Result=success do serviço e
   completed:1 contam término da rodada, não sucesso da restauração.
8. Nova consulta/restore `024ab3ff-e683-464c-90b5-d0001ddbb500`, em outra
   pasta, snapshot 350165be…: portal confirmou, diff dos arquivos não
   excluídos aprovado, ignorar.tmp ausente. SHA-256 do arquivo de 512 MiB
   original/restaurado idêntico:
   `42cedb897367b0af37997133c8805061649d96e0970fd2294a63ff80c2f8fd90`.

## Estado e pendências

Timers de recuperação foram **parados**, anteriormente habilitados; não há
evidência de disable. Podem voltar após sessão/reboot: inactive não implica
disabled. Não confundir com os timers antigos de consulta/sync de outro
laboratório, cuja desativação já foi confirmada anteriormente.

Nenhuma fila, repositório, pasta parcial ou recuperação foi apagada. 512 MiB
sintéticos não representam dados/volumes dos clientes. Reboot ainda não foi
executado; queda abrupta/abandono de running exige ensaio separado.
Próximo passo: [roteiro de persistência](prova-reinicio-recuperacao.md).

Testes automatizados anteriores permanecem em recuperação-portal-linux.md e
registro-trabalho.md. Nesta sessão somente código/documentação do repositório
foram inspecionados e snippets foram verificados em dados sintéticos; não
houve acesso operacional, deploy ou regeneração do pacote congelado.
