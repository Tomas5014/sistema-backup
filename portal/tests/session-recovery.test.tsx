import {afterEach,expect,it,vi} from 'vitest';
import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {MemoryRouter,Routes,Route} from 'react-router-dom';
import {AuthProvider,useAuth} from '../src/auth';
import {Layout} from '../src/components/Layout';
import {LoginPage} from '../src/pages/LoginPage';
import {AssistedOperations} from '../src/components/AssistedOperations';
import {clearCookies,json,noContent,OPERATOR,stubFetch} from './helpers';
const prefix='/api/v1/installations/lab-inst/assisted';
const summary={lifecycle:'active',device_epoch:1,access_state:'none',current_device:null,operations:[]};
function Page(){const {operator}=useAuth();return <AssistedOperations id="lab-inst" admin={operator?.role==='admin'}/>;}
function mount(){return render(<MemoryRouter initialEntries={['/lab']}><AuthProvider><Routes><Route path="/login" element={<LoginPage/>}/><Route element={<Layout/>}><Route path="/lab" element={<Page/>}/></Route></Routes></AuthProvider></MemoryRouter>);}
async function choose(){fireEvent.click(await screen.findByRole('button',{name:'Recuperar usando B'}));fireEvent.click(screen.getByRole('checkbox'));}
afterEach(()=>{vi.unstubAllGlobals();clearCookies();});
it('sessão restaurada recebe CSRF na consulta autenticada e exige confirmação',async()=>{
 const calls=stubFetch({'GET /api/v1/auth/me':()=>{document.cookie='macip_csrf=restored; Path=/';return json(OPERATOR);},['GET '+prefix]:()=>json(summary),['POST '+prefix]:()=>json({id:'one'},202)});
 mount();await choose();expect(calls.filter(c=>c.key==='POST '+prefix)).toHaveLength(0);
 fireEvent.click(screen.getByRole('button',{name:'Confirmar atendimento'}));
 await waitFor(()=>expect(calls.filter(c=>c.key==='POST '+prefix)).toHaveLength(1));
 expect(calls.find(c=>c.key==='POST '+prefix)?.init?.headers).toMatchObject({'X-CSRF-Token':'restored'});
});
it('CSRF perdido não envia operação; recuperação explícita recarrega geração e descarta confirmação antiga',async()=>{
 document.cookie='macip_csrf=first; Path=/';let epoch=1,reads=0;
 const calls=stubFetch({'GET /api/v1/auth/me':()=>{reads++;document.cookie='macip_csrf=recovered; Path=/';return json(OPERATOR);},['GET '+prefix]:()=>json({...summary,device_epoch:epoch}),['POST '+prefix]:()=>json({id:'one'},202)});
 mount();await choose();clearCookies();fireEvent.click(screen.getByRole('button',{name:'Confirmar atendimento'}));
 await screen.findByRole('button',{name:'Verificar sessão'});expect(calls.filter(c=>c.key==='POST '+prefix)).toHaveLength(0);
 const before=reads;epoch=2;fireEvent.click(screen.getByRole('button',{name:'Verificar sessão'}));
 await screen.findByRole('button',{name:'Recuperar usando B'});expect(reads).toBe(before+1);
 expect(screen.queryByRole('button',{name:'Confirmar atendimento'})).toBeNull();expect(calls.filter(c=>c.key==='POST '+prefix)).toHaveLength(0);
 await choose();fireEvent.click(screen.getByRole('button',{name:'Confirmar atendimento'}));
 await waitFor(()=>expect(calls.filter(c=>c.key==='POST '+prefix)).toHaveLength(1));
 expect(JSON.parse(String(calls.find(c=>c.key==='POST '+prefix)?.init?.body))).toMatchObject({expected_epoch:2,confirmed:true});
});
it('CSRF recusado pela API não repete POST nem vira falta de permissão',async()=>{
 document.cookie='macip_csrf=old; Path=/';
 const calls=stubFetch({'GET /api/v1/auth/me':()=>{document.cookie='macip_csrf=new; Path=/';return json(OPERATOR);},['GET '+prefix]:()=>json(summary),['POST '+prefix]:()=>new Response(JSON.stringify({detail:'proteção CSRF'}),{status:403,headers:{'X-MACIP-Session-Protection':'required'}})});
 mount();await choose();fireEvent.click(screen.getByRole('button',{name:'Confirmar atendimento'}));await screen.findByRole('button',{name:'Verificar sessão'});
 expect(calls.filter(c=>c.key==='POST '+prefix)).toHaveLength(1);
 fireEvent.click(screen.getByRole('button',{name:'Verificar sessão'}));await screen.findByRole('button',{name:'Recuperar usando B'});
 expect(calls.filter(c=>c.key==='POST '+prefix)).toHaveLength(1);expect(screen.queryByRole('button',{name:'Confirmar atendimento'})).toBeNull();
});
it('sessão expirada pede login e revalida instalação antes de nova confirmação',async()=>{
 document.cookie='macip_csrf=first; Path=/';let expired=false;
 const calls=stubFetch({'GET /api/v1/auth/me':()=>expired?json({detail:'expirada'},401):json(OPERATOR),'GET /api/v1/auth/csrf':()=>noContent(),'POST /api/v1/auth/login':()=>{expired=false;document.cookie='macip_csrf=login; Path=/';return json(OPERATOR);},['GET '+prefix]:()=>json(summary),['POST '+prefix]:()=>json({detail:'expirada'},401)});
 mount();await choose();expired=true;fireEvent.click(screen.getByRole('button',{name:'Confirmar atendimento'}));await screen.findByRole('button',{name:'Entrar'});
 fireEvent.change(screen.getByLabelText('Login'),{target:{value:'ana'}});fireEvent.change(screen.getByLabelText('Senha'),{target:{value:'synthetic-test-only'}});
 fireEvent.click(screen.getByRole('button',{name:'Entrar'}));await screen.findByRole('button',{name:'Recuperar usando B'});
 expect(screen.queryByRole('button',{name:'Confirmar atendimento'})).toBeNull();expect(calls.filter(c=>c.key==='POST '+prefix)).toHaveLength(1);expect(calls.filter(c=>c.key==='POST /api/v1/auth/login')).toHaveLength(1);
});
it('permissão negada permanece distinta e não renova a sessão',async()=>{
 document.cookie='macip_csrf=first; Path=/';
 const calls=stubFetch({'GET /api/v1/auth/me':()=>json(OPERATOR),['GET '+prefix]:()=>json(summary),['POST '+prefix]:()=>json({detail:'perfil sem permissão de escrita'},403)});
 mount();await choose();const reads=calls.filter(c=>c.key==='GET /api/v1/auth/me').length;
 fireEvent.click(screen.getByRole('button',{name:'Confirmar atendimento'}));await screen.findByText('perfil sem permissão de escrita');
 expect(screen.queryByRole('button',{name:'Verificar sessão'})).toBeNull();expect(calls.filter(c=>c.key==='GET /api/v1/auth/me')).toHaveLength(reads);expect(calls.filter(c=>c.key==='POST '+prefix)).toHaveLength(1);
});
