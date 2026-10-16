"""
Restauração de backup incremental.

Uso:
    python3 restore-incremental.py \
        ~/testar-backup/arquivos-teste/ \
        ~/testar-backup/backups/

"""

import time
from pathlib import Path
import argparse

import manifest
from utils import extrair_backup, calcular_hash


# Recebe os argumentos via linha de comando
def receber_argumentos_restore_incremental():
    parser = argparse.ArgumentParser(
        description="Sistema de restauração de backup incremental"
    )

    parser.add_argument(
        "origem",
        help="Diretório referente ao backup."
    )

    parser.add_argument(
        "path_backup",
        help="Diretório raiz onde os backups estão armazenados."
    )

    return parser.parse_args()


# Valida o diretório onde os backups estão armazenados.
def validar_diretorio(path_backups):
    if not path_backups.exists():
        return False, f"destino de backup não existe: {path_backups}"

    if not path_backups.is_dir():
        return False, f"destino de backup não é um diretório: {path_backups}"

    return True, None


# Localiza todos os manifestos incrementais ligados ao Full informado.
def encontrar_manifestos_incrementais(origem, destino, base_full):
    origem = Path(origem).expanduser().resolve()
    destino = Path(destino).expanduser().resolve()

    candidatos = []

    for caminho in destino.glob("*-manifesto-incremental.json"):
        try:
            manifesto_incremental = manifest.carregar_manifest(caminho)
        except (OSError, ValueError):
            continue

        if (
            manifesto_incremental.get("tipo") == "incremental"
            and manifesto_incremental.get("base_full") == base_full
            and Path(manifesto_incremental["origem"]).resolve() == origem
        ):
            candidatos.append(caminho)

    return sorted(
        candidatos,
        key=lambda caminho: caminho.name
    )


# Verifica se os incrementais formam uma cadeia coerente.
#
# INC 1 -> backup_anterior = None
# INC 2 -> backup_anterior = manifesto do INC 1
# INC 3 -> backup_anterior = manifesto do INC 2
def validar_cadeia_incremental(caminhos_incrementais):
    anterior = None

    for caminho in caminhos_incrementais:
        manifesto_incremental = manifest.carregar_manifest(caminho)

        referencia_anterior = manifesto_incremental.get(
            "backup_anterior"
        )

        if referencia_anterior != anterior:
            return False, (
                "Cadeia incremental inválida em "
                f"{caminho.name}. "
                f"Esperado backup_anterior={anterior}, "
                f"encontrado={referencia_anterior}."
            )

        anterior = caminho.name

    return True, None


# Remove os arquivos indicados pelo manifesto incremental.
def remover_arquivos_restauracao(
    destino_restauracao,
    manifesto_incremental
):
    destino_restauracao = (
        Path(destino_restauracao)
        .expanduser()
        .resolve()
    )

    for removido in manifesto_incremental.get("removidos", []):
        caminho_remover = (
            destino_restauracao / removido["nome"]
        ).resolve()

        if not caminho_remover.is_relative_to(
            destino_restauracao
        ):
            erro = (
                "Erro: caminho inválido no manifesto: "
                f"{removido['nome']}"
            )
            return False, erro

        if caminho_remover.exists():
            if not caminho_remover.is_file():
                return False, (
                    "Erro: era esperado um arquivo para remoção, "
                    f"mas o caminho não é arquivo: {removido['nome']}"
                )

            try:
                caminho_remover.unlink()
            except OSError as erro:
                return False, (
                    f"Não foi possível remover o arquivo "
                    f"{removido['nome']}: {erro}"
                )

    return True, None


# Remove os diretórios indicados pelo manifesto incremental.
# A ordenação por profundidade garante a remoção de dentro para fora.
def remover_diretorios_restauracao(
    destino_restauracao,
    manifesto_incremental
):
    destino_restauracao = (
        Path(destino_restauracao)
        .expanduser()
        .resolve()
    )

    diretorios_removidos = manifesto_incremental.get(
        "diretorios_removidos",
        []
    )

    diretorios_removidos = sorted(
        diretorios_removidos,
        key=lambda diretorio: len(Path(diretorio).parts),
        reverse=True
    )

    for diretorio in diretorios_removidos:
        caminho = (
            destino_restauracao / diretorio
        ).resolve()

        if not caminho.is_relative_to(
            destino_restauracao
        ):
            return False, (
                "Erro: caminho inválido no manifesto: "
                f"{diretorio}"
            )

        if caminho.exists():
            if not caminho.is_dir():
                return False, (
                    "Erro: era esperado um diretório para remoção, "
                    f"mas o caminho não é diretório: {diretorio}"
                )

            try:
                caminho.rmdir()
            except OSError as erro:
                return False, (
                    f"Não foi possível remover o diretório "
                    f"{diretorio}: {erro}"
                )

    return True, None


