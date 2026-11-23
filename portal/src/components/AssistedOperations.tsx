import {useEffect,useRef,useState} from "react";
import {ApiError,assistedSummary,requestAssisted,retryAssisted,revokeRecoveryExport,downloadRecoveryExport,
 type AssistedAction,type AssistedSummary,type AssistedOperation} from "../api";
import {formatDate} from "../format";

const actions:Record<AssistedAction,string>={replace:"Substituir computador",close:"Encerrar instalação",reopen:"Reabrir instalação",catalog:"Consultar pontos em B",restore:"Preparar arquivos para recuperação"};
const stages={pending:"Aguardando executor",running:"Verificando",succeeded:"Concluída",failed:"Não concluída"};
const lifecycle={active:"Computador em operação",replacing:"Substituição solicitada",awaiting_device:"Aguardando novo computador",closing:"Encerramento solicitado",closed:"Instalação encerrada",reopening:"Conferindo reabertura"};
const access={none:"Nenhuma revogação solicitada",requested:"Revogação SSH solicitada; aguardando confirmação de A",confirmed:"A confirmou os acessos previstos neste atendimento",failed:"Revogação SSH não confirmada; existem acessos pendentes"};
const errors:Record<string,string>={unavailable:"Executor ou armazenamento indisponível. Os dados foram preservados.",busy:"Há uma operação em andamento. Aguarde e tente novamente.",identity_changed:"A identidade ou o inventário mudou. O administrador deve conferir os repositórios.",reference_invalid:"O ponto não está disponível ou contém um formato não suportado nesta prova.",recovery_failed:"A conferência dos arquivos falhou. Nenhum pacote foi liberado.",interrupted:"Operação interrompida e não concluída. Uma pasta parcial, se existir, foi preservada. Consulte os pontos e solicite nova recuperação."};

