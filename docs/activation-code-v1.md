# Ativação por código · contrato v1 de laboratório

Na candidata de automação, siga
[Operação automática do laboratório](operacao-automatica-laboratorio.md).
A nova prova de B é automática e o aplicativo aguarda sem trocar a chave.
Os comandos manuais por instalação deste documento descrevem o modo
anterior, preservado como referência; não fazem parte do novo fluxo técnico.

Central e Windows usam branches próprias. A central parte de
`078efb86581640bcedc40b2d2fd59699709a434f` e o Windows parte de
`7d62a800325146d1357f8832b1df5c4c3b2eff8d`. O instalador Windows 0.1.3
permanece referência para a alternativa por perfil.

## Contrato

O código para copiar e colar tem a forma
`https://central-do-laboratorio#macip-activation=<segredo-base64url>`.
O segredo contém 32 bytes aleatórios (43 caracteres). O fragmento da URL
não é enviado como parte de uma navegação HTTP. O aplicativo extrai o
segredo e o envia somente no corpo de um POST HTTPS. O operador deve
conferir o endereço da central e a CA do laboratório antes de entregar o código.

| Operação | Autorização e conteúdo |
| --- | --- |
| POST /api/v1/activation-codes | Admin + CSRF de sessão, `installation_id`. Exige prova vigente de B e modo de laboratório. Retorna `id`, `activation_code`, `expires_at` uma vez, com no-store. |
| POST /api/v1/activation-codes/{id}/revoke | Admin + CSRF de sessão. Revoga emissão/resgate/entrega. |
| POST /api/v1/activations/redeem | HTTPS, corpo com `activation_code` (somente os 43 caracteres), `activation_id`, `device_id`, `resume_secret`, `public_key`. Retorna versão 1, ID, estado e URL HTTPS de A. |
| POST /v1/activate em A | HTTPS, `activation_id` e `resume_secret`. HTTP 202/`awaiting_protection` ou HTTP 200/`ready`, versão 1, `profile` e `restic_password`. |
| POST /api/v1/activation-executor/authorize | Bearer de A, `activation_id` e SHA-256 da credencial de retomada. Responde somente configuração pública e autorização. |
| POST /api/v1/activation-executor/registered | Bearer de A, mesmo corpo. Registra a nova impressão digital e torna a prova antiga de B inválida. |
| POST /api/v1/activation-executor/delivered | Bearer de A, mesmo corpo. Reconfere B e o vínculo imediatamente antes da entrega, registra horário e estado. |

Os UUIDs de ativação e dispositivo e a credencial aleatória de retomada são
criados localmente. A chave Ed25519 também nasce no Windows. Somente seus
dois campos públicos SSH são enviados. O banco armazena hashes do código e
da credencial de retomada, identidade da instalação/chave/dispositivo,
solicitante, horários e estado. A senha Restic é lida do cofre original em A
e retorna diretamente ao Windows pelo endpoint de A. A central não recebe
esse segredo nem uma chave privada.

O código expira em dez minutos antes do primeiro uso. O primeiro resgate
consome o código atomicamente. Durante 24 horas após o resgate, a mesma
ativação pode retomar com o mesmo código, UUIDs, chave e credencial de
retomada. Perda da resposta ou reinício do aplicativo reutiliza os arquivos
locais da tentativa. Outra identidade é recusada. Há limite de 20 tentativas
por endereço/minuto, incluindo entradas inválidas, coordenado pelo PostgreSQL.

No Windows, a tentativa fica em `BaseDir/activation-code`, privada à conta.
A senha Restic e a credencial de retomada usam a proteção DPAPI existente.
A chave privada fica sob ACL da mesma conta para o OpenSSH. O fluxo só
preenche a configuração candidata depois de validar a host key SFTP e o ID
Restic. Salvar a agenda mantém o procedimento transacional e a confirmação
nativa da conta Windows. **Descartar tentativa** remove somente os arquivos
conhecidos da tentativa e permite outro código. Revogar código interrompe a
entrega, mas uma chave já autorizada permanece em A até revogação específica
do dispositivo pelo responsável.

## Vínculo com B

Registrar a chave nova altera a impressão digital incluída no vínculo
vigente. O primeiro clique registra a chave e mostra preparação pendente.
O responsável executa nova preparação/cópia/prova em B para a identidade
atual e então o técnico clica novamente com o mesmo código. Apenas
`restic copy` ou a criação da estrutura de B mantêm a entrega bloqueada.
Mudança posterior de repositório, host key ou vínculo exige nova prova ou
nova emissão conforme a identidade alterada.

## Rodar no laboratório descartável

1. Siga o roteiro `ensaio-windows-a-b.md` até a prova seed, usando a branch
   central de ativação. A fixture criada por `prepare-a` recebe um marcador
   de propriedade para o executor HTTPS de A. Use uma instalação fictícia
   nova e guarde sua área, sem transferir credenciais antigas para a VM.
2. Em um servidor descartável, emita um certificado de laboratório com SAN
   para o IPv4/DNS alcançado pela VM. Por exemplo, no Bash do roteiro:

```bash
openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 2 \
  -subj '/CN=MACIP Activation Lab' \
  -addext "subjectAltName=IP:$LAB_IP" \
  -keyout "$LAB/private/tls-key.pem" -out "$LAB/private/tls-cert.pem"
chmod 600 "$LAB/private/tls-key.pem"
export SSL_CERT_FILE="$LAB/private/tls-cert.pem"
"$PY" "$LAB_TOOL" configure-activation "$LAB" \
  --public-url "https://$LAB_IP:8187" \
  --delivery-url "https://$LAB_IP:9447/v1/activate"
```