# Aplica um manifesto incremental sobre o estado esperado dos arquivos.
def aplicar_incremental_arquivos(
    estado_arquivos,
    manifesto_incremental
):
    estado_arquivos = estado_arquivos.copy()

    for arquivo in manifesto_incremental.get("novos", []):
        estado_arquivos[arquivo["nome"]] = arquivo

    for arquivo in manifesto_incremental.get("alterados", []):
        estado_arquivos[arquivo["nome"]] = arquivo

    for arquivo in manifesto_incremental.get("removidos", []):
        estado_arquivos.pop(
            arquivo["nome"],
            None
        )

    return estado_arquivos


# Aplica um manifesto incremental sobre o estado esperado dos diretórios.
def aplicar_incremental_diretorios(
    estado_diretorios,
    manifesto_incremental
):
    estado_diretorios = estado_diretorios.copy()

    for diretorio in manifesto_incremental.get(
        "diretorios_novos",
        []
    ):
        estado_diretorios.add(diretorio)

    for diretorio in manifesto_incremental.get(
        "diretorios_removidos",
        []
    ):
        estado_diretorios.discard(diretorio)

    return estado_diretorios


# Reconstrói o estado final esperado:
# Full + todos os incrementais da cadeia.
def construir_estado_esperado(
    manifesto_full,
    caminhos_incrementais
):
    arquivos_esperados = manifest.indexar_arquivos(
        manifesto_full["arquivos"]
    )

    diretorios_esperados = set(
        manifesto_full.get("diretorios", [])
    )

    for caminho_manifesto in caminhos_incrementais:
        manifesto_incremental = manifest.carregar_manifest(
            caminho_manifesto
        )

        arquivos_esperados = aplicar_incremental_arquivos(
            arquivos_esperados,
            manifesto_incremental
        )

        diretorios_esperados = aplicar_incremental_diretorios(
            diretorios_esperados,
            manifesto_incremental
        )

    return arquivos_esperados, diretorios_esperados


# Valida arquivos e diretórios depois da restauração.
def validar_restauracao(
    destino_restauracao,
    arquivos_esperados,
    diretorios_esperados
):
    destino_restauracao = (
        Path(destino_restauracao)
        .expanduser()
        .resolve()
    )

    sucesso = True

    # Valida arquivos esperados e seus hashes.
    for nome, arquivo in arquivos_esperados.items():
        caminho = (
            destino_restauracao / nome
        ).resolve()

        if not caminho.is_relative_to(
            destino_restauracao
        ):
            print(
                f"[ERRO] Caminho inválido no estado esperado: {nome}"
            )
            sucesso = False
            continue

        if not caminho.exists() or not caminho.is_file():
            print(
                f"[ERRO] Arquivo ausente ou inválido: {nome}"
            )
            sucesso = False
            continue

        try:
            hash_restaurado = calcular_hash(caminho)
        except OSError as erro:
            print(
                f"[ERRO] Não foi possível calcular hash "
                f"de {nome}: {erro}"
            )
            sucesso = False
            continue

        if hash_restaurado == arquivo["hash"]:
            print(f"[OK] Arquivo: {nome}")
        else:
            print(f"[ERRO] Hash diferente: {nome}")
            sucesso = False

    # Descobre os arquivos realmente restaurados.
    arquivos_encontrados = set()

    for caminho in destino_restauracao.rglob("*"):
        if caminho.is_file():
            relativo = caminho.relative_to(
                destino_restauracao
            )

            arquivos_encontrados.add(
                str(relativo)
            )

    nomes_arquivos_esperados = set(
        arquivos_esperados.keys()
    )

    arquivos_extras = (
        arquivos_encontrados
        - nomes_arquivos_esperados
    )

    for nome in arquivos_extras:
        print(
            f"[ERRO] Arquivo inesperado: {nome}"
        )
        sucesso = False

    # Descobre os diretórios realmente restaurados.
    diretorios_encontrados = set()

    for caminho in destino_restauracao.rglob("*"):
        if caminho.is_dir():
            relativo = caminho.relative_to(
                destino_restauracao
            )

            diretorios_encontrados.add(
                str(relativo)
            )

    diretorios_ausentes = (
        diretorios_esperados
        - diretorios_encontrados
    )

    diretorios_extras = (
        diretorios_encontrados
        - diretorios_esperados
    )

    for diretorio in diretorios_ausentes:
        print(
            f"[ERRO] Diretório ausente: {diretorio}"
        )
        sucesso = False

    for diretorio in diretorios_extras:
        print(
            f"[ERRO] Diretório inesperado: {diretorio}"
        )
        sucesso = False

    return sucesso