export function AssistedOperations({id,admin,onChanged}:{id:string;admin:boolean;onChanged?:()=>void}){
 const [data,setData]=useState<AssistedSummary|null>(null),[error,setError]=useState<string|null>(null);
 const [busy,setBusy]=useState(false),[confirm,setConfirm]=useState<AssistedAction|"revoke"|null>(null);
 const [checked,setChecked]=useState(false),[snapshot,setSnapshot]=useState("");
 const life=useRef(0),sequence=useRef(0),intent=useRef<{key:string;id:string;epoch?:number;catalog?:string;snapshot?:string}|null>(null);
 useEffect(()=>{
  let alive=true;const generation=++life.current;
  setData(null);setError(null);setBusy(false);setConfirm(null);setChecked(false);setSnapshot("");intent.current=null;
  async function load(){const seq=++sequence.current;try{const value=await assistedSummary(id);if(alive&&seq===sequence.current){setData(value);setError(null);}}
   catch(err){if(alive&&seq===sequence.current&&!(err instanceof ApiError&&err.status===404))setError(err instanceof Error?err.message:"Falha na consulta do atendimento.");}}
  void load();const timer=window.setInterval(()=>void load(),5000);
  return()=>{alive=false;if(life.current===generation)++life.current;++sequence.current;window.clearInterval(timer);};
 },[id]);
 async function perform(fn:()=>Promise<unknown>){
  if(busy)return;const ticket=life.current;setBusy(true);setError(null);
  try{await fn();if(ticket!==life.current)return;setConfirm(null);setChecked(false);setSnapshot("");onChanged?.();
   const seq=++sequence.current,value=await assistedSummary(id);if(ticket===life.current&&seq===sequence.current)setData(value);
  }catch(err){if(ticket===life.current)setError(err instanceof Error?err.message:"Solicitação não confirmada. Consulte o estado antes de repetir.");}
  finally{if(ticket===life.current)setBusy(false);}
 }
 const running=data?.operations.find(o=>o.state==="pending"||o.state==="running");
 const catalog=data?.operations.find(o=>o.action==="catalog"&&o.state==="succeeded"&&o.device_epoch===data.device_epoch);
 const recoveries=data?.operations.filter(o=>o.action==="restore")??[];
 function choose(action:AssistedAction|"revoke"){
  intent.current={key:action,id:crypto.randomUUID(),epoch:data?.device_epoch,catalog:catalog?.id,snapshot};
  setConfirm(action);setChecked(false);setError(null);
 }
 function submit(){
  if(!data||!confirm||!checked||confirm==="revoke")return;
  const action=confirm,decision=intent.current;
  if(!decision?.epoch)return;
  void perform(()=>requestAssisted(id,{id:decision.id,action,expected_epoch:decision.epoch!,confirmed:true,
   ...(action==="restore"?{catalog_id:decision.catalog,snapshot_b:decision.snapshot}:{})}));
 }
 async function download(op:AssistedOperation){
  const ticket=life.current;
  await perform(async()=>{const blob=await downloadRecoveryExport(id,op.id);if(ticket!==life.current)return;
   const url=URL.createObjectURL(blob),link=document.createElement("a");link.href=url;link.download=`recuperacao-${op.id}.zip`;link.click();window.setTimeout(()=>URL.revokeObjectURL(url),0);
  });
 }
 if(!data&&!error)return null;
 return <section aria-label="Operação e recuperação assistidas" className="assisted-operations">
  <h3>Operação e recuperação assistidas · Laboratório</h3>
  {error?<p role="alert" className="error-state">{error}</p>:null}
  {data?<>
   <p role="status">{lifecycle[data.lifecycle]}. {access[data.access_state]}.</p>
   {data.access_state!=="none"?<p>Os códigos e tokens centrais anteriores foram revogados. Isso não elimina segredos ou arquivos já copiados no computador antigo.</p>:null}
   {data.lifecycle==="awaiting_device"?<p>{data.current_device?"A chave do novo computador foi registrada. Aguarde a prova de B e retome a mesma ativação no aplicativo.":"Próximo passo: gere um código de ativação e use-o no computador substituto. A liberação exige chave nova e nova prova de B."}</p>:null}
   {data.lifecycle==="closed"?<p>Histórico, repositórios e pontos foram preservados. Você pode recuperar usando B ou solicitar reabertura com identidade, inventário e nova prova conferidos.</p>:null}
   {admin?<div className="form-actions">
    {(data.lifecycle==="active"||(data.lifecycle==="awaiting_device"&&!!data.current_device))?<button className="btn" disabled={busy||!!running} onClick={()=>choose("replace")}>Substituir computador</button>:null}
    {(["active","awaiting_device"] as string[]).includes(data.lifecycle)?<button className="btn" disabled={busy||!!running} onClick={()=>choose("close")}>Encerrar instalação</button>:null}
    {data.lifecycle==="closed"?<button className="btn" disabled={busy||!!running} onClick={()=>choose("reopen")}>Reabrir instalação</button>:null}
    <button className="btn" disabled={busy||!!running} onClick={()=>choose("catalog")}>Recuperar usando B</button>
   </div>:null}
   <p>A recuperação usa somente B. Preparar os arquivos não reconstrói A nem retoma a agenda do computador.</p>
   {catalog?<div>
    <h4>Pontos disponíveis em B</h4>
    <p>Consulta confirmada em {formatDate(catalog.finished_at)}. O executor confere novamente o ponto antes de recuperar.</p>
    {catalog.result?.points?.length?<>
     <label htmlFor={`protected-point-${id}`}>Ponto para recuperação</label>
     <select id={`protected-point-${id}`} value={snapshot} disabled={busy||!!running||!admin||confirm!==null} onChange={e=>setSnapshot(e.target.value)}>
      <option value="">Selecione um ponto</option>
      {catalog.result.points.map(p=><option key={p.snapshot_b} value={p.snapshot_b}>{formatDate(p.time)} · B {p.snapshot_b.slice(0,12)} · cópia confirmada</option>)}
     </select>
     <details><summary>Identificação dos pontos e da instalação</summary><p>Instalação <code>{id}</code></p>
      {catalog.result.points.map(p=><p key={p.snapshot_b}>A <code>{p.snapshot_a}</code><br/>B <code>{p.snapshot_b}</code></p>)}
     </details>
     {admin?<button className="btn" disabled={!snapshot||busy||!!running} onClick={()=>choose("restore")}>Preparar arquivos para recuperação</button>:null}
    </>:<p>Consulta concluída, sem pontos disponíveis confirmados para esta instalação.</p>}
   </div>:null}
   {confirm&&confirm!=="revoke"?<div className="confirmation" role="group" aria-label="Confirmar atendimento">
    <h4>{actions[confirm]}</h4>
    {confirm==="replace"||confirm==="close"?<p>Serão revogados códigos, tokens e autorizações SSH do computador anterior. Histórico e dados de A/B serão preservados. A confirmação SSH depende do executor de A; falha ou indisponibilidade fica pendente.</p>:null}
    {confirm==="close"?<p>Novos provisionamentos e ativações desta instalação ficarão bloqueados. A agenda local antiga deve ser pausada no aplicativo. Nenhum repositório ou segredo de recuperação será apagado.</p>:null}
    {confirm==="reopen"?<p>A precisa voltar com o mesmo repositório, inventário e vínculo. Um repositório vazio será recusado. Depois será exigida nova ativação e prova de B.</p>:null}
    {confirm==="catalog"?<p>O executor consultará os pontos reais desta instalação em B, sem acessar A.</p>:null}
    {confirm==="restore"?<p>O ponto selecionado será restaurado em pasta nova privada no laboratório e conferido. Você poderá baixar os arquivos por 24 horas e abri-los no aplicativo Windows, sem credencial de B. Origem, agenda e configuração ativa não serão substituídas.</p>:null}
    <label><input type="checkbox" checked={checked} onChange={e=>setChecked(e.target.checked)} disabled={busy}/>Conferi a instalação e autorizo este atendimento</label>
    <div className="form-actions"><button className="btn btn-primary" disabled={!checked||busy} onClick={submit}>{busy?"Registrando…":"Confirmar atendimento"}</button>
     <button className="btn" disabled={busy} onClick={()=>setConfirm(null)}>Cancelar</button></div>
   </div>:null}
   <h4>Acompanhamento</h4>
   {data.operations.length===0?<p>Nenhum atendimento solicitado.</p>:<ul className="assisted-history">{data.operations.slice(0,10).map(op=><li key={op.id}>
    <strong>{actions[op.action]} · {stages[op.state]}</strong><p>{formatDate(op.finished_at??op.started_at??op.created_at)}</p>
    {op.state==="failed"?<p role="alert">{errors[op.error_code??""]??"O executor não confirmou a conclusão. Dados preservados."}</p>:null}
    {op.action==="restore"&&op.state==="succeeded"?<p>{op.result?.files} arquivos conferidos. Download até {formatDate(op.result?.expires_at??null)}.</p>:null}
    {admin&&op.action==="restore"&&op.state==="succeeded"&&!op.result?.export_revoked&&op.result?.expires_at&&Date.parse(op.result.expires_at)>Date.now()?<div className="form-actions">
     <button className="btn" disabled={busy} onClick={()=>void download(op)}>Baixar arquivos recuperados</button>
     <button className="btn" disabled={busy} onClick={()=>{setConfirm("revoke");setChecked(false);intent.current={key:"revoke",id:op.id};}}>Revogar download</button>
    </div>:null}
    {op.result?.export_revoked?<p>Download revogado. Arquivos já baixados não são removidos.</p>:null}
    {admin&&op.state==="failed"&&op.action!=="restore"?<button className="btn" disabled={busy||!!running} onClick={()=>void perform(()=>retryAssisted(id,op.id))}>Tentar novamente este atendimento</button>:null}
    <details><summary>Detalhes técnicos do atendimento</summary><p>Referência <code>{op.id}</code> · geração {op.device_epoch} · tentativa {op.attempt} · etapa {op.step}</p>
     {op.error_code?<p>Categoria: {op.error_code}</p>:null}{op.result?.archive_sha256?<p>SHA-256 <code>{op.result.archive_sha256}</code></p>:null}</details>
   </li>)}</ul>}
   {confirm==="revoke"?<div role="group" aria-label="Confirmar revogação do download"><p>O link deixará de funcionar. Arquivos já baixados e dados de B serão preservados.</p><button className="btn" disabled={busy} onClick={()=>void perform(()=>revokeRecoveryExport(id,intent.current!.id))}>Confirmar revogação do download</button><button className="btn" onClick={()=>setConfirm(null)}>Cancelar</button></div>:null}
   {recoveries.some(o=>o.state==="failed")?<p>Uma falha não libera arquivos parciais. Faça nova consulta de B para solicitar outra recuperação em pasta nova.</p>:null}
  </>:null}
 </section>;
}
