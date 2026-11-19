"""Reinício controlado da publicação inicial, sem Docker ou dados operacionais."""
import json
from pathlib import Path
import pytest
from macip_backup_api.lab_repository import initialize_repository


def test_partial_initialization_is_not_published_and_retries(tmp_path):
    repository=tmp_path/'repo';repository.mkdir(mode=0o700)
    calls=[]
    def interrupted(stage):
        (stage/'keys').mkdir();(stage/'keys/partial').write_text('synthetic')
        raise RuntimeError('interrupted')
    with pytest.raises(RuntimeError):initialize_repository(repository,interrupted,lambda p:'never')
    assert not (repository/'config').exists()
    def init(stage):
        calls.append(True);(stage/'config').write_text('synthetic-id')
    assert initialize_repository(repository,init,lambda p:(p/'config').read_text())=='synthetic-id'
    assert len(calls)==1 and (repository/'config').read_text()=='synthetic-id'


def test_completed_unpublished_stage_keeps_identity(tmp_path):
    repository=tmp_path/'repo';repository.mkdir(mode=0o700)
    def init(stage):(stage/'config').write_text('fixed-id')
    def interrupted_verify(stage):raise RuntimeError('worker stopped after init')
    with pytest.raises(RuntimeError):initialize_repository(repository,init,interrupted_verify)
    assert initialize_repository(repository,lambda _:pytest.fail('must not init again'),lambda p:(p/'config').read_text())=='fixed-id'


def test_published_repository_and_unexpected_stage_are_preserved(tmp_path):
    repository=tmp_path/'repo';repository.mkdir(mode=0o700);(repository/'config').write_text('existing')
    with pytest.raises(RuntimeError):
        initialize_repository(repository,lambda p:(p/'config').write_text('candidate'),lambda p:'candidate')
    assert (repository/'config').read_text()=='existing'
    other=tmp_path/'other';other.mkdir(mode=0o700)
    stage=tmp_path/'.other-initializing';stage.mkdir(mode=0o700);(stage/'unknown').write_text('preserve')
    with pytest.raises(RuntimeError):initialize_repository(other,lambda _:None,lambda _:'bad')
    assert (stage/'unknown').read_text()=='preserve'


def test_unpublished_stage_with_snapshot_is_never_discarded(tmp_path):
    repository=tmp_path/'repo';repository.mkdir(mode=0o700)
    stage=tmp_path/'.repo-initializing';stage.mkdir(mode=0o700)
    (stage/'snapshots').mkdir();(stage/'snapshots/point').write_text('synthetic point')
    with pytest.raises(RuntimeError,match='pontos'):
        initialize_repository(repository,lambda _:pytest.fail('must not init'),lambda _:'none')
    assert (stage/'snapshots/point').read_text()=='synthetic point'
