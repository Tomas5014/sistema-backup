"""
    python3 backup-diferencial.py \
        ~/testar-backup/arquivos-teste/ \
        ~/testar-backup/backups/ \
        --exclude "*.log" "*.tmp" ".recycle"

    python3 backup-diferencial.py ~/testar-backup/arquivos-teste/ ~/testar-backup/backups/
                
"""

import time
from pathlib import Path
import argparse
import manifest
import tarfile
from utils import inicio, termino, gera_log, validar_diretorios

# Função para receber os argumentos via linha de comando
def receber_argumentos_diferencial():
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
    parser.add_argument(
        '-e',
        '--exclude',
        nargs='*',
        default=[],
        help='Arquivos ou diretórios que serão ignorados do backup.'
    )
    return parser.parse_args()

# Cria o backup diferencial com os arquivos em diferenças
def criar_backup_diferencial(origem, diferencas, destino):
    origem = Path(origem).expanduser().resolve()
    destino = Path(destino).expanduser().resolve()

    arquivos_backup = (diferencas["novos"] + diferencas["alterados"])

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
                recursive=False,
            )

    return destino

# Faz o backup diferencial
def backup_diferencial (origem, destino, exclude):
    hora_inicio = time.strftime('%H-%M-%S')
    dia_inicio = (time.strftime("%Y-%m-%d"))

    # Transforma as strings em Path
    origem = Path(origem).expanduser().resolve()
    destino = Path(destino).expanduser().resolve()
    destino_log = destino / "logs"

    # As necessidades dos três diretórios fornecidos pelo usuário (existência e se é diretório)
    validacao_diretorios, msg_erro = validar_diretorios(origem, destino, destino_log)
    if (validacao_diretorios == False):
        print(f"Erro: {msg_erro}")
        return False

    # Cria os diretórios de destino, caso necessário.
    destino.mkdir(parents=True, exist_ok=True)
    destino_log.mkdir(parents=True, exist_ok=True)

    # Retorna o caminho para o local onde o log sera salvo
    destino_log = gera_log(destino_log, "diferencial")
    
    # Printar o Banner no arquivo de log
    start = inicio(hora_inicio)
    l = open(destino_log, 'w')
    l.write(start)
    l.close()

    # Retorna um dicionário dos arquivos da pasta origem
    arquivos_atuais = manifest.listar_arquivos(origem)

    #Retorna um dicionário dos diretórios atuais da pasta origem
    diretorios_atuais = manifest.listar_diretorios(origem)

    # Encontra o manifest full mais recente, relacionado a pasta origem, na pasta destino
    caminho_manifest_full = manifest.encontrar_manifest_full_mais_recente(origem, destino)

    if caminho_manifest_full is None:
        mensagem_ausencia_full = ("Erro: nenhum backup full encontrado para essa origem.")
        
        print(mensagem_ausencia_full)

        with open(destino_log, "a", encoding="utf-8") as log:
            log.write(f"\n{mensagem_ausencia_full}\n")
        return False
    
    # Carrega os dados do manifesto em um dicionário
    manifesto_full = manifest.carregar_manifest(caminho_manifest_full)

    # Separa todos os arquivos novos, atualizados e removidos do manifest full para os arquivos na pasta origem.
    diferencas = manifest.comparar_manifest(manifesto_full, arquivos_atuais, diretorios_atuais)

    # Cria o backup diferencial
    nome_arquivo_backup = f"{dia_inicio}_{hora_inicio}-backup-diferencial.tar.gz"
    path_backup = destino / nome_arquivo_backup
    criar_backup_diferencial(origem, diferencas, path_backup)

    # Gera e salva o manifesto diferencial
    manifesto_diferencial = manifest.gerar_manifest_diferencial(origem, diferencas, caminho_manifest_full, dia_inicio, hora_inicio, nome_arquivo_backup)
    destino_manifesto = destino / f"{dia_inicio}_{hora_inicio}-manifesto-diferencial.json"
    manifest.salvar_manifest(manifesto_diferencial, destino_manifesto)

    # Printa o final e relatorio
    final = termino(dia_inicio, hora_inicio, path_backup, destino_log)
    r = open(destino_log, 'a')
    r.write(final)
    r.close()


if (__name__ == "__main__"):
    # log = '/home/estagiario01/testar-backup/logs/'
    # origem = '~/testar-backup/arquivos-teste/'
    # destino = '~/testar-backup/backups/'
    # exclude = ('*.log', '*.tmp', '.recycle')
    args = receber_argumentos_diferencial()
    backup_diferencial(args.origem, args.destino, args.exclude)

