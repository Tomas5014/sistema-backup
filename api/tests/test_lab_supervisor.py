"""Processos POSIX próprios e descartáveis, sem Docker/systemd ou credenciais."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from macip_backup_api.lab_supervisor import control,identity,signal_identity,stop_child
from macip_backup_api.lab_runtime import json_once

pytestmark=pytest.mark.skipif(os.name!='posix',reason='supervisor de laboratório exige POSIX')


def test_reused_pid_identity_does_not_signal_other_process():
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)
    try:
        token=identity(child.pid)
        signal_identity(str(child.pid)+':0',signal.SIGKILL,group=True)
        assert child.poll() is None
        signal_identity(token,signal.SIGTERM,group=True)
        child.wait(timeout=5)
        assert identity(child.pid)!=token
    finally:
        if child.poll() is None:child.kill();child.wait()


def test_orphan_worker_requires_selective_stop_before_start(tmp_path):
    root=tmp_path/'área própria';root.mkdir(mode=0o700)
    state=root/'private/services';state.mkdir(mode=0o700,parents=True)
    own=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)
    other=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)
    try:
        json_once(state/'processes.json',dict(supervisor=None,children={'alerts':identity(own.pid)},restarts={}))
        with pytest.raises(RuntimeError,match='ainda ativos'):control(root,'start')
        assert control(root,'stop')['running'] is False
        own.wait(timeout=5)
        assert other.poll() is None
    finally:
        for proc in (own,other):
            if proc.poll() is None:proc.kill()
            proc.wait()


def test_supervised_stop_terminates_registered_stopped_child(tmp_path):
    root=tmp_path/'lab';root.mkdir(mode=0o700)
    registry=root/'private/services/children-protection';registry.mkdir(mode=0o700,parents=True)
    child_file=root/'child.pid'
    script="import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
    parent=subprocess.Popen([sys.executable,'-c',script,str(child_file)],start_new_session=True)
    token=None
    try:
        deadline=time.monotonic()+5
        while not child_file.exists() and time.monotonic()<deadline:time.sleep(.01)
        child=int(child_file.read_text());token=identity(child)
        file=registry/str(child);file.write_text(token);file.chmod(0o600)
        signal_identity(token,signal.SIGSTOP,group=True)
        stop_child(root,'protection',parent,identity(parent.pid))
        assert identity(child)!=token
    finally:
        if token:signal_identity(token,signal.SIGKILL,group=True)
        if parent.poll() is None:parent.kill()
        parent.wait()


def test_child_started_during_graceful_shutdown_is_also_stopped(tmp_path):
    root=tmp_path/'lab';root.mkdir(mode=0o700)
    registry=root/'private/services/children-protection';registry.mkdir(mode=0o700,parents=True)
    ready=root/'ready'
    script=r'''
import os,signal,subprocess,sys,time
from pathlib import Path
registry=Path(sys.argv[1])
def spawn(*_):
    p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)
    fields=Path(f'/proc/{p.pid}/stat').read_text().rsplit(')',1)[1].split()
    file=registry/str(p.pid);file.write_text(str(p.pid)+':'+fields[19]);file.chmod(0o600)
signal.signal(signal.SIGTERM,spawn)
Path(sys.argv[2]).touch()
while True:time.sleep(.1)
'''
    parent=subprocess.Popen([sys.executable,'-c',script,str(registry),str(ready)],start_new_session=True)
    try:
        deadline=time.monotonic()+5
        while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
        stop_child(root,'protection',parent,identity(parent.pid))
        values=[file.read_text() for file in registry.iterdir()]
        assert values and all(identity(int(value.split(':')[0]))!=value for value in values)
    finally:
        for file in registry.iterdir():signal_identity(file.read_text(),signal.SIGKILL,group=True)
        if parent.poll() is None:parent.kill()
        parent.wait()
