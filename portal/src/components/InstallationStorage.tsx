import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, installationStorage, saveStoragePolicy, requestStorage,
  type InstallationStorage as Summary, type StoragePolicy } from "../api";
import { formatDate } from "../format";

const reasons: Record<string,string> = {no_policy:"Sem política",keep_last:"Quantidade mantida",protected:"Ponto protegido",
  copy_unproven:"Cópia em B não comprovada",source_present:"Ponto ainda publicado em A"};
const errors: Record<string,string> = {busy:"Operação concorrente. Aguarde sem remover locks.",unavailable:"Armazenamento indisponível. Confira o executor e simule novamente.",
  identity_changed:"Identidade alterada. Confira os repositórios antes de simular novamente.",inventory_changed:"Os pontos mudaram. Simule novamente.",
  reference_stale:"Inventário do outro servidor sem medição recente. Aguarde os executores e simule novamente.",interrupted:"Operação interrompida. Confira os IDs removidos e simule novamente."};
const states: Record<string,string> = {pending:"Na fila",running:"Em processamento",succeeded:"Concluída",failed:"Falhou"};
function bytes(value: number) { return `${(value / 1024 ** 3).toLocaleString("pt-BR",{maximumFractionDigits:3})} GiB`; }

export function InstallationStorage({id,admin}:{id:string;admin:boolean}) {
  const [data,setData]=useState<Summary|null>(null);
  const [error,setError]=useState<string|null>(null);
  const [busy,setBusy]=useState(false);
  const [confirm,setConfirm]=useState<string|null>(null);
  const [editing,setEditing]=useState(false);
  const [draft,setDraft]=useState<Record<string,string>>({});
  const generation=useRef(0),life=useRef(0);
  useEffect(()=>{
    let alive=true;const lifetime=++life.current;
    setData(null);setError(null);setBusy(false);setConfirm(null);setEditing(false);
    async function load(){
      const ticket=++generation.current;
      try { const value=await installationStorage(id);if(alive && ticket===generation.current){setData(value);setError(null);} }
      catch(err){if(alive && ticket===generation.current && !(err instanceof ApiError && err.status===404))setError(err instanceof Error?err.message:"Falha na consulta.");}
    }
    void load();const timer=window.setInterval(()=>void load(),15000);
    return ()=>{alive=false;if(life.current===lifetime)++life.current;++generation.current;window.clearInterval(timer);};
  },[id]);
  async function action(fn:()=>Promise<unknown>){
    if(busy)return;const ticket=life.current;setBusy(true);setError(null);
    try {await fn();if(ticket!==life.current)return;setConfirm(null);setEditing(false);
      const request=++generation.current;const value=await installationStorage(id);
      if(ticket===life.current && request===generation.current)setData(value);
    }catch(err){if(ticket===life.current)setError(err instanceof Error?err.message:"Falha na solicitação.");}
    finally{if(ticket===life.current)setBusy(false);}
  }
  function edit(){if(!data)return;setDraft(Object.fromEntries(Object.entries(data.policy).map(([k,v])=>[k,Array.isArray(v)?v.join("\n"):v===null?"":String(v)])));setEditing(true);setConfirm(null);}
  function save(ev:FormEvent){
    ev.preventDefault();
    try {
      const policy={} as StoragePolicy;
      for(const k of ["keep_a","keep_b","low_a","low_b"] as const){
        const raw=draft[k]?.trim()??"";
        if(raw && (!/^[1-9]\d*$/.test(raw)||!Number.isSafeInteger(Number(raw))))throw new Error("Use inteiros positivos ou deixe vazio.");
        policy[k]=raw?Number(raw):null;
      }
      for(const k of ["pins_a","pins_b"] as const){
        policy[k]=(draft[k]??"").split("\n").map(x=>x.trim()).filter(Boolean);
        if(policy[k].some(x=>!(/^[a-f0-9]{64}$/).test(x)))throw new Error("Pontos fixados exigem IDs completos, um por linha.");
      }
      void action(()=>saveStoragePolicy(id,policy));
    }catch(err){setError((err as Error).message);}
  }
  if(!data&&!error)return null;
  return <section aria-label="Capacidade e retenção">
    <h3>Capacidade e retenção · Laboratório</h3>
    <p>Exclusão automática desativada. Remover pontos não recupera espaço imediatamente. Esta ação não executa prune.</p>
    {error?<p role="alert" className="error-state">{error}</p>:null}
    {data?<>
      <p>Medições de volume compartilhado não devem ser somadas entre instalações.</p>
      {(["a","b"] as const).map(side=>{
        const c=data.capacity[side],sample=c.last_good;
        const op=data.operations.find(o=>o.side===side);
        const p=op?.result?.plan;
        return <section key={side} aria-label={`Armazenamento ${side.toUpperCase()}`}>
          <h4>{side==="a"?"A · Primário":"B · Protegido"}</h4>
          <p role="status">{c.stale?"Sem medição recente":c.low?"Espaço disponível atingiu o limite configurado":"Medição recente"}</p>
          {c.last_attempt?<p>Última tentativa de medição: {formatDate(c.last_attempt)}{c.error?` · ${errors[c.error]??"Medição indisponível"}`:""}</p>:null}
          {sample?<dl className="preparation-meta">
            <div><dt>Volume compartilhado · disponível / utilizado / total</dt><dd>{bytes(sample.free)} / {bytes(sample.used)} / {bytes(sample.total)}</dd></div>
            <div><dt>Uso individual do repositório</dt><dd>{sample.repository_bytes===null?"Não medido":bytes(sample.repository_bytes)}</dd></div>
            <div><dt>Última medição válida{c.stale?" · histórica":""}</dt><dd>{formatDate(sample.measured_at)}</dd></div>
          </dl>:null}
          <p>Manter: {data.policy[`keep_${side}`]??"todos os pontos (sem política)"}. Limite de espaço livre: {data.policy[`low_${side}`]===null?"não configurado":bytes(data.policy[`low_${side}`]!)}.</p>
          {admin?<button className="btn" disabled={busy || data.operations.some(o=>["pending","running"].includes(o.state))}
            onClick={()=>void action(()=>requestStorage(id,{id:crypto.randomUUID(),side,action:"simulate"}))}>Simular retenção {side.toUpperCase()}</button>:null}
          {op?<>
            <p>{op.action==="simulate"?"Simulação":"Remoção de snapshots"}: {states[op.state]??op.state} · {formatDate(op.finished_at??op.created_at)} · Solicitante: {op.requester}</p>
            {op.error?<p role="alert">{errors[op.error]??"Falha. Confira a operação antes de tentar novamente."}</p>:null}
            {p?<><p>Repositório <code className="storage-id">{p.repository_id}</code></p>
              <table className="data-table"><thead><tr><th>Data</th><th>Snapshot</th><th>Decisão</th></tr></thead>
                <tbody>{p.points.map(point=><tr key={point.id}><td>{formatDate(point.time)}</td><td><code className="storage-id">{point.id}</code></td>
                  <td>{point.action==="remove"?"Candidato à exclusão":`Manter · ${reasons[point.reason??""]??"Protegido"}`}</td></tr>)}</tbody></table>
              {p.points.length===0?<p>Nenhum ponto encontrado.</p>:null}
            </>:null}
            {op.result?.removed.length?<p>IDs removidos: {op.result.removed.map(v=><code className="storage-id" key={v}>{v} </code>)}</p>:null}
            {admin && op.state==="succeeded" && op.action==="simulate" && p?.points.some(v=>v.action==="remove")?<div>
              <label><input type="checkbox" checked={confirm===op.id} disabled={busy} onChange={ev=>setConfirm(ev.target.checked?op.id:null)}/>
                Confirmo a exclusão dos candidatos em {side.toUpperCase()} desta instalação.</label>
              <button className="btn" disabled={busy || confirm!==op.id || editing} onClick={()=>void action(()=>requestStorage(id,{id:crypto.randomUUID(),side,action:"execute",simulation:op.id}))}>Executar retenção {side.toUpperCase()}</button>
              <p>Os executores conferirão novamente identidade e inventário. Mudanças exigem outra simulação.</p>
            </div>:null}
          </>:null}
        </section>;
      })}
      {admin&&!editing?<button className="btn" disabled={busy} onClick={edit}>Editar política do laboratório</button>:null}
      {admin&&editing?<form onSubmit={save}>
        {(["a","b"] as const).map(side=><fieldset key={side}><legend>{side.toUpperCase()}</legend>
          {([`keep_${side}`,`low_${side}`] as const).map(k=><label key={k}>{k.startsWith("keep")?"Quantidade de pontos (vazio mantém todos)":"Aviso de espaço livre em bytes (vazio desativa)"}
            <input inputMode="numeric" value={draft[k]??""} disabled={busy} onChange={ev=>setDraft({...draft,[k]:ev.target.value})}/></label>)}
          <label>Pontos de recuperação fixados (IDs completos, um por linha)<textarea value={draft[`pins_${side}`]??""} disabled={busy} onChange={ev=>setDraft({...draft,[`pins_${side}`]:ev.target.value})}/></label>
        </fieldset>)}
        <button className="btn" disabled={busy}>Salvar política</button><button className="btn" type="button" disabled={busy} onClick={()=>setEditing(false)}>Cancelar</button>
      </form>:null}
      <details><summary>Histórico de retenção e decisões</summary><ul>{data.operations.map(op=><li key={op.id}>{op.side.toUpperCase()} · {op.action==="simulate"?"Simulação":"Remoção"} · {states[op.state]} · {formatDate(op.created_at)} · {op.requester}{op.error?` · ${errors[op.error]??"Falha"}`:""}{op.result?.removed.map(v=><code className="storage-id" key={v}>{v} </code>)}</li>)}</ul></details>
    </>:null}
  </section>;
}
