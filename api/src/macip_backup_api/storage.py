"""Política conservadora do laboratório; nunca executa comandos nem calcula espaço recuperável."""
from datetime import UTC, datetime
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Digest = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Count = Annotated[int, Field(strict=True, gt=0, le=10000)]
Bytes = Annotated[int, Field(strict=True, gt=0, le=2**63-1)]


class Policy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    keep_a: Count | None = None
    keep_b: Count | None = None
    low_a: Bytes | None = None
    low_b: Bytes | None = None
    pins_a: list[Digest] = Field(default_factory=list, max_length=100)
    pins_b: list[Digest] = Field(default_factory=list, max_length=100)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def plan(context, side, points, peer):
    """Inventários reais são obtidos sob exclusão pelos executores, não pelo navegador."""
    policy = Policy.model_validate(context['policy'])
    ordered = sorted(points, key=lambda p:(datetime.fromisoformat(p['time']),p['id']), reverse=True)
    if len({p['id'] for p in ordered}) != len(ordered):
        raise ValueError('inventário ambíguo')
    keep = getattr(policy,'keep_'+side)
    pinned = set(getattr(policy,'pins_'+side))
    binding=context.get('protection') or {}
    proof=binding.get('proof') or {}
    last=binding.get('last_copy') or {}
    pinned.update(p for p in (proof.get('snapshot_'+side),last.get('snapshot_'+side)) if p)
    if side=='a' and context.get('seed'):pinned.add(context['seed']['snapshot_a'])
    if not pinned.issubset({p['id'] for p in ordered}):
        raise ValueError('ponto protegido ausente; inspeção necessária')
    pairs=context['pairs']
    peer_ids=set(peer)
    rows=[]
    for index,point in enumerate(ordered):
        reason=None
        if keep is None:reason='no_policy'
        elif index<keep:reason='keep_last'
        elif point['id'] in pinned:reason='protected'
        elif side=='a':
            target=pairs.get(point['id'])
            if not binding.get('proof') or not target or target not in peer_ids:
                reason='copy_unproven'
        else:
            # B nunca perde a proteção de um ponto ainda publicado em A.
            if any(a in peer_ids and b==point['id'] for a,b in pairs.items()):
                reason='source_present'
        rows.append(dict(id=point['id'],time=point['time'],action='keep' if reason else 'remove',reason=reason))
    value=dict(side=side,repository_id=context['repository_'+side],source=context['source'],
               binding_id=binding.get('binding_id'),policy=context['policy'],points=rows,peer=sorted(peer_ids))
    value['fingerprint']=digest(value)
    return value


def capacity_public(value, *, now=None, threshold=None, worker_stale=False):
    now=now or datetime.now(UTC)
    good=value.get('last_good') if value else None
    stale=worker_stale or not good or bool(value.get('error'))
    if good:
        age=(now-datetime.fromisoformat(good['measured_at'])).total_seconds()
        stale=stale or age<0 or age>120
    return dict(last_good=good, last_attempt=value.get('last_attempt') if value else None,
                error=value.get('error') if value else None,stale=bool(stale),
                low=bool(not stale and threshold is not None and good['free']<=threshold))


class PlannedPoint(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: Digest
    time: str = Field(max_length=80)
    action: Literal['keep','remove']
    reason: Literal['no_policy','keep_last','protected','copy_unproven','source_present'] | None


class RetentionPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    side: Literal['a','b']
    repository_id: Digest
    source: Digest
    binding_id: str
    policy: Policy
    points: list[PlannedPoint] = Field(max_length=1000)
    peer: list[Digest] = Field(max_length=1000)
    fingerprint: Digest
