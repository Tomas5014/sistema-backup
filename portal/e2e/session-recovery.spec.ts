/** Chrome/API/PostgreSQL reais; sem mocks de sessão/cookies. HTTP só loopback
 * explícito da infraestrutura E2E. Segurança Secure também coberta na API HTTPS. */
import {expect,test} from '@playwright/test';
test.skip(!process.env.MACIP_API_TEST_DATABASE_URL,'exige PostgreSQL descartável');
const API='http://127.0.0.1:8180';
const password='synthetic-session-test-only';
async function createOperator(role:'admin'|'viewer'){
 const login='csrf-'+role+'-'+crypto.randomUUID();
 const r=await fetch(API+'/api/v1/operators',{method:'POST',headers:{Authorization:'Bearer test-admin-e2e','Content-Type':'application/json'},body:JSON.stringify({login,name:login,role,password})});
 expect(r.status).toBe(201);return login;
}
for(const role of ['admin','viewer'] as const){
 test(`sessão ${role} restaurada sem CSRF mantém permissão e não duplica escrita`,async({page,context})=>{
  const login=await createOperator(role);
  await page.goto('/login');await page.getByLabel('Login',{exact:true}).fill(login);await page.getByLabel('Senha',{exact:true}).fill(password);await page.getByRole('button',{name:'Entrar',exact:true}).click();
  await expect(page.getByRole('link',{name:'Clientes',exact:true})).toBeVisible();
  const before=await context.cookies();expect(before.find(c=>c.name==='macip_session')?.httpOnly).toBe(true);
  const session=before.find(c=>c.name==='macip_session')?.value;
  await context.clearCookies({name:'macip_csrf'});await page.reload();
  await expect(page.getByRole('link',{name:'Clientes',exact:true})).toBeVisible();
  const after=await context.cookies();expect(after.find(c=>c.name==='macip_csrf')?.value).toBeTruthy();
  expect(after.find(c=>c.name==='macip_session')?.value).toBe(session);
  // API mantém recusa sem cabeçalho mesmo após restaurar o cookie.
  expect(await page.evaluate(async()=> (await fetch('/api/v1/clients',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'must-not-create'})})).status)).toBe(403);
  await page.getByRole('link',{name:'Clientes',exact:true}).click();
  if(role==='viewer'){await expect(page.getByRole('button',{name:'Novo cliente',exact:true})).toHaveCount(0);return;}
  await page.getByRole('button',{name:'Novo cliente',exact:true}).click();
  const name='Fictício CSRF '+crypto.randomUUID();
  async function fill(){await page.getByLabel('Nome do cliente',{exact:true}).fill(name);await page.getByLabel('Identificador da instalação',{exact:true}).fill('pc-csrf-test');await page.getByLabel('Máquina',{exact:true}).fill('WINDOWS-FICTICIO');}
  await fill();
  let writes=0;page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname==='/api/v1/clients/prepare')writes++;});
  // Cookie some durante confirmação: nenhum POST sai, não há repetição automática.
  await context.clearCookies({name:'macip_csrf'});await page.getByRole('button',{name:'Preparar instalação',exact:true}).click();
  await page.getByRole('button',{name:'Verificar sessão',exact:true}).click();
  await expect(page.getByRole('button',{name:'Novo cliente',exact:true})).toBeVisible();expect(writes).toBe(0);
  await page.getByRole('button',{name:'Novo cliente',exact:true}).click();await fill();
  await page.getByRole('button',{name:'Preparar instalação',exact:true}).click();await expect(page.getByText(/Preparação registrada para/)).toBeVisible();expect(writes).toBe(1);
  await context.clearCookies({name:'macip_session'});await page.reload();await expect(page.getByRole('button',{name:'Entrar',exact:true})).toBeVisible();
 });
}