# Executa a restauração incremental.
def restore_incremental(origem, path_backups):
    data_inicio = time.strftime("%Y-%m-%d")
    hora_inicio = time.strftime("%H-%M-%S")

    origem = Path(origem).expanduser().resolve()
    path_backups = (
        Path(path_backups)
        .expanduser()
        .resolve()
    )

    validacao_diretorios, msg_erro = validar_diretorio(
        path_backups
    )

    if not validacao_diretorios:
        print(f"Erro: {msg_erro}")
        return False

    # Localiza o Full mais recente da origem.
    caminho_manifest_full = (
        manifest.encontrar_manifest_full_mais_recente(
            origem,
            path_backups
        )
    )

    if caminho_manifest_full is None:
        print(
            "Erro: nenhum backup full encontrado "
            "para essa origem."
        )
        return False

    try:
        manifesto_full = manifest.carregar_manifest(
            caminho_manifest_full
        )
    except (OSError, ValueError) as erro:
        print(
            f"Erro ao carregar manifesto Full: {erro}"
        )
        return False

    base_full = caminho_manifest_full.name

    # Localiza todos os incrementais pertencentes ao Full.
    caminhos_incrementais = encontrar_manifestos_incrementais(
        origem,
        path_backups,
        base_full
    )

    # Garante que a cadeia não esteja quebrada.
    cadeia_valida, erro = validar_cadeia_incremental(
        caminhos_incrementais
    )

    if not cadeia_valida:
        print(f"Erro: {erro}")
        return False

    # Cria a pasta de restauração.
    destino_restauracao = path_backups / (
        f"restauracao-incremental-"
        f"{data_inicio}-{hora_inicio}"
    )

    destino_restauracao.mkdir(
        parents=True,
        exist_ok=True
    )

    # 1. Extrai o Full.
    caminho_backup_full = (
        path_backups
        / manifesto_full["arquivo_backup"]
    )

    sucesso, erro = extrair_backup(
        caminho_backup_full,
        destino_restauracao
    )

    if not sucesso:
        print(
            f"Erro ao extrair backup Full: {erro}"
        )
        return False

    # 2. Aplica cada incremental em ordem.
    for caminho_manifesto in caminhos_incrementais:
        try:
            manifesto_incremental = (
                manifest.carregar_manifest(
                    caminho_manifesto
                )
            )
        except (OSError, ValueError) as erro:
            print(
                f"Erro ao carregar manifesto incremental "
                f"{caminho_manifesto.name}: {erro}"
            )
            return False

        caminho_backup_incremental = (
            path_backups
            / manifesto_incremental[
                "arquivo_backup_incremental"
            ]
        )

        print(
            "Aplicando incremental: "
            f"{caminho_manifesto.name}"
        )

        # Primeiro extrai arquivos/diretórios novos e alterados.
        sucesso, erro = extrair_backup(
            caminho_backup_incremental,
            destino_restauracao
        )

        if not sucesso:
            print(
                "Erro ao extrair backup incremental "
                f"{caminho_manifesto.name}: {erro}"
            )
            return False

        # Depois processa arquivos removidos naquele ponto.
        sucesso, erro = remover_arquivos_restauracao(
            destino_restauracao,
            manifesto_incremental
        )

        if not sucesso:
            print(
                f"{erro}\n"
                "Erro ao processar arquivos removidos "
                f"em {caminho_manifesto.name}."
            )
            return False

        # Por fim processa diretórios removidos.
        sucesso, erro = remover_diretorios_restauracao(
            destino_restauracao,
            manifesto_incremental
        )

        if not sucesso:
            print(
                f"{erro}\n"
                "Erro ao processar diretórios removidos "
                f"em {caminho_manifesto.name}."
            )
            return False

    # 3. Reconstrói o estado esperado para validar o resultado.
    arquivos_esperados, diretorios_esperados = (
        construir_estado_esperado(
            manifesto_full,
            caminhos_incrementais
        )
    )

    resultado = validar_restauracao(
        destino_restauracao,
        arquivos_esperados,
        diretorios_esperados
    )

    if resultado:
        print(
            "Restauração incremental concluída "
            "e validada com sucesso."
        )
        print(
            f"Destino: {destino_restauracao}"
        )
        return True

    print(
        "Erro: restauração concluída, "
        "mas a validação falhou."
    )
    return False


if __name__ == "__main__":
    args = receber_argumentos_restore_incremental()

    restore_incremental(
        args.origem,
        args.path_backup
    )
