#!/usr/bin/env bash
# Caminho LABORATÓRIO: CA própria + certificado de servidor com SAN
# do hostname informado. Nada é instalado na confiança do sistema —
# o CA fica em tls/lab-ca.pem para confiança explícita por cliente.
#
# Uso: scripts/mk-lab-tls.sh <hostname> [dias]
set -euo pipefail
source "$(dirname "$0")/_lib.sh"

HOSTNAME="${1:?uso: mk-lab-tls.sh <hostname> [dias]}"
DAYS="${2:-825}"
TLS_DIR="$CENTRAL_DIR/tls"

umask 077
mkdir -p "$TLS_DIR"

# CA de laboratório (chave no projeto, não no sistema)
openssl req -x509 -newkey rsa:3072 -nodes \
  -keyout "$TLS_DIR/lab-ca.key" -out "$TLS_DIR/lab-ca.pem" \
  -days "$DAYS" -subj "/CN=MAC IP Lab CA/O=MAC IP Homologation" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"

# certificado do servidor assinado pela CA
openssl req -newkey rsa:2048 -nodes \
  -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/server.csr" \
  -subj "/CN=$HOSTNAME/O=MAC IP Homologation"

CATMP="$(mktemp -d)"
trap 'rm -rf "$CATMP"' EXIT
cat > "$CATMP/ext.cnf" <<EOT
[v3]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:$HOSTNAME
EOT
openssl x509 -req -in "$TLS_DIR/server.csr" \
  -CA "$TLS_DIR/lab-ca.pem" -CAkey "$TLS_DIR/lab-ca.key" -CAcreateserial \
  -out "$TLS_DIR/cert.pem" -days "$DAYS" -extensions v3 -extfile "$CATMP/ext.cnf"
rm -f "$TLS_DIR/server.csr"
chmod 600 "$TLS_DIR/key.pem" "$TLS_DIR/lab-ca.key"
echo "tls/: cert.pem + key.pem (SAN=$HOSTNAME) — CA em tls/lab-ca.pem"
