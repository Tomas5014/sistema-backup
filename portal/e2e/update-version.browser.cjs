const {chromium,expect}=require('@playwright/test');
const {join}=require('node:path');
async function main(){
 const [origin]=process.argv.slice(2);const browser=await chromium.launch({channel:'chrome'});
 try{
  const page=await browser.newPage({ignoreHTTPSErrors:true,viewport:{width:1200,height:800}});
  await page.goto(origin+'/login');await page.getByLabel('Login',{exact:true}).fill(process.env.MACIP_TEST_BROWSER_LOGIN);
  await page.getByLabel('Senha',{exact:true}).fill(process.env.MACIP_TEST_BROWSER_PASSWORD);
  await page.getByRole('button',{name:'Entrar',exact:true}).click();
  await expect(page.getByRole('link',{name:'Clientes',exact:true})).toBeVisible();
  const version=page.getByRole('region',{name:'Versão e atualização do laboratório'});
  await expect(version).toBeVisible();await version.getByText('Versão e situação da atualização',{exact:true}).click();
  await expect(version.getByText(/esquema 14/)).toBeVisible();await expect(version.getByRole('button')).toHaveCount(0);
  for(const [name,width,height] of [['desktop',1200,800],['mobile',390,844]]){
   await page.setViewportSize({width,height});
   if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('overflow');
   if(process.env.MACIP_TEST_UPDATE_SCREENSHOTS)await page.screenshot({path:join(process.env.MACIP_TEST_UPDATE_SCREENSHOTS,name+'.png'),fullPage:true});
  }
 }finally{await browser.close();}
}
main().catch(()=>{console.error('Prova visual de versão não concluída.');process.exitCode=1;});
