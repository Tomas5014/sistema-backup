// Acompanhamento e reconhecimento reais no painel do laboratório descartável.
const {chromium,expect}=require('@playwright/test');
async function main(){
 const [origin,client]=process.argv.slice(2);
 const browser=await chromium.launch({channel:'chrome'});
 try{
  const page=await browser.newPage({ignoreHTTPSErrors:true});
  await page.goto(origin+'/login');
  await page.getByLabel('Login',{exact:true}).fill(process.env.MACIP_TEST_BROWSER_LOGIN);
  await page.getByLabel('Senha',{exact:true}).fill(process.env.MACIP_TEST_BROWSER_PASSWORD);
  await page.getByRole('button',{name:'Entrar',exact:true}).click();
  await expect(page.getByRole('link',{name:'Clientes',exact:true})).toBeVisible();
  await page.goto(origin+'/clients/'+client);
  await expect(page.getByText('Cópia protegida atrasada',{exact:true})).toHaveCount(3);
  await expect(page.getByText(/Executor B: Sem atualização recente/)).toBeVisible();
  await expect(page.getByText('Último backup',{exact:true})).toBeVisible();
  await expect(page.getByText('Última cópia protegida',{exact:true})).toBeVisible();
  for(let n=0;n<3;n++){
   await page.getByRole('button',{name:'Reconhecer recebimento',exact:true}).first().click();
   await expect(page.getByText(/Reconhecido em/)).toHaveCount(n+1);
  }
  console.log('Três alertas e reconhecimento por sessão/CSRF no painel real aprovados.');
 }finally{await browser.close();}
}
main().catch(()=>{console.error('Acompanhamento visual real falhou.');process.exitCode=1;});