3. Encerre com Ctrl+C os processos HTTP da API e do preview dessa área.
   Gere o build atual do portal. No terminal da central, usando as variáveis
   do roteiro e o certificado criado, execute:

```bash
"$PY" "$LAB_TOOL" activation-api "$LAB" \
  --certificate "$LAB/private/tls-cert.pem" --key "$LAB/private/tls-key.pem" \
  --ca "$LAB/private/tls-cert.pem"
```

4. Em outro terminal com as mesmas variáveis da área, inicie A:

```bash
"$PY" "$LAB_TOOL" activation-a "$LAB" \
  --certificate "$LAB/private/tls-cert.pem" --key "$LAB/private/tls-key.pem" \
  --ca "$LAB/private/tls-cert.pem"
```

5. Importe somente o certificado público/CA no repositório confiável da
   conta do técnico e do navegador da VM, conferindo o SHA-256 por canal
   independente. A chave privada TLS fica no servidor. Não desative a
   validação TLS. Abra `https://<IPv4-escolhido>:8187` e entre novamente.
   A central de laboratório serve o build do portal na mesma origem HTTPS.
6. No cadastro com prova seed vigente, clique **Gerar código de ativação**.
   A emissão exige `MACIP_API_LAB_ACTIVATION=1`; o helper configura essa
   opção e as URLs para os dois processos acima. Entregue o valor por canal
   privado. O painel permite ocultá-lo e revogá-lo. Não fotografe seu conteúdo.
7. Na branch Windows `feature/windows-backup-activation-code`, use os
   arquivos do aplicativo candidato incluindo `Activation-Code.ps1`.
   A instalação candidata deve incluir esse módulo, já listado no NSIS.
   Para gerar instalador, siga o BUILD.md da linha Windows com uma versão
   distinta, por exemplo `/DVERSAO=0.1.4-dev1`. Preserve o instalador 0.1.3
   e registre os hashes de cada artefato separadamente.
8. Na aba **Ativar**, cole o código e clique **Usar código de ativação**.
   A chave é criada localmente. O primeiro retorno informa a nova prova
   pendente. Na área do servidor, execute:

```bash
export SSL_CERT_FILE="$LAB/private/tls-cert.pem"
"$PY" "$LAB_TOOL" refresh-b "$LAB"
"$PY" "$LAB_TOOL" copy-activation "$LAB"
"$PY" "$LAB_TOOL" prove-activation "$LAB"
```

9. Clique novamente **Usar código de ativação**, com o mesmo valor. Após
   conferir SFTP/ID, o aplicativo avança para **Pasta e horário**. Continue
   o roteiro a partir da escolha da pasta e exclusão. Confirme a janela
   nativa da conta, o disparo sem sessão, os dois restores e a recuperação
   do ponto Windows por B com A/VM desligados.
10. Para a alternativa por perfil, use **Importar perfil**, chave e senha
    separadas conforme o roteiro anterior. Não misture o material de duas
    instalações. Encerre os dois servidores HTTPS com Ctrl+C e os
    contêineres pelo comando `stop` do helper após preservar as evidências.

Os segredos dos processos são carregados do arquivo privado do laboratório.
O executor A aceita somente a operação fixa de acrescentar a chave pública,
preservando as linhas existentes e a restrição da chave de B. No adaptador
Docker de laboratório, o nome do contêiner é uma configuração local de A,
e seu marcador deve corresponder à raiz de contas. A API não escolhe
comandos, contêineres nem caminhos administrativos.

## Extensão de monitoramento na candidata posterior

As branches de alertas adicionam `POST /api/v1/activations/monitoring`
após a entrega pronta. A candidata Windows pede a credencial limitada
com a mesma identidade/segredo de retomada, por HTTPS; não a inclui no
perfil JSON. Veja [Alertas centrais](alertas-centrais-laboratorio.md).
O consumidor de ativação anterior continua aceito. A candidata nova exige
`MACIP_API_LAB_ALERTS=1` e migração 12 na central. No helper de laboratório,
`activation-api` habilita esse modo explicitamente.

## Verificações e limites

O teste `tests/test_activation_real.py` executa central e A em HTTPS com
certificado temporário, PowerShell no Linux, OpenSSH/Restic reais e nova
prova de B por seccomp. Confere recusa da CA desconhecida, retomada, chave
de leitura de B preservada, segredo fora do banco e validação SFTP/ID antes
de retornar pronto. DPAPI nesse teste é explicitamente simulado.

Para reproduzir, configure o PostgreSQL descartável conforme os testes de B:

```bash
cd api
MACIP_TEST_WINDOWS_WORKTREE=/caminho/absoluto/do/worktree-windows \
MACIP_TEST_POWERSHELL=/tmp/pwsh/pwsh \
MACIP_TEST_PROVISIONING_SFTP_IMAGE=macip-protection-lab-sshd \
PYTHONPATH=src ../.venv-api/bin/python -m pytest -o tmp_path_retention_policy=none \
  tests/test_activation.py tests/test_activation_real.py tests/test_migrations.py -q
```

O teste usa `MACIP_API_TEST_DATABASE_URL`, que deve estar definido para o
banco descartável. O teste real é ignorado explicitamente se PowerShell,
worktree Windows ou fixture OpenSSH não estiverem configurados.
Na linha Windows, execute `run-activation-tests.ps1`,
`run-gui-tests.ps1` e `run-windows-agent-tests.ps1`.

A migração 11 é expansiva, mantém os dados existentes e exige coordenação
da atualização da API. Um código anterior recusa o esquema 11. Para retorno,
preserve o backup pré-migração e avalie escritas posteriores antes de uma
restauração. A integração candidata no Windows permanece pendente de
cliques, DPAPI real, agenda sem sessão e recuperação do ponto Windows
somente por B.
