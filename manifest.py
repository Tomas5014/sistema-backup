from pathlib import Path
import json
from utils import calcular_hash


# Salva um manifest em um diretório destino
def salvar_manifest(dados, destino):
    with open(destino, "w", encoding='utf-8') as arquivo:
        json.dump(
            dados,
            arquivo,
            indent=4,
            ensure_ascii=False,
        )


# Retorna um dicionário dos arquivos de um diretório
def listar_arquivos(origem):
    origem = Path(origem).expanduser().resolve()

    arquivos = []

    for caminho in origem.rglob("*"):       # Percorre recursivamente tudo que existe dentro dessa pasta
        if caminho.is_file():
            info = caminho.stat()
            caminho_relativo = caminho.relative_to(origem)

            arquivos.append({
                'nome': str(caminho_relativo), 
                'tamanho': info.st_size, 
                'modificado': info.st_mtime,
                'hash': calcular_hash(caminho)
            })
    
    return arquivos

def listar_diretorios(origem):
    origem = Path(origem).expanduser().resolve()

    diretorios = []

    for caminho in origem.rglob("*"):
        if caminho.is_dir():
            relativo = caminho.relative_to(origem)
            diretorios.append(str(relativo))

    return diretorios

# Gera um manifest para um backup full
def gerar_manifest_full(origem, data, hora, arquivo_backup):

    origem = Path(origem).expanduser().resolve()
    arquivos = listar_arquivos(origem)
    diretorios = listar_diretorios(origem)
    
    manifest = {
        "tipo": "backup_full",
        "origem": str(origem),
        "data": f"{data} {hora.replace("-",":")}",
        "arquivo_backup": Path(arquivo_backup).name,
        "arquivos": arquivos,
        "diretorios": diretorios
    }

    return manifest


# Gera um manifest para um backup diferencial
def gerar_manifest_diferencial(origem, diferencas, base_full, data, hora, nome_arquivo_backup):
    origem = Path(origem).expanduser().resolve()
    
    manifest = {
        "tipo": "diferencial",
        "base_full": Path(base_full).name,
        "arquivo_backup_diferencial": nome_arquivo_backup,
        "origem": str(origem),
        "data": f"{data} {hora.replace("-",":")}",
        "novos": diferencas["novos"],
        "alterados": diferencas["alterados"],
        "removidos": diferencas["removidos"],
        "diretorios_novos": diferencas["diretorios_novos"],
        "diretorios_removidos": diferencas["diretorios_removidos"],
        
    }
    return manifest


# Carrega o json do manifesto em um dicionário
def carregar_manifest(caminho):
    with open(caminho, "r", encoding="utf-8") as arquivo:
        dados = json.load(arquivo)

    return dados

#Retorna um dicionário de arquivos onde o indicie é o nome do arquivo
# Essa função será usada para facilitar a busca de um arquivo pelo nome sem tem que percorre-lo.
def indexar_arquivos(arquivos):
    indice = {}

    for arquivo in arquivos:
        indice[arquivo["nome"]] = arquivo

    return indice


# Retorna todos os arquivos novos, modificados e removidos entre um manifesto 
# e arquivos de um diretório.
def comparar_manifest(manifest_antigo, arquivos_atuais, diretorios_atuais):
    arquivos_antigos = manifest_antigo["arquivos"]
    diretorios_antigos = manifest_antigo["diretorios"]

    antigos = indexar_arquivos(arquivos_antigos)
    atuais = indexar_arquivos(arquivos_atuais)

    antigos_dir = set(diretorios_antigos)
    atuais_dir = set(diretorios_atuais)

    novos = []
    alterados = []
    removidos = []
    diretorios_novos = list(atuais_dir - antigos_dir)
    diretorios_removidos = list(antigos_dir - atuais_dir)

    # Verifica se há arquivos alterados ou novos em atuais
    for nome, arquivo_atual in atuais.items():
        if nome not in antigos:
            novos.append(arquivo_atual)
        else:
            hash_atual = arquivo_atual["hash"]
            hash_antigo = antigos[nome]["hash"]
            if hash_atual != hash_antigo:
                alterados.append(arquivo_atual)

    # Verifica se há arquivos removidos em antigos.
    for nome, arquivo_antigo in antigos.items():
        if nome not in atuais:
            removidos.append(arquivo_antigo)

    return{
        "novos": novos,
        "alterados": alterados,
        "removidos": removidos,
        "diretorios_novos": diretorios_novos,
        "diretorios_removidos": diretorios_removidos,
    }

# Encontra o manifest do backup mais recente dentro do destino
def encontrar_manifest_full_mais_recente(origem, destino):
    destino = Path(destino).expanduser().resolve()
    origem = Path(origem).expanduser().resolve()

    candidatos = []

    for caminho in destino.glob("*-manifesto-full.json"):
        manifest = carregar_manifest(caminho)
        if(
            manifest["tipo"] == "backup_full"
            and Path(manifest["origem"]).resolve() == origem
        ):
            candidatos.append(caminho)

    if not candidatos: return None

    return max(
        candidatos,
        key=lambda caminho: caminho.stat().st_mtime
    )

# Encontra o manifest do backup mais recente dentro do destino
def encontrar_manifest_diferencial_mais_recente(origem, destino, base_full):
    destino = Path(destino).expanduser().resolve()
    origem = Path(origem).expanduser().resolve()

    candidatos = []

    for caminho in destino.glob("*-manifesto-diferencial.json"):
        manifest = carregar_manifest(caminho)
        if(
            manifest["tipo"] == "diferencial"
            and manifest["base_full"] == base_full
            and Path(manifest["origem"]).resolve() == origem
        ):
            candidatos.append(caminho)

    if not candidatos: return None

    return max(
        candidatos,
        key=lambda caminho: caminho.stat().st_mtime
    )

def aplicar_incremental_arquivos(estado, manifest_incremental):
    estado = estado.copy()

    for arquivo in manifest_incremental["novos"]:
        estado[arquivo["nome"]] = arquivo

    for arquivo in manifest_incremental["alterados"]:
        estado[arquivo["nome"]] = arquivo

    for arquivo in manifest_incremental["removidos"]:
        estado.pop(arquivo["nome"], None)

    return estado

def aplicar_incremental_diretorios(estado, manifest_incremental):
    estado = estado.copy()

    for diretorio in manifest_incremental["diretorios_novos"]:
        estado.add(diretorio)

    for diretorio in manifest_incremental["diretorios_removidos"]:
        estado.discard(diretorio)

    return estado