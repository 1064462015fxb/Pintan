/* 自动采集入口：只提交URL；用持久任务恢复状态，绝不退回“保存空资料”。 */
(() => {
  const $=id=>document.getElementById(id), view=$('collectorView');if(!view)return;
  const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const active=new Set(['queued','opening','reading','waiting_user','normalizing','downloading']);
  let current='', timer=null, bridgeTimer=null, polling=false, deadline=0, lastRevision='', stopped=false, previousBridge='';
  view.insertAdjacentHTML('afterbegin',`<section class="panel" style="margin-bottom:20px;border-top:4px solid #207965">
    <div class="actions"><h2>粘贴链接，自动采集</h2><span id="bridgeState" class="badge">检查助手连接…</span><button id="bridgeCheck">检查连接</button></div>
    <p class="muted">由已配对的Chrome/Edge读取商品页面。可关闭助手弹窗，登录状态保留在该浏览器。</p>
    <div class="actions"><label style="flex:1">1688商品链接<input id="autoCollectUrl" style="width:100%" placeholder="https://detail.1688.com/offer/736208388272.html"></label><button class="primary" id="autoCollectStart">开始采集</button></div>
    <p id="autoCollectNotice" class="status" role="status"></p>
    <details id="bridgeSetup"><summary>首次安装 / 配对 / 连接说明</summary>
      <ol><li><a href="/api/collector/bridge/extension.zip">下载采集助手ZIP</a>并解压，或使用项目内的extension文件夹。</li>
      <li>Chrome打开 chrome://extensions，Edge打开 edge://extensions；启用开发者模式，加载已解压的扩展。旧版需重新加载。</li>
      <li>点击下方生成配对码，在浏览器工具栏的品探助手中输入。连接成功后可关闭弹窗。</li>
      <li>1688登录或验证需在助手所在的Chrome/Edge中完成。工作台可以继续在当前浏览器打开。</li></ol>
      <button id="bridgePairCode">生成配对码</button> <strong id="bridgeCode"></strong>
      <p class="muted">需要Chrome/Edge 120+；连接地址固定127.0.0.1:8010。配对码5分钟有效，仅使用一次。助手不读取Cookie。</p>
    </details>
    <hr><div class="actions"><label>自动任务 <select id="autoCollectTasks"><option value="">暂无任务</option></select></label><button id="autoCollectRefresh">刷新任务</button></div>
    <div id="autoCollectProgress" aria-live="polite"></div>
    <div class="actions"><button id="autoCollectResume" hidden>继续采集 / 重试</button><button id="autoCollectCancel" hidden>取消任务</button></div>
  </section>`);
  async function api(path,body){
    const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),15000);
    try{
      const response=await fetch(path,{signal:controller.signal,...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
      let data;try{data=await response.json();}catch{throw Error('服务响应无效，已停止查询；请检查服务后手动刷新');}
      if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'请求参数错误');return data;
    }finally{clearTimeout(timeout);}
  }
  function error(e){stopped=true;clearTimeout(timer);$('autoCollectNotice').textContent=e.name==='AbortError'?'请求超时，已停止轮询，请检查服务后刷新任务。':e.message;}
  async function checkBridge(){
    const data=await api('/api/collector/bridge');
    const helper=data.helpers.find(h=>h.connected);
    $('bridgeState').textContent={offline:'助手未连接',connected:'助手已连接',busy:'助手忙碌'}[data.status]+(helper?' · 助手 v'+helper.version:' · 服务 v'+data.version)+(helper&&helper.version!==data.version?'（分区采集需重新加载新版扩展）':'');
    if(data.status==='offline'&&previousBridge!=='offline')$('bridgeSetup').open=true;
    previousBridge=data.status;
  }
  function showTask(task){
    $('autoCollectProgress').innerHTML=`<h3>${esc(task.status_label)} · 商品 ${esc(task.offer_id)}</h3>
      <p>${esc(task.message||'已进入任务队列')}</p><p>本次发现 ${task.found||0} 张 · 本次下载成功 ${task.downloaded||0} · 失败 ${task.failed||0}</p>
      <small>任务 ${esc(task.task_id)} · 更新于 ${esc(task.updated_at)}</small>
      ${task.model_status==='fallback'?'<p class="status">模型整理降级：页面证据已保留，图片下载不受影响。</p>':''}
      ${(task.regions||[]).map(r=>`<p>${esc(r.region)}：${r.recognized?'已识别':'未识别'} / ${r.found} 张 · ${esc(r.reason)}</p>`).join('')}
      ${task.image_results?.length?`<details><summary>逐图下载结果（本次运行）</summary>${task.image_results.map(r=>`<p>${esc(r.asset_id)}：${r.success?'本次下载成功':r.retained_history?'本次下载失败，保留历史文件':'本次下载失败'} · ${esc(r.width||'—')} × ${esc(r.height||'—')}${!r.success?' · '+esc(r.error_type||r.attempts?.filter(a=>a.status==='failed').map(a=>a.error_type).join(' / ')||'请重试'):''}</p>`).join('')}</details>`:''}`;
    $('autoCollectResume').hidden=!['waiting_user','failed','interrupted','cancelled'].includes(task.status);
    $('autoCollectCancel').hidden=!active.has(task.status);
    if(task.run_id&&lastRevision!==task.run_id+'|'+task.updated_at+'|'+task.status){
      lastRevision=task.run_id+'|'+task.updated_at+'|'+task.status;
      window.dispatchEvent(new CustomEvent('pintan:collector-run',{detail:task.run_id}));
    }
  }
  async function poll(){
    if(polling||!current||stopped||location.hash!=='#collector')return;
    clearTimeout(timer);polling=true;const id=current;
    try{
      const task=await api('/api/collector/tasks/'+encodeURIComponent(id));
      if(current!==id)return;showTask(task);
      if(active.has(task.status)&&task.status!=='waiting_user'){
        if(Date.now()>deadline){error(Error('等待超过30分钟，已停止轮询；任务仍保留，可刷新查询或取消。'));return;}
        timer=setTimeout(poll,1500);
      }
    }catch(e){error(e);}finally{polling=false;if(current!==id&&!stopped)timer=setTimeout(poll,0);}
  }
  async function list(){
    const data=await api('/api/collector/tasks');
    $('autoCollectTasks').innerHTML=data.items.map(t=>`<option value="${esc(t.task_id)}">${esc(t.offer_id)} · ${esc(t.status_label)} · ${esc(t.created_at)}</option>`).join('')||'<option value="">暂无任务</option>';
    if(data.items.some(t=>t.task_id===current))$('autoCollectTasks').value=current;
    current=$('autoCollectTasks').value;deadline=Date.now()+1800000;stopped=false;await poll();
  }
  $('autoCollectStart').onclick=async()=>{
    $('autoCollectStart').disabled=true;
    try{
      const task=await api('/api/collector/tasks',{page_url:$('autoCollectUrl').value.trim()});current=task.task_id;
      $('autoCollectNotice').textContent=task.deduplicated?'已定位同商品的活动任务，没有重复启动。':'自动任务已提交，无需填写标题和图片地址。';
      lastRevision='';await list();
    }catch(e){error(e);}finally{$('autoCollectStart').disabled=false;}
  };
  $('autoCollectTasks').onchange=()=>{current=$('autoCollectTasks').value;stopped=false;lastRevision='';deadline=Date.now()+1800000;void poll();};
  for(const [id,action] of [['autoCollectResume','resume'],['autoCollectCancel','cancel']])$(id).onclick=async()=>{
    $(id).disabled=true;try{await api('/api/collector/tasks/'+encodeURIComponent(current)+'/'+action,{});await list();}catch(e){error(e);}finally{$(id).disabled=false;}
  };
  $('bridgePairCode').onclick=async()=>{try{const d=await api('/api/collector/bridge/pair-code',{});$('bridgeCode').textContent=d.code+'（5分钟有效）';}catch(e){error(e);}};
  $('bridgeCheck').onclick=()=>checkBridge().catch(error);
  $('autoCollectRefresh').onclick=()=>list().catch(error);
  async function enter(){
    clearTimeout(timer);clearTimeout(bridgeTimer);
    if(location.hash!=='#collector')return;
    try{await checkBridge();await list();}catch(e){error(e);}
    async function connectionLoop(){
      if(location.hash!=='#collector')return;
      try{await checkBridge();bridgeTimer=setTimeout(connectionLoop,5000);}catch(e){error(e);}
    }
    bridgeTimer=setTimeout(connectionLoop,5000);
  }
  window.addEventListener('hashchange',enter);void enter();
})();
