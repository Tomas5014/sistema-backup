# Piloto SFTP — provisionamento do servidor real

Este guia prepara uma conta SFTP dedicada em um servidor Linux real para
executar `tests/sftp/run-pilot.sh`. Os passos espelham o fixture Docker
(`Dockerfile`/`sshd_config`): usuário sem shell, somente chave,
`internal-sftp`.

## 1. No servidor piloto (requer sudo)

Pré-requisitos em uma VM Ubuntu:

```bash
sudo apt install openssh-server     # geralmente já presente
sudo ufw allow 22                   # somente se ufw estiver ativo
```

A VM precisa ser alcançável na porta 22 pela máquina que rodará o piloto:
rede *bridged* entrega um IP da LAN direto; NAT exige port-forward do host.

```bash
# usuário dedicado, sem shell de login interativa
sudo useradd --create-home --shell /usr/sbin/nologin macipsftptest
sudo passwd --delete macipsftptest

# diretório de repositórios de teste
sudo install -d -m 755 -o macipsftptest -g macipsftptest \
    /home/macipsftptest/repositories

# autorizar a chave pública gerada no cliente (conteúdo de
# ~/.ssh/macip-piloto_ed25519.pub, ver etapa 2)
sudo install -d -m 700 -o macipsftptest -g macipsftptest \
    /home/macipsftptest/.ssh
echo 'ssh-ed25519 AAAA... macip-backup-piloto' | \
    sudo tee /home/macipsftptest/.ssh/authorized_keys >/dev/null
sudo chown macipsftptest:macipsftptest /home/macipsftptest/.ssh/authorized_keys
sudo chmod 600 /home/macipsftptest/.ssh/authorized_keys

# restringir a conta a SFTP interno; acrescentar AO FINAL de sshd_config
sudo tee -a /etc/ssh/sshd_config <<'EOF'

Match User macipsftptest
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PubkeyAuthentication yes
    ForceCommand internal-sftp
    AllowTcpForwarding no
    X11Forwarding no
EOF
sudo sshd -t && sudo systemctl reload sshd   # Debian/RHEL: sshd; Ubuntu: ssh
```

O bloco `Match` precisa ficar depois das diretivas globais e antes de
qualquer outro bloco `Match` existente não pode haver conflito — o `tee -a`
anexa ao fim do arquivo, que é o lugar correto.

## 2. No cliente (esta máquina)

A chave já foi gerada em `~/.ssh/macip-piloto_ed25519`. Se precisar
regerar:

```bash
ssh-keygen -t ed25519 -N '' -C "macip-backup-piloto" -f ~/.ssh/macip-piloto_ed25519
cat ~/.ssh/macip-piloto_ed25519.pub   # instalar no servidor (etapa 1)
```

## 3. Fingerprint da host key

No servidor:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Confira esse fingerprint por canal seguro. Ele será comparado ao que
`ssh-keyscan` observar pela rede — divergência indica MITM ou host errado.

## 4. Executar o piloto

```bash
export MACIP_SFTP_HOST=<host-do-servidor>
export MACIP_SFTP_PORT=22
export MACIP_SFTP_USER=macipsftptest
export MACIP_SFTP_IDENTITY_FILE=~/.ssh/macip-piloto_ed25519
export MACIP_SFTP_REMOTE_PATH=repositories/piloto
export MACIP_SFTP_HOST_KEY_SHA256=SHA256:...   # fingerprint da etapa 3

tests/sftp/run-pilot.sh
```

Sem `MACIP_SFTP_HOST_KEY_SHA256` o script exibe os fingerprints observados
e aborta; `MACIP_SFTP_TRUST_ON_FIRST_USE=1` aceita a chave vista (TOFU).

Volume do teste de piloto: `MACIP_SFTP_PILOT_FILES` (padrão 300) e
`MACIP_SFTP_PILOT_MEBIBYTES` (padrão 64).

## 5. Limpeza

Cada teste cria `MACIP_SFTP_REMOTE_PATH/macip-test-*` e o script os lista
ao final. `MACIP_SFTP_CLEANUP_SSH=1` tenta removê-los via shell SSH — com
`ForceCommand internal-sftp` a conta não tem shell, então a remoção é
manual no servidor (`rm -rf ~/repositories/piloto/macip-test-*`).
