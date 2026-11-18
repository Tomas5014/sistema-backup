"""Endereço fixo do repositório dentro do chroot SFTP da instalação."""

import re

HOST = r"[A-Za-z0-9.-]+"
REPOSITORY = "repository"


def known_hosts_name(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def repository_url(account: str, host: str, port: int) -> str:
    authority = host if port == 22 else f"{host}:{port}"
    # Na sintaxe URL do Restic, // após a autoridade indica caminho absoluto.
    return f"sftp://{account}@{authority}//{REPOSITORY}"


def matches_endpoint(account: str, url: str, known_hosts: str) -> bool:
    match = re.fullmatch(
        rf"sftp://{re.escape(account)}@({HOST})(?::([1-9][0-9]{{0,4}}))?//{REPOSITORY}",
        url,
    )
    if not match:
        return False
    host, port_text = match.groups()
    port = int(port_text or 22)
    return (
        port <= 65535
        and url == repository_url(account, host, port)
        and matches_known_hosts(known_hosts, host, port)
    )


def matches_known_hosts(line: str, host: str, port: int) -> bool:
    name = re.escape(known_hosts_name(host, port))
    return bool(re.fullmatch(
        rf"{name} (ssh-(?:ed25519|rsa)|ecdsa-[^\s]+) [A-Za-z0-9+/=]+",
        line,
    ))
