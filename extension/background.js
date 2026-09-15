/* MV3后台持有连接和任务，弹窗只负责首次配对。
 * Chrome 120+：20秒WS心跳保活；alarms在后台意外停止后唤醒重连。
 * 不使用cookies、webRequest、debugger或页面MAIN world，不读任何站点存储。
 */
const BASE='http://127.0.0.1:8010', VERSION='2.4.4';
let socket=null, heartbeat=null, connecting=false, active=null, statusText='尚未配对';
let serial=Promise.resolve();const cancelled=new Set();
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const offerOf=url=>/^https:\/\/detail\.1688\.com\/offer\/(\d+)\.html(?:[?#]|$)/.exec(url||'')?.[1];
function send(data){if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify(data));}
function progress(task,stage,error_type){if(!task.cancelled)send({type:'progress',task_id:task.task_id,lease:task.lease,stage,error_type});}
async function stopCapture(task){
  task.cancelled=true;
  if(task.tabId)try{await chrome.scripting.executeScript({target:{tabId:task.tabId},func:()=>{window.__pintanCaptureCancel=true;}});}catch{/* 页面已关闭无需处理 */}
}
async function collect(command){
  if(cancelled.has(command.task_id))return;
  const task={...command,cancelled:false};active=task;
  let createdHere=false,submitted=false;
  try {
    if(offerOf(task.page_url)!==task.offer_id)throw Error('page_changed');
    const tabs=await chrome.tabs.query({url:'https://detail.1688.com/offer/*'});
    let tab=tabs.find(t=>offerOf(t.url)===task.offer_id);
    if(!tab){tab=await chrome.tabs.create({url:task.page_url,active:true});createdHere=true;}
    task.tabId=tab.id;statusText='正在打开商品 '+task.offer_id;
    const deadline=Date.now()+(task.mode==='text_only'?30000:60000);
    while(Date.now()<deadline){
      if(task.cancelled)return;
      tab=await chrome.tabs.get(task.tabId);
      if(tab.status==='complete')break;
      await sleep(400);
    }
    if(tab.status!=='complete')throw Error('page_timeout');
    if(offerOf(tab.url)!==task.offer_id)throw Error('login_required');
    await sleep(task.mode==='text_only'?700:1500);if(task.cancelled)return;
    progress(task,'reading');statusText='正在读取商品 '+task.offer_id+'，弹窗可以关闭';
    await chrome.scripting.executeScript({target:{tabId:task.tabId},files:['capture.js']});
    const results=await chrome.scripting.executeScript({target:{tabId:task.tabId},func:mode=>window.pintanCapture(mode),args:[task.mode||'full']});
    if(task.cancelled)return;
    const result=results[0]?.result;
    if(!result)throw Error('unrecognized_page');
    if(result.error_type)throw Error(result.error_type);
    if(offerOf(result.page_url)!==task.offer_id)throw Error('page_changed');
    send({type:'snapshot',task_id:task.task_id,lease:task.lease,snapshot:result});
    submitted=true;
    statusText='页面证据已提交，工作台将继续下载图片';
  }catch(error){
    const allowed=['login_required','verification_required','unrecognized_page','page_timeout','page_changed','capture_timeout'];
    const kind=allowed.includes(error.message)?error.message:'page_changed';
    progress(task,'waiting_user',kind);
    statusText='需要人工检查商品页，请在工作台查看原因并继续';
  }finally{
    // 只自动关闭批量预筛中新建的标签；用户原有标签和等待人工处理页面均保留。
    if(createdHere&&submitted&&task.mode==='text_only'&&task.tabId)try{await chrome.tabs.remove(task.tabId);}catch{/* 已关闭 */}
    if(active===task)active=null;
  }
}
async function search(command){
  if(cancelled.has(command.batch_id))return;
  const task={...command,task_id:command.batch_id,cancelled:false};active=task;
  const report=(stage,error_type)=>send({type:'search_progress',batch_id:task.batch_id,lease:task.lease,stage,error_type});
  let createdHere=false,submitted=false;
  try{
    const expected=new URL(task.page_url);
    if(expected.protocol!=='https:'||expected.hostname!=='s.1688.com')throw Error('page_changed');
    let tabs=await chrome.tabs.query({url:'https://s.1688.com/*'}),tab=tabs.find(t=>t.url?.includes('offer_search'));
    if(!tab){tab=await chrome.tabs.create({url:task.page_url,active:true});createdHere=true;}else await chrome.tabs.update(tab.id,{url:task.page_url,active:true});
    task.tabId=tab.id;report('opening_search');statusText='正在打开1688搜索：'+task.keyword;
    const deadline=Date.now()+60000;
    while(Date.now()<deadline){if(task.cancelled)return;tab=await chrome.tabs.get(task.tabId);if(tab.status==='complete')break;await sleep(400);}
    if(tab.status!=='complete')throw Error('page_timeout');
    if(!tab.url?.startsWith('https://s.1688.com/'))throw Error('login_required');
    await sleep(1600);report('reading_search');statusText='正在读取搜索结果，可关闭弹窗';
    await chrome.scripting.executeScript({target:{tabId:task.tabId},files:['search_capture.js']});
    const results=await chrome.scripting.executeScript({target:{tabId:task.tabId},func:(keyword,maxCandidates)=>window.pintanSearchCapture(keyword,maxCandidates),args:[task.keyword,task.max_candidates]});
    const result=results[0]?.result;if(!result)throw Error('unrecognized_page');if(result.error_type)throw Error(result.error_type);
    send({type:'search_snapshot',batch_id:task.batch_id,lease:task.lease,snapshot:result});submitted=true;statusText='搜索候选已提交，正在排队读取商品详情';
  }catch(error){
    const allowed=['login_required','verification_required','unrecognized_page','page_timeout','page_changed','capture_timeout'];
    report('waiting_user',allowed.includes(error.message)?error.message:'page_changed');statusText='搜索页需要人工处理，请在工作台继续';
  }finally{
    if(createdHere&&submitted&&task.tabId)try{await chrome.tabs.remove(task.tabId);}catch{/* 已关闭 */}
    if(active===task)active=null;
  }
}
async function connect(){
  if(connecting||socket?.readyState===WebSocket.OPEN||socket?.readyState===WebSocket.CONNECTING)return;
  connecting=true;
  try {
    const {pintanPair}=await chrome.storage.local.get('pintanPair');
    if(!pintanPair){statusText='尚未配对，请从工作台获取配对码';return;}
    const ws=new WebSocket('ws://127.0.0.1:8010/api/collector/bridge/ws');socket=ws;
    ws.onopen=()=>{
      send({type:'hello',helper_id:pintanPair.helper_id,token:pintanPair.token,protocol:1,version:VERSION});
      clearInterval(heartbeat);heartbeat=setInterval(()=>send({type:'heartbeat'}),20000);
    };
    ws.onmessage=event=>{
      let data;try{data=JSON.parse(event.data);}catch{return;}
      if(data.type==='hello_ack')statusText='已连接工作台，可以关闭弹窗';
      if(data.type==='collect'){
        cancelled.delete(data.task_id);
        serial=serial.then(()=>socket===ws?collect(data):null).catch(()=>{statusText='采集后台异常，请重连';});
      }
      if(data.type==='search'){
        cancelled.delete(data.batch_id);
        serial=serial.then(()=>socket===ws?search(data):null).catch(()=>{statusText='批量搜索后台异常，请重连';});
      }
      if(data.type==='cancel'){cancelled.add(data.task_id);if(active?.task_id===data.task_id)void stopCapture(active);}
      if(data.type==='rejected')statusText=data.message;
    };
    ws.onclose=()=>{
      if(socket!==ws)return;
      socket=null;clearInterval(heartbeat);
      if(active)void stopCapture(active);
      statusText='连接已断开；将自动重连，已中断任务需在工作台重试';
      setTimeout(()=>void connect(),3000);
    };
    ws.onerror=()=>{statusText='本机服务不可达，请启动8010服务';};
  }finally{connecting=false;}
}
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(sender.id!==chrome.runtime.id)return;
  (async()=>{
    if(message.type==='pair') {
      if(!/^[A-F0-9]{8}$/.test(message.code||''))throw Error('请输入工作台生成的8位配对码');
      const response=await fetch(BASE+'/api/collector/bridge/pair',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({code:message.code,name:'Chrome/Edge采集助手',version:VERSION})});
      const data=await response.json();if(!response.ok)throw Error(data.detail||'配对失败');
      await chrome.storage.local.setAccessLevel({accessLevel:'TRUSTED_CONTEXTS'});
      await chrome.storage.local.set({pintanPair:data});
      if(socket)socket.close();socket=null;await connect();statusText='配对成功，正在连接…';
    }
    if(message.type==='connect')await connect();
    reply({message:statusText});
  })().catch(error=>reply({message:message.type==='pair'?String(error.message):'连接失败，请检查本机服务'}));
  return true;
});
chrome.alarms.create('pintan-reconnect',{periodInMinutes:.5});
chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name==='pintan-reconnect')void connect();});
chrome.runtime.onStartup.addListener(()=>void connect());
chrome.runtime.onInstalled.addListener(()=>void connect());
void connect();
