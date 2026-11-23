const {chromium,expect}=require('@playwright/test');
const fs=require('node:fs');
let stage='start';
async function main(){
 const [origin,client,action,out,snapshot]=process.argv.slice(2);
 const browser=await chromium.launch({channel:'chrome'});
 try{
  const page=await browser.newPage({ignoreHTTPSErrors:true,viewport:{width:1200,height:850},acceptDownloads:true});
  stage='login';await page.goto(origin+'/login');await page.getByLabel('Login',{exact:true}).fill(process.env.MACIP_TEST_BROWSER_LOGIN);
  await page.getByLabel('Senha',{exact:true}).fill(process.env.MACIP_TEST_BROWSER_PASSWORD);
  await page.getByRole('button',{name:'Entrar',exact:true}).click();
  await expect(page.getByRole('link',{name:'Clientes',exact:true})).toBeVisible();
  stage='client';await page.goto(origin+'/clients/'+client);
  const region=page.getByRole('region',{name:'Operação e recuperação assistidas'});
  await expect(region).toBeVisible();
  async function confirm(){
   const response=page.waitForResponse(r=>r.url().endsWith('/assisted')&&r.request().method()==='POST');
   await region.getByRole('checkbox').check();await region.getByRole('button',{name:'Confirmar atendimento',exact:true}).click();
   const r=await response;if(r.status()!==202)throw Error('atendimento recusado');return await r.json();
  }
  let result={};
  stage=action;
  if(action==='replace'){
   await region.getByRole('button',{name:'Substituir computador',exact:true}).click();
   await expect(region.getByText(/Serão revogados códigos/)).toBeVisible();
   result=await confirm();
   await expect(region.getByText(/Aguardando novo computador/)).toBeVisible({timeout:90000});
  }else if(action==='recover'){
   await region.getByRole('button',{name:'Recuperar usando B',exact:true}).click();result.catalog=await confirm();
   const selector=region.getByLabel('Ponto para recuperação');
   await expect(selector.locator('option[value="'+snapshot+'"]')).toBeAttached({timeout:90000});
   await selector.selectOption(snapshot);
   await region.getByRole('button',{name:'Preparar arquivos para recuperação',exact:true}).click();result.restore=await confirm();
   const download=region.getByRole('button',{name:'Baixar arquivos recuperados',exact:true});
   await expect(download).toBeVisible({timeout:120000});
   stage='download';const received=page.waitForEvent('download');await download.click();await (await received).saveAs(out+'.zip');
  }else if(action==='close'){
   await region.getByRole('button',{name:'Encerrar instalação',exact:true}).click();result=await confirm();
   await expect(region.getByText(/Revogação SSH não confirmada/)).toBeVisible({timeout:90000});
  }else{throw Error('ação inválida');}
  stage='responsive';
  if(process.env.MACIP_TEST_ASSISTED_SCREENSHOTS){
   for(const [name,width,height] of [['desktop',1200,850],['mobile',390,844]]){
    await page.setViewportSize({width,height});
    await region.screenshot({path:process.env.MACIP_TEST_ASSISTED_SCREENSHOTS+'/'+action+'-'+name+'.png'});
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('overflow');
   }
  }
  fs.writeFileSync(out,JSON.stringify(result));
 }finally{await browser.close();}
}
main().catch(error=>{console.error('Prova visual de atendimento não concluída: '+stage+' / '+error.constructor.name);process.exitCode=1;});
