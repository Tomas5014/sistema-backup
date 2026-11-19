import {useEffect,useState} from "react";
import {ApiError,labVersion,type LabVersion as Version} from "../api";
import {formatDate} from "../format";

export function LabVersion(){
 const [data,setData]=useState<Version|null>(null),[unavailable,setUnavailable]=useState(false);
 useEffect(()=>{
  let alive=true,request=0;
  async function load(){const id=++request;try{const value=await labVersion();if(alive&&id===request){setData(value);setUnavailable(false);}}
   catch(err){if(alive&&id===request&&!(err instanceof ApiError&&err.status===404))setUnavailable(true);}}
  void load();const timer=window.setInterval(()=>void load(),5000);
  return()=>{alive=false;++request;window.clearInterval(timer);};
 },[]);
 if(!data?.commit)return null;
 const message=unavailable?"Sem atualização recente da versão. Confira a central; isso não comprova falha do backup.":
  (data?.state==="failed"||data?.state==="needs-recovery")?"Atualização não concluída. O administrador deve conferir a etapa e usar a recuperação guiada.":
  data?.phase==="reconcile"?"Recuperação aguarda conferência dos pontos e nova prova de B. Manutenção bloqueada.":
  data?.phase==="drain"?"Atualização: aguardando os trabalhos em andamento. Novas solicitações estão suspensas.":
  data?.phase==="freeze"?"Atualização em andamento. Resultados devem permanecer na fila local até a central voltar.":
  "Versão de laboratório conferida.";
 return <section className="page" aria-label="Versão e atualização do laboratório">
  <p role="status">{message}</p>
  {(data?.inventory_warning??0)>0?<p role="alert">{data?.inventory_warning} pontos históricos ausentes em A. Confira as cópias em B; restaurar o banco não recria snapshots.</p>:null}
  {data?.commit?<details><summary>Versão e situação da atualização</summary>
   <p>Central <code>{data.commit}</code> · PostgreSQL: esquema {data.schema}</p>
   <p>Windows compatível <code>{data.windows}</code></p>
   <p>Conferência: {formatDate(data.updated_at)}. Estado: {data.state}.</p>
   {data.error?<p role="alert">Falha registrada. Use o roteiro do administrador para conferir e recuperar.</p>:null}
  </details>:null}
 </section>;
}
