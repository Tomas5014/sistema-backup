// Navegador real, API simulada explicitamente. Nenhuma credencial operacional.
const { chromium, expect } = require('@playwright/test');
const { createServer } = require('node:http');
const { readFile } = require('node:fs/promises');
const { resolve, join } = require('node:path');
const { once } = require('node:events');
async function main() {
  const root=resolve(__dirname,'../dist');
  const server=createServer(async(req,res)=>{
    const path=new URL(req.url,'http://localhost').pathname;
    try {
      const file=path.startsWith('/assets/') ? join(root,path) : join(root,'index.html');
      res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');
      res.end(await readFile(file));
    } catch { res.statusCode=404; res.end(); }
  });
  server.listen(0,'127.0.0.1'); await once(server,'listening');
  const browser=await chromium.launch({channel:'chrome'});
  try {
    const page=await browser.newPage();
    let ack=false;
    await page.context().addCookies([{name:'macip_csrf',value:'synthetic-csrf',url:`http://127.0.0.1:${server.address().port}`}]);
    const stamp='2026-11-18T12:00:00Z';
    await page.route('**/api/**', async route=>{
      const path=new URL(route.request().url()).pathname;
      let body;
      if(path==='/api/v1/auth/me') body={id:'op',name:'Operador fictício',login:'lab',role:'admin',active:true};
      else if(path==='/api/v1/clients') body=[{id:'client-a',name:'Cliente fictício A',created_at:stamp}];
      else if(path.endsWith('/agents')) body=[];
      else if(path.endsWith('/preparations')) body=[{client_id:'client-a',installation:{id:'agent-a',identifier:'pc-lab',machine_name:'Windows fictício',platform:'windows',suggested_time:'10:00'},state:'primary_ready',step:'verified',protection_state:'proven',last_protected_copy:null,profile_available:false,created_at:stamp,updated_at:stamp,operation:{id:'operation',kind:'prepare_primary',attempt:1,state:'succeeded',step:'verified',created_at:stamp}}];
      else if(path.endsWith('/ack')) { ack=true; body={acknowledged:true}; }
      else if(path.endsWith('/alerts')) body={configured:true,paused:false,last_backup:{status:'partial',finished_at:stamp,snapshot_id:null},last_protected_copy:{confirmed_at:stamp,snapshot_a:'a'.repeat(64),snapshot_b:'b'.repeat(64)},next_deadline:stamp,alerts:['backup_failed','backup_missing','copy_late'].map((kind,i)=>({id:'alert-'+i,kind,detail:{result_code:i===0?'incomplete':'prazo encerrado'},created_at:stamp,resolved_at:null,acknowledged_at:ack&&i===0?stamp:null,delivery_state:'sent'}))};
      else return route.fulfill({status:404,contentType:'application/json',body:'{}'});
      await route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/clients/client-a`);
    for(const label of ['Último backup','Última cópia protegida','Próximo prazo','Backup falhou','Backup não confirmado no prazo','Cópia protegida atrasada']) await expect(page.getByText(label,{exact:true})).toBeVisible();
    await page.getByRole('button',{name:'Reconhecer recebimento'}).first().click();
    await expect(page.getByText(/Reconhecido em/)).toBeVisible();
    for(const [name,width,height] of [['desktop',1200,900],['mobile',390,844]]) {
      await page.setViewportSize({width,height});
      if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)) throw Error('overflow horizontal');
      if(process.env.MACIP_TEST_ALERTS_SCREENSHOTS) await page.screenshot({path:join(process.env.MACIP_TEST_ALERTS_SCREENSHOTS,name+'.png'),fullPage:true});
    }
    console.log('Chrome: três alertas, reconhecimento e 1200/390px aprovados; API simulada.');
  } finally { await browser.close(); server.close(); }
}
main().catch(err=>{console.error(err);process.exitCode=1;});
