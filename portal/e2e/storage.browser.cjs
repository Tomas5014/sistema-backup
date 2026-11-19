// Painel real de retenção, somente com área descartável fornecida pelo teste.
const {chromium,expect}=require('@playwright/test');
const {join}=require('node:path');
let phase="launch";
async function main(){
 const [origin,client]=process.argv.slice(2);
 const browser=await chromium.launch({channel:'chrome'});
 try{
  const page=await browser.newPage({ignoreHTTPSErrors:true,viewport:{width:1200,height:900}});
  phase='login';await page.goto(origin+'/login');
  await page.getByLabel('Login',{exact:true}).fill(process.env.MACIP_TEST_BROWSER_LOGIN);
  await page.getByLabel('Senha',{exact:true}).fill(process.env.MACIP_TEST_BROWSER_PASSWORD);
  await page.getByRole('button',{name:'Entrar',exact:true}).click();
  await expect(page.getByRole('link',{name:'Clientes',exact:true})).toBeVisible();
  phase='capacity';await page.goto(origin+'/clients/'+client);
  const section=page.getByRole('region',{name:'Capacidade e retenção',exact:true});
  await expect(section.getByText(/Exclusão automática desativada/)).toBeVisible();
  await expect(section.getByText(/Espaço disponível atingiu o limite configurado/)).toHaveCount(2,{timeout:60000});
  phase='request';const queued=page.waitForResponse(r=>r.url().endsWith('/storage/operations')&&r.request().method()==='POST');
  await section.getByRole('button',{name:'Simular retenção A',exact:true}).click();
  const response=await queued;const operation=await response.json();
  const summaryUrl=response.url().slice(0,-'/operations'.length);
  phase='wait-result';await expect.poll(async()=>{const r=await page.request.get(summaryUrl);const data=await r.json();return data.operations.find(o=>o.id===operation.id)?.state;},{timeout:60000}).toBe('succeeded');
  phase='render-result';await page.reload();
  await expect(section.getByText(/Simulação: Concluída/)).toBeVisible({timeout:60000});
  await expect(section.getByRole('button',{name:'Executar retenção A',exact:true})).toBeDisabled();
  await expect(section.getByText('Candidato à exclusão',{exact:true})).toHaveCount(2);
  phase='layout';for(const [label,width,height] of [['desktop',1200,900],['mobile',390,844]]){
   await page.setViewportSize({width,height});
   if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('overflow');
   if(process.env.MACIP_TEST_STORAGE_SCREENSHOTS)await page.screenshot({path:join(process.env.MACIP_TEST_STORAGE_SCREENSHOTS,label+'.png'),fullPage:true});
  }
  console.log('Painel real: capacidade, candidatos e confirmação obrigatória aprovados.');
 }finally{await browser.close();}
}
main().catch(error=>{console.error('Prova visual de retenção falhou: '+phase+' / '+error.name);process.exitCode=1;});
