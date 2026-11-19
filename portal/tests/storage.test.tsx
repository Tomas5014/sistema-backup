import {afterEach,expect,it,vi} from "vitest";
import {act,render,screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {InstallationStorage} from "../src/components/InstallationStorage";
import {json,clearCookies} from "./helpers";

const plan={side:"a",repository_id:"f".repeat(64),fingerprint:"hash",points:[
 {id:"a".repeat(64),time:"2026-11-19T12:00:00Z",action:"keep",reason:"copy_unproven"},
 {id:"b".repeat(64),time:"2026-11-18T12:00:00Z",action:"remove",reason:null}]};
const op={id:"sim-a",side:"a",action:"simulate",state:"succeeded",requester:"operador",created_at:"2026-11-19T12:00:00Z",finished_at:"2026-11-19T12:00:01Z",error:null,result:{plan,removed:[]}};
const summary={automatic:false,policy:{keep_a:1,keep_b:null,low_a:50,low_b:null,pins_a:[],pins_b:[]},capacity:{
 a:{stale:false,low:true,last_good:{total:1000,used:990,free:10,measured_at:"2026-11-19T12:00:00Z",volume:"same-volume",scope:"shared-volume",repository_bytes:5},error:null},
 b:{stale:true,low:false,last_good:null,error:"unavailable"}},operations:[op],audit:[]};
afterEach(()=>{vi.unstubAllGlobals();clearCookies();});
function stub(fn:(u:string,i:RequestInit)=>Response|Promise<Response>){vi.stubGlobal("fetch",vi.fn((u:string,i:RequestInit={})=>fn(String(u),i)));}

it("separa volume e repositório, stale e limite; confirmação explícita envia CSRF",async()=>{
 document.cookie="macip_csrf=storage-csrf; path=/";
 let sent=false;
 stub((url,init)=>{if(url.endsWith("/operations")){const body=JSON.parse(init.body as string);expect(body.side).toBe("a");expect(body.simulation).toBe("sim-a");expect(body.action).toBe("execute");expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("storage-csrf");sent=true;return json(op);}return json(summary);});
 render(<InstallationStorage id="a" admin/>);
 expect(await screen.findByText(/Exclusão automática desativada/)).toBeInTheDocument();
 expect(screen.getByText(/atingiu o limite/)).toBeInTheDocument();
 expect(screen.getByText("Sem medição recente")).toBeInTheDocument();
 expect(screen.getByText(/não devem ser somadas/)).toBeInTheDocument();
 expect(screen.getByText("Manter · Cópia em B não comprovada")).toBeInTheDocument();
 const execute=screen.getByRole("button",{name:"Executar retenção A"});expect(execute).toBeDisabled();
 await userEvent.click(screen.getByRole("checkbox"));await userEvent.click(execute);expect(sent).toBe(true);
});
it("viewer só consulta; erros são texto; resultado antigo não aparece em outra instalação",async()=>{
 let resolve!:(r:Response)=>void;
 stub(u=>u.includes("/a/")?new Promise(r=>{resolve=r;}):json({...summary,operations:[{...op,error:"inventory_changed"}]}));
 const page=render(<InstallationStorage id="a" admin={false}/>);
 page.rerender(<InstallationStorage id="b" admin={false}/>);
 expect(await screen.findByText("Os pontos mudaram. Simule novamente.")).toBeInTheDocument();
 expect(screen.queryByRole("button")).not.toBeInTheDocument();
 await act(async()=>resolve(json({...summary,operations:[{...op,requester:"<script>antiga</script>"}]})));
 expect(screen.queryByText(/antiga/)).not.toBeInTheDocument();
});
it("edita política, preserva vazio e recusa fracionários antes de enviar",async()=>{
 document.cookie="macip_csrf=storage-csrf; path=/";
 let saved:any;
 stub((url,init)=>{if(url.endsWith("/policy")){saved=JSON.parse(init.body as string);return json({});}return json(summary);});
 render(<InstallationStorage id="a" admin/>);
 await userEvent.click(await screen.findByRole("button",{name:"Editar política do laboratório"}));
 const field=screen.getAllByLabelText("Quantidade de pontos (vazio mantém todos)")[0];
 await userEvent.clear(field);await userEvent.type(field,"1.5");
 await userEvent.click(screen.getByRole("button",{name:"Salvar política"}));
 expect(await screen.findByRole("alert")).toHaveTextContent("inteiros positivos");expect(saved).toBeUndefined();
 await userEvent.clear(field);await userEvent.click(screen.getByRole("button",{name:"Salvar política"}));
 expect(saved.keep_a).toBeNull();expect(saved.keep_b).toBeNull();
});
it("não reaproveita confirmação após troca de instalação",async()=>{
 stub(()=>json(summary));const page=render(<InstallationStorage id="a" admin/>);
 await userEvent.click(await screen.findByRole("checkbox"));
 expect(screen.getByRole("button",{name:"Executar retenção A"})).toBeEnabled();
 page.rerender(<InstallationStorage id="b" admin/>);
 expect(await screen.findByRole("button",{name:"Executar retenção A"})).toBeDisabled();
});
