"""Administração LOCAL de operadores — bootstrap e recuperação.

Sem usuário padrão: o primeiro administrador nasce aqui. A senha entra
por `getpass` (interativo) ou `--password-stdin` (pipe) — NUNCA por
argumento de linha de comando, que vazaria em `ps`/histórico.

Também serve para recuperação administrativa: redefinir senha e
reativar operador quando a sessão HTTP foi perdida/revogada.

Uso:
    macip-backup-api-operator create --login Ana --name "Ana" --role admin
    macip-backup-api-operator reset-password --login ana
    macip-backup-api-operator set-active --login ana --active true
    macip-backup-api-operator list
"""

from __future__ import annotations

import argparse
import getpass
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import engine, migrate_now
from .models import Operator
from .operators import (
    audit,
    hash_password,
    normalize_login,
    revoke_sessions,
)


def _password(args, confirm: bool) -> str:
    if args.password_stdin:
        password = sys.stdin.read().rstrip("\n")
    else:
        password = getpass.getpass("Senha: ")
        if confirm:
            again = getpass.getpass("Confirme a senha: ")
            if password != again:
                raise SystemExit("senhas divergentes")
    if not 8 <= len(password) <= 256:
        raise SystemExit("senha deve ter entre 8 e 256 caracteres")
    return password


def _get_operator(session: Session, login: str) -> Operator:
    operator = session.execute(
        select(Operator).where(Operator.login == normalize_login(login))
    ).scalar_one_or_none()
    if operator is None:
        raise SystemExit(f"operador '{login}' inexistente")
    return operator


def main() -> int:
    parser = argparse.ArgumentParser(prog="macip-backup-api-operator")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="cria operador (bootstrap)")
    create.add_argument("--login", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--role", choices=("admin", "viewer"), required=True)
    create.add_argument(
        "--password-stdin",
        action="store_true",
        help="lê a senha de stdin em vez de getpass",
    )

    reset = commands.add_parser("reset-password", help="redefine a senha")
    reset.add_argument("--login", required=True)
    reset.add_argument("--password-stdin", action="store_true")

    active = commands.add_parser("set-active", help="ativa/desativa operador")
    active.add_argument("--login", required=True)
    active.add_argument("--active", choices=("true", "false"), required=True)

    commands.add_parser("list", help="lista operadores (sem segredos)")

    args = parser.parse_args()
    migrate_now()  # esquema pronto também no primeiro uso
    with Session(engine()) as db:
        if args.command == "create":
            operator = Operator(
                id=uuid.uuid4(),
                login=normalize_login(args.login),
                name=args.name,
                password_hash=hash_password(_password(args, confirm=True)),
                role=args.role,
                active=True,
            )
            db.add(operator)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise SystemExit(f"login '{operator.login}' já existe")
            audit.info("bootstrap operator criado login=%s", operator.login)
            print(f"operador '{operator.login}' ({operator.role}) criado")
            return 0
        if args.command == "reset-password":
            operator = _get_operator(db, args.login)
            operator.password_hash = hash_password(_password(args, confirm=True))
            revoke_sessions(db, operator.id)  # sessões antigas morrem
            db.commit()
            audit.info("reset local senha login=%s", operator.login)
            print(f"senha de '{operator.login}' redefinida — sessões revogadas")
            return 0
        if args.command == "set-active":
            operator = _get_operator(db, args.login)
            operator.active = args.active == "true"
            if not operator.active:
                revoke_sessions(db, operator.id)
            db.commit()
            audit.info(
                "set-active local login=%s active=%s", operator.login, operator.active
            )
            print(f"'{operator.login}' active={operator.active}")
            return 0
        if args.command == "list":
            rows = db.execute(select(Operator).order_by(Operator.login)).scalars()
            for row in rows:
                print(
                    f"{row.login}\t{row.role}\tactive={row.active}\t{row.name}"
                )
            return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
