"""Executa Restic local com criação de sockets bloqueada pelo kernel Linux."""

import ctypes
import errno
import os
import socket
import sys


def main():
    seccomp = ctypes.CDLL("libseccomp.so.2")
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise SystemExit("filtro de rede indisponível")
    try:
        deny = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO(EPERM)
        for name in (b"socket", b"socketpair", b"io_uring_setup"):
            syscall = seccomp.seccomp_syscall_resolve_name(name)
            if syscall < 0 or seccomp.seccomp_rule_add(ctx, deny, syscall, 0) != 0:
                raise SystemExit("filtro de rede incompleto")
        if seccomp.seccomp_load(ctx) != 0:
            raise SystemExit("filtro de rede recusado")
    finally:
        seccomp.seccomp_release(ctx)
    try:
        probe = socket.socket()
    except PermissionError as error:
        if error.errno != errno.EPERM:
            raise
    else:
        probe.close()
        raise SystemExit("filtro de rede ineficaz")
    os.execv(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
