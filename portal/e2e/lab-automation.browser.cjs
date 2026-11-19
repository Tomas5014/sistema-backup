// Chrome + central reais, certificados efêmeros do teste. Nenhum dado de cliente real.
const { chromium, expect } = require('@playwright/test');
const { writeFile } = require('node:fs/promises');
const { join } = require('node:path');
async function main(){
  const [origin,name,output]=process.argv.slice(2);
  const browser=await chromium.launch({channel:'chrome'});
  try{
    const page=await browser.newPage({ignoreHTTPSErrors:true,viewport:{width:1200,height:900}});
    // A exceção de CA deste navegador de teste não altera o transporte do aplicativo.
    await page.goto(origin+'/login');
    await page.getByLabel('Login',{exact:true}).fill(process.env.MACIP_TEST_BROWSER_LOGIN);
    await page.getByLabel('Senha',{exact:true}).fill(process.env.MACIP_TEST_BROWSER_PASSWORD);
    await page.getByRole('button',{name:'Entrar',exact:true}).click();
    await page.getByRole('link',{name:'Clientes',exact:true}).click();
    await page.getByRole('button',{name:'Novo cliente',exact:true}).click();
    await page.getByLabel('Nome do cliente',{exact:true}).fill(name);
    await page.getByLabel('Identificador da instalação',{exact:true}).fill('pc-lab');
    await page.getByLabel('Máquina',{exact:true}).fill('Windows fictício automático');
    const prepared=page.waitForResponse(r=>r.url().endsWith('/api/v1/clients/prepare')&&r.request().method()==='POST');
    await page.getByRole('button',{name:'Preparar instalação',exact:true}).click();
    const initial=await (await prepared).json();
    if(initial.profile_available)throw Error('perfil não pode nascer liberado');
    await page.getByRole('link',{name:/Acompanhar preparação/}).click();
    await expect(page.getByRole('button',{name:'Baixar perfil',exact:true})).toBeDisabled();
    await expect(page.getByText('Preparação e cópia automáticas',{exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'Gerar código de ativação',exact:true})).toBeVisible({timeout:180000});
    await expect(page.getByRole('button',{name:'Baixar perfil',exact:true})).toBeEnabled();
    const issued=page.waitForResponse(r=>r.url().endsWith('/api/v1/activation-codes')&&r.request().method()==='POST');
    await page.getByRole('button',{name:'Gerar código de ativação',exact:true}).click();
    const code=await (await issued).json();
    await writeFile(output,JSON.stringify({installation_id:initial.installation.id,client_id:initial.client_id,code}),{mode:0o600,flag:'wx'});
    await page.getByRole('button',{name:'Ocultar código',exact:true}).click();
    if(process.env.MACIP_TEST_AUTOMATION_SCREENSHOTS){
      for(const [label,width,height] of [['desktop',1200,900],['mobile',390,844]]){
        await page.setViewportSize({width,height});
        if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('overflow horizontal');
        await page.screenshot({path:join(process.env.MACIP_TEST_AUTOMATION_SCREENSHOTS,label+'.png'),fullPage:true});
      }
    }
    console.log('Cadastro visual, preparação A/B automática e emissão de código aprovados; segredo fora das evidências.');
  }finally{await browser.close();}
}
main().catch(()=>{console.error('Fluxo visual de laboratório falhou; confira o estado da prova.');process.exitCode=1;});
