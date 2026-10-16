"""
Backup incremental.

Uso:
    python3 backup-incremental.py \
        ~/testar-backup/arquivos-teste/ \
        ~/testar-backup/backups/
"""

import time
from pathlib import Path
import argparse
import tarfile

import manifest
from utils import inicio, termino, gera_log, validar_diretorios


def receber_argumentos_incremental():
    parser = argparse.ArgumentParser(
        description="Sistema de Backup Incremental"
    )

    parser.add_argument(
        "origem",
        help="Diretório que será copiado."
    )

    parser.add_argument(
        "destino",
        help="Diretório onde o backup será armazenado."
    )

    return parser.parse_args()


def encontrar_manifestos_incrementais(origem, destino, base_full):
    origem = Path(origem).expanduser().resolve()
    destino = Path(destino).expanduser().resolve()

    candidatos = []

    for caminho in destino.glob("*-manifesto-incremental.json"):
        manifesto_incremental = manifest.carregar_manifest(caminho)

        if (
            manifesto_incremental.get("tipo") == "incremental"
            and manifesto_incremental.get("base_full") == base_full
            and Path(manifesto_incremental["origem"]).resolve() == origem
        ):
            candidatos.append(caminho)

    return sorted(candidatos, key=lambda caminho: caminho.name)


def aplicar_incremental_arquivos(estado, manifesto_incremental):
    estado = estado.copy()

    for arquivo in manifesto_incremental["novos"]:
        estado[arquivo["nome"]] = arquivo

    for arquivo in manifesto_incremental["alterados"]:
        estado[arquivo["nome"]] = arquivo

    for arquivo in manifesto_incremental["removidos"]:
        estado.pop(arquivo["nome"], None)

    return estado


def aplicar_incremental_diretorios(estado, manifesto_incremental):
    estado = estado.copy()

    for diretorio in manifesto_incremental["diretorios_novos"]:
        estado.add(diretorio)

    for diretorio in manifesto_incremental["diretorios_removidos"]:
        estado.discard(diretorio)

    return estado


def construir_estado_incremental(manifesto_full, caminhos_incrementais):
    estado_arquivos = manifest.indexar_arquivos(
        manifesto_full["arquivos"]
    )

    estado_diretorios = set(
        manifesto_full.get("diretorios", [])
    )

    for caminho_manifesto in caminhos_incrementais:
        manifesto_incremental = manifest.carregar_manifest(
            caminho_manifesto
        )

        estado_arquivos = aplicar_incremental_arquivos(
            estado_arquivos,
            manifesto_incremental
        )

        estado_diretorios = aplicar_incremental_diretorios(
            estado_diretorios,
            manifesto_incremental
        )

    return estado_arquivos, estado_diretorios


def criar_backup_incremental(origem, diferencas, destino):
    origem = Path(origem).expanduser().resolve()
    destino = Path(destino).expanduser().resolve()

    arquivos_backup = (
        diferencas["novos"]
        + diferencas["alterados"]
    )

    diretorios_backup = diferencas["diretorios_novos"]

    with tarfile.open(destino, "w:gz") as arquivo_tar:
        for arquivo in arquivos_backup:
            caminho_completo = origem / arquivo["nome"]

            arquivo_tar.add(
                caminho_completo,
                arcname=arquivo["nome"]
            )

        for diretorio in diretorios_backup:
            caminho_completo = origem / diretorio

            arquivo_tar.add(
                caminho_completo,
                arcname=diretorio,
                recursive=False
            )

    return destino


def gerar_manifest_incremental(
    origem,
    diferencas,
    base_full,
    backup_anterior,
    data,
    hora,
    nome_arquivo_backup
):
    origem = Path(origem).expanduser().resolve()

    return {
        "tipo": "incremental",
        "base_full": Path(base_full).name,
        "backup_anterior": backup_anterior,
        "arquivo_backup_incremental": nome_arquivo_backup,
        "origem": str(origem),
        "data": f"{data} {hora.replace('-', ':')}",
        "novos": diferencas["novos"],
        "alterados": diferencas["alterados"],
        "removidos": diferencas["removidos"],
        "diretorios_novos": diferencas["diretorios_novos"],
        "diretorios_removidos": diferencas["diretorios_removidos"]
    }


def backup_incremental(origem, destino):
    hora_inicio = time.strftime("%H-%M-%S")
    dia_inicio = time.strftime("%Y-%m-%d")

    origem = Path(origem).expanduser().resolve()
    destino = Path(destino).expanduser().resolve()
    destino_log = destino / "logs"

    validacao_diretorios, msg_erro = validar_diretorios(
        origem,
        destino,
        destino_log
    )

    if not validacao_diretorios:
        print(f"Erro: {msg_erro}")
        return False

    destino.mkdir(parents=True, exist_ok=True)
    destino_log.mkdir(parents=True, exist_ok=True)

    path_log = gera_log(destino_log, "incremental")

    start = inicio(hora_inicio)
    with open(path_log, "w", encoding="utf-8") as log:
        log.write(start)

    arquivos_atuais = manifest.listar_arquivos(origem)
    diretorios_atuais = manifest.listar_diretorios(origem)

    caminho_manifest_full = (
        manifest.encontrar_manifest_full_mais_recente(
            origem,
            destino
        )
    )

    if caminho_manifest_full is None:
        mensagem = (
            "Erro: nenhum backup full encontrado para essa origem."
        )

        print(mensagem)

        with open(path_log, "a", encoding="utf-8") as log:
            log.write(f"\n{mensagem}\n")

        return False

    manifesto_full = manifest.carregar_manifest(
        caminho_manifest_full
    )

    base_full = caminho_manifest_full.name

    caminhos_incrementais = encontrar_manifestos_incrementais(
        origem,
        destino,
        base_full
    )

    estado_arquivos, estado_diretorios = construir_estado_incremental(
        manifesto_full,
        caminhos_incrementais
    )

    estado_anterior = {
        "arquivos": list(estado_arquivos.values()),
        "diretorios": list(estado_diretorios)
    }

    diferencas = manifest.comparar_manifest(
        estado_anterior,
        arquivos_atuais,
        diretorios_atuais
    )

    nome_arquivo_backup = (
        f"{dia_inicio}_{hora_inicio}"
        "-backup-incremental.tar.gz"
    )

    path_backup = destino / nome_arquivo_backup

    criar_backup_incremental(
        origem,
        diferencas,
        path_backup
    )

    backup_anterior = (
        caminhos_incrementais[-1].name
        if caminhos_incrementais
        else None
    )

    manifesto_incremental = gerar_manifest_incremental(
        origem,
        diferencas,
        caminho_manifest_full,
        backup_anterior,
        dia_inicio,
        hora_inicio,
        nome_arquivo_backup
    )

    destino_manifesto = destino / (
        f"{dia_inicio}_{hora_inicio}"
        "-manifesto-incremental.json"
    )

    manifest.salvar_manifest(
        manifesto_incremental,
        destino_manifesto
    )

    final = termino(
        dia_inicio,
        hora_inicio,
        path_backup,
        path_log
    )

    with open(path_log, "a", encoding="utf-8") as log:
        log.write(final)

    return True


if __name__ == "__main__":
    args = receber_argumentos_incremental()

    backup_incremental(
        args.origem,
        args.destino
    )
