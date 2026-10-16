"""
    python3 backup-full.py ~/testar-backup/arquivos-teste/ ~/testar-backup/backups/
"""

import time
import subprocess
from pathlib import Path
import tarfile
import argparse
from utils import inicio, validar_diretorios, termino, gera_log
import manifest


# Função para receber os argumentos via linha de comando
def receber_argumentos_full():
    parser = argparse.ArgumentParser(
        description='Sistema de Backup Full'
    )
    parser.add_argument(
        'origem',
        help="Diretório que será copiado."
    )
    parser.add_argument(
        'destino',
        help="Diretório onde o backup será armazenado."
    )

    return parser.parse_args()


# Constroi o arquivo e path de backup e rotorna
def gera_backup(origem, destino,data,hora):
    nome_arquivo_backup = f"{data}_{hora}-backup-full.tar.gz"
    path_destino = destino / nome_arquivo_backup                # Operador "/" junta caminhos
    backup = ['tar', 'czvf', path_destino, "-C", origem, "."]
    return backup, path_destino


# Verifica a integridade do arquivo tar.gz gerado
def verifica_backup (path_backup):
    try:
        with tarfile.open(str(path_backup), "r:gz") as arquivo:
            membros = arquivo.getmembers()

        if len(membros) > 0:
            print("Integridade do backup verificada com sucesso!")
            return True

        print("Erro: Backup está vazio.")
        return False
    
    except (tarfile.TarError, EOFError, OSError) as erro:
        print(f"Erro ao verificar o backup: {erro}")
        return False


def validar_backup_full (resultado_backup, path_backup):
    if resultado_backup.returncode != 0:
        return False, f"falha ao executar backup."

    if not path_backup.exists():
        return False, f"arquivo de backup não foi criado."

    if path_backup.stat().st_size == 0:
        return False, f"arquivo de backup está vazio."

    if not verifica_backup(path_backup):
        return False, f"backup criado, mas falhou na verificação."

    return True, f"Backup realizado com sucesso!"

# Cria os backups
def backup_full(origem, destino):
    dia_incio = (time.strftime("%Y-%m-%d"))
    hora_inicio = time.strftime("%H-%M-%S")

    # Transformar string em Path
    origem = Path(origem).expanduser().resolve()
    destino = Path(destino).expanduser().resolve()
    destino_log = destino / "logs"

    # As necessidades dos três diretórios fornecidos pelo usuário (existência e se é diretório)
    validacao_diretorios, msg_erro = validar_diretorios(origem, destino, destino_log)
    if (not validacao_diretorios):
        print(f"Erro: {msg_erro}")
        return False

    # Cria os diretórios de destino, caso necessário.
    destino.mkdir(parents=True, exist_ok=True)
    destino_log.mkdir(parents=True, exist_ok=True)
    
    path_log = gera_log(destino_log, "full")
    backup, path_backup = gera_backup(origem, destino, dia_incio, hora_inicio)
    start = inicio(hora_inicio)

    # Printa o Banner
    l = open(path_log, 'w');
    l.write(start)
    l.close()

    # Monta todos os discos que estão no FSTAB
    mount = ['mount', '-a']
    resultado_mont = subprocess.run(mount)
    if (resultado_mont.returncode !=0):
        print("Erro: Falha ao montar discos.")
        return

    # Roda o backup
    with open(path_log, 'a') as log:
        resultado_backup = subprocess.run(
            backup,
            stdout=log,     # Imprime a saida, em caso de sucesso, no arquivo de log.
            stderr=log      # Imprime o erro, em caso de falha, no arquivo de log.
        )

    validacao_backup, msg_validacao_backup = validar_backup_full(resultado_backup, path_backup)

    # Validar se o backup full foi feito corretamente
    if (validacao_backup):
        print(msg_validacao_backup)
    else:
        print(f"Erro: {msg_validacao_backup}")
        return False


    # Printa o final e relatório
    final = termino(dia_incio, hora_inicio, path_backup, path_log)
    r = open(path_log, 'a')
    r.write(final)
    r.close()

    # Gera e salva o manifest full com os arquivos da origem
    destino_manifesto = destino / f"{dia_incio}_{hora_inicio}-manifesto-full.json"
    manifesto = manifest.gerar_manifest_full(origem, dia_incio, hora_inicio, path_backup)
    manifest.salvar_manifest(manifesto, destino_manifesto)


if (__name__ == "__main__"):
    # origem = '~/testar-backup/arquivos-teste/'
    # destino = '~/testar-backup/backups/'
    args = receber_argumentos_full()
    backup_full(args.origem, args.destino)