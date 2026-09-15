/* MV3后台协议单测：Chrome API替身，不冒充真实浏览器/1688验收。 */
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../extension/background.js'),'utf8');
const tick=()=>new Promise(resolve=>setTimeout(resolve,5));
async function setup({existing=true,captureError=null}={}){
  const writes=[],created=[],removed=[],queries=[],intervals=[],scripts=[],sockets=[];
  class Socket{
    static OPEN=1;static CONNECTING=0;
    constructor(url){this.url=url;this.readyState=0;sockets.push(this);}
    send(raw){writes.push(JSON.parse(raw));}
    close(){this.readyState=3;this.onclose?.();}
  }
  const event=()=>({addListener:()=>{}});
  const chrome={
    storage:{local:{get:async()=>({pintanPair:{helper_id:'helper',token:'test-only-token'}})}},
    alarms:{create:()=>{},onAlarm:event()},
    runtime:{onMessage:event(),onStartup:event(),onInstalled:event(),id:'test'},
    tabs:{query:async q=>{queries.push(q);return existing?[{id:7,url:'https://detail.1688.com/offer/736208388272.html?x=1'}]:[];},
      create:async q=>{created.push(q);return {id:8,url:q.url,status:'complete'};},
      remove:async id=>{removed.push(id);},
      get:async()=>({status:'complete',url:'https://detail.1688.com/offer/736208388272.html'})},
    scripting:{executeScript:async options=>{
      scripts.push(options);
      return [{result:options.files?null:captureError?{error_type:captureError}:{page_url:'https://detail.1688.com/offer/736208388272.html',title:'fixture',visible_text:'fixture',images:[],skus:[],regions:[],diagnostics:[]}}];
    }}
  };
  vm.runInNewContext(source,{chrome,WebSocket:Socket,setInterval:fn=>{intervals.push(fn);return 1;},clearInterval:()=>{},
    setTimeout:(fn,ms)=>{if(ms<3000)queueMicrotask(fn);return 1;},URL,Promise,Error,console});
  await tick();const ws=sockets[0];ws.readyState=1;ws.onopen();ws.onmessage({data:JSON.stringify({type:'hello_ack'})});
  const run=async overrides=>{ws.onmessage({data:JSON.stringify({type:'collect',task_id:'CT-test',lease:'lease',page_url:'https://detail.1688.com/offer/736208388272.html',offer_id:'736208388272',...overrides})});await tick();};
  return {run,writes,created,removed,queries,intervals,scripts,ws};
}
test('后台收到URL任务即可读取与提交；不需要弹窗参与',async()=>{
  const t=await setup();await t.run();
  assert.equal(t.created.length,0);assert.equal(t.scripts[0].target.tabId,7);
  const payload=t.writes.find(x=>x.type==='snapshot');assert.ok(payload);
  assert.equal(payload.task_id,'CT-test');assert.equal(payload.snapshot.title,'fixture');
  assert.equal(JSON.stringify(payload).includes('test-only-token'),false);
});
test('没有相同商品页就新开标签，不覆盖现有页面',async()=>{
  const t=await setup({existing:false});await t.run();
  assert.equal(t.created.length,1);assert.equal(t.scripts[0].target.tabId,8);
});
test('批量文字采集完成后仅关闭助手新建的详情页',async()=>{
  const fresh=await setup({existing:false});await fresh.run({mode:'text_only'});
  assert.deepEqual(fresh.removed,[8]);
  const reused=await setup({existing:true});await reused.run({mode:'text_only'});
  assert.deepEqual(reused.removed,[]);
});
test('等待人工处理时保留助手新建的详情页',async()=>{
  const t=await setup({existing:false,captureError:'verification_required'});await t.run({mode:'text_only'});
  assert.deepEqual(t.removed,[]);
});
test('登录与验证码进入等待人工处理，不提交空快照',async()=>{
  const t=await setup({captureError:'verification_required'});await t.run();
  assert.equal(t.writes.some(x=>x.type==='snapshot'),false);
  assert.equal(t.writes.at(-1).stage,'waiting_user');assert.equal(t.writes.at(-1).error_type,'verification_required');
});
test('无效商品任务不打开网页',async()=>{
  const t=await setup();await t.run({page_url:'https://evil.example/offer/736208388272.html'});
  assert.equal(t.queries.length,0);assert.equal(t.scripts.length,0);
});
test('后台心跳不包含登录凭据',async()=>{
  const t=await setup();t.intervals[0]();assert.deepEqual(t.writes.at(-1),{type:'heartbeat'});
});
test('manifest限定后台、站点及所需权限，弹窗不直接采集',()=>{
  const manifest=JSON.parse(fs.readFileSync(require('node:path').join(__dirname,'../extension/manifest.json'),'utf8'));
  assert.equal(manifest.background.service_worker,'background.js');
  assert.deepEqual(manifest.permissions,['scripting','storage','alarms']);
  assert.deepEqual(manifest.host_permissions,['https://detail.1688.com/*','https://s.1688.com/*','http://127.0.0.1:8010/*']);
  const popup=fs.readFileSync(require('node:path').join(__dirname,'../extension/popup.js'),'utf8');
  assert.equal(popup.includes('executeScript'),false);
});
test('扩展清单、后台连接和采集脚本版本保持一致',()=>{
  const manifest=JSON.parse(fs.readFileSync(require('node:path').join(__dirname,'../extension/manifest.json'),'utf8'));
  const backgroundVersion=source.match(/VERSION='([^']+)'/)?.[1];
  const capture=fs.readFileSync(require('node:path').join(__dirname,'../extension/capture.js'),'utf8');
  const captureVersion=capture.match(/CAPTURE_VERSION='([^']+)'/)?.[1];
  assert.equal(backgroundVersion,manifest.version);
  assert.equal(captureVersion,manifest.version);
  assert.ok(fs.existsSync(require('node:path').join(__dirname,'../extension/search_capture.js')));
});
test('搜索采集覆盖Shadow DOM、商品属性和内嵌offerId',()=>{
  const search=fs.readFileSync(require('node:path').join(__dirname,'../extension/search_capture.js'),'utf8');
  assert.ok(search.includes('element.shadowRoot'));
  assert.ok(search.includes('[data-offer-id]'));
  assert.ok(search.includes('embedded-state'));
  assert.ok(search.includes('login_required'));
});
