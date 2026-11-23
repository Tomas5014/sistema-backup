import {afterEach,expect,it,vi} from "vitest";
import {fireEvent,render,screen,waitFor,act} from "@testing-library/react";
import {AssistedOperations} from "../src/components/AssistedOperations";
import {json,stubFetch,clearCookies} from "./helpers";
import {executionTaskLabel} from "../src/format";
const base={lifecycle:"active",device_epoch:1,access_state:"none",current_device:null,operations:[]};
const prefix='/api/v1/installations/inst-a/assisted';
afterEach(()=>{vi.unstubAllGlobals();vi.useRealTimers();clearCookies();});
it('viewer consulta sem ações administrativas',async()=>{
 stubFetch({['GET '+prefix]:()=>json(base)});render(<AssistedOperations id="inst-a" admin={false}/>);
 expect(await screen.findByText(/Computador em operação/)).toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'Encerrar instalação'})).not.toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'Recuperar usando B'})).not.toBeInTheDocument();
});
it('confirmação explícita e repetição após resposta perdida preservam identidade',async()=>{
 document.cookie='macip_csrf=assisted-csrf; Path=/';
 let attempts=0;const bodies:Record<string,unknown>[]=[];
 stubFetch({['GET '+prefix]:()=>json(base),['POST '+prefix]:(init)=>{bodies.push(JSON.parse(String(init?.body)));return ++attempts===1?Promise.reject(Error('offline')):json({id:'operation'},202);}});
 render(<AssistedOperations id="inst-a" admin/>);
 fireEvent.click(await screen.findByRole('button',{name:'Substituir computador'}));
 expect(screen.getByText(/Serão revogados códigos/)).toBeInTheDocument();
 expect(screen.getByRole('button',{name:'Confirmar atendimento'})).toBeDisabled();
 fireEvent.click(screen.getByRole('checkbox'));fireEvent.click(screen.getByRole('button',{name:'Confirmar atendimento'}));
 await screen.findByRole('alert');
 fireEvent.click(screen.getByRole('button',{name:'Confirmar atendimento'}));
 await waitFor(()=>expect(bodies).toHaveLength(2));
 expect(bodies[0]).toEqual(bodies[1]);expect(bodies[0]).toMatchObject({expected_epoch:1,action:'replace',confirmed:true});
});
it('revogação pendente ou falha não aparece confirmada',async()=>{
 stubFetch({['GET '+prefix]:()=>json({...base,lifecycle:'closing',access_state:'failed',device_epoch:2,
 operations:[{id:'op',action:'close',state:'failed',error_code:'unavailable',step:'failed',device_epoch:2,attempt:1,created_at:'2026-11-23T10:00:00Z',result:null}]})});
 render(<AssistedOperations id="inst-a" admin/>);
 expect(await screen.findByText(/Revogação SSH não confirmada/)).toBeInTheDocument();
 expect(screen.queryByText(/A confirmou os acessos/)).not.toBeInTheDocument();
 expect(screen.getByRole('button',{name:'Tentar novamente este atendimento'})).toBeInTheDocument();
});
it('catálogo vazio tem resultado distinto de consulta pendente',async()=>{
 stubFetch({['GET '+prefix]:()=>json({...base,operations:[{id:'catalog',action:'catalog',state:'succeeded',result:{points:[]},device_epoch:1,attempt:1,created_at:'2026-11-23T10:00:00Z',finished_at:'2026-11-23T10:00:01Z'}]})});
 render(<AssistedOperations id="inst-a" admin/>);
 expect(await screen.findByText(/Consulta concluída, sem pontos disponíveis/)).toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'Baixar arquivos recuperados'})).not.toBeInTheDocument();
});
it('resultado parcial não libera download e erro é texto',async()=>{
 stubFetch({['GET '+prefix]:()=>json({...base,operations:[{id:'restore',action:'restore',state:'failed',error_code:'interrupted',step:'<img src=x onerror=alert(1)>',result:{files:2},device_epoch:1,attempt:1,created_at:'2026-11-23T10:00:00Z'}]})});
 const page=render(<AssistedOperations id="inst-a" admin/>);
 expect(await screen.findByText(/Operação interrompida e não concluída/)).toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'Baixar arquivos recuperados'})).not.toBeInTheDocument();
 expect(page.container.querySelector('img')).toBeNull();
});
it('resposta antiga não troca o cliente nem seu catálogo',async()=>{
 let resolve!:(r:Response)=>void;
 stubFetch({['GET '+prefix]:()=>new Promise<Response>(r=>{resolve=r;}),['GET /api/v1/installations/inst-b/assisted']:()=>json({...base,lifecycle:'closed',device_epoch:3,access_state:'confirmed'})});
 const page=render(<AssistedOperations id="inst-a" admin/>);
 page.rerender(<AssistedOperations id="inst-b" admin/>);
 await screen.findByText(/Instalação encerrada/);
 await act(async()=>resolve(json({...base,lifecycle:'replacing',access_state:'requested'})));
 expect(screen.queryByText(/Substituição solicitada/)).not.toBeInTheDocument();
 expect(screen.getByText(/Instalação encerrada/)).toBeInTheDocument();
});

it('histórico distingue gerações sem mudar identificadores ou tarefas Linux',()=>{
 const id='11111111-2222-3333-4444-555555555555';
 expect(executionTaskLabel('windows:'+id)).toBe('Backup Windows · geração 1');
 expect(executionTaskLabel('windows:'+id+':device-2')).toBe('Backup Windows · geração 2');
 expect(executionTaskLabel('backup-linux')).toBe('backup-linux');
});
