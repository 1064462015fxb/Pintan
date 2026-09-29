/* 网络和轮询函数独立于 DOM，便于验证失败后不会无限请求。 */
async function requestJson(url, options) {
  const controller=new AbortController();
  const {timeoutMs=30000,...settings}=options||{};
  const timer=setTimeout(()=>controller.abort(),timeoutMs);
  try{
  const response = await fetch(url, {...settings,signal:controller.signal});
  let data;
  try { data = await response.json(); } catch { throw new Error('服务返回了无效数据'); }
  if (!response.ok) {
    const detail=data.detail;
    const messages=detail&&typeof detail==='object'&&Array.isArray(detail.blockers)?detail.blockers.map(x=>x.message).filter(Boolean):[];
    const error=new Error(typeof detail==='string'?detail:(messages.join('；')||`请求失败（${response.status}）`));
    error.detail=detail;throw error;
  }
  return data;
  }catch(error){
    if(controller.signal.aborted)throw new Error('服务请求超过等待时间，请检查连接或刷新查看任务；后台任务不一定已停止。');
    throw error;
  }finally{clearTimeout(timer)}
}

async function pollTask(id, onStatus = () => {}, wait = ms => new Promise(resolve => setTimeout(resolve, ms))) {
  if (!id || typeof id !== 'string') throw new Error('服务未返回有效任务编号');
  const deadline = Date.now() + 10 * 60 * 1000;
  while (Date.now() < deadline) {
    let data;
    try{data=await requestJson('/api/tasks/'+encodeURIComponent(id))}catch(error){if(typeof window!=='undefined'&&location.hash==='#content')PintanUI.progress({status:'interrupted',error:error.message});throw error}
    if(typeof window!=='undefined'&&location.hash==='#content'){
      PintanUI.progress(data);
      if(['angle','platform'].includes(data.kind))PintanUI.save('contentTask', ['queued','running'].includes(data.status)?{id,kind:data.kind,product:sessionStorage.getItem('pintan.content.product')}:null);
    }
    onStatus(data.status);
    if (data.status === 'completed') return data.result;
    if (data.status === 'failed' || data.status === 'interrupted') throw new Error(data.error || '任务中断');
    if (!['queued', 'running'].includes(data.status)) throw new Error('未知任务状态，已停止轮询');
    await wait(1000);
  }
  if(typeof window!=='undefined'&&location.hash==='#content')PintanUI.progress({status:'interrupted',error:'等待超时，可刷新查看任务'});
  throw new Error('等待超过10分钟，已停止轮询，请刷新查看状态');
}

// 纯函数不改原始数据或综合排名；缺失值在两个方向都置底。
function filterStable(rows, query = '', field = 'total_score', direction = 'desc', limit = '10') {
  const keys = ['title','stable_id','category','feature_tags'];
  const filtered = rows.filter(r => keys.some(k => String(r[k] || '').toLowerCase().includes(query.trim().toLowerCase())));
  const number = v => v === '' || v == null || !Number.isFinite(Number(v)) ? null : Number(v);
  filtered.sort((a,b) => {
    const x = number(a[field]), y = number(b[field]);
    if (x === null && y !== null) return 1;
    if (y === null && x !== null) return -1;
    const diff = x === null ? 0 : (x-y) * (direction === 'asc' ? 1 : -1);
    return diff || String(a.stable_id).localeCompare(String(b.stable_id));
  });
  return {total:filtered.length, items:limit === 'all' ? filtered : filtered.slice(0,Number(limit))};
}
function routeFor(hash) {
  const route = hash.replace(/^#/, '');
  if(route==='compliance')return 'content';
  if(route==='collector'||route==='selection/batch')return 'collector/batch';
  if(/^distribution\/(product|package)\/[^/]+$/.test(route))return route;
  return ['home','collector/batch','collector/trending','selection/stable','selection/trending','content','distribution'].includes(route) ? route : 'home';
}
if (typeof module !== 'undefined') module.exports = {requestJson, pollTask, filterStable, routeFor};
if (typeof document !== 'undefined') {
  const $ = id => document.getElementById(id);
  const UI=PintanUI;
  let stablePage=UI.read('stablePage',1),candidatePage=UI.read('candidatePage',1),batchPage=UI.read('batchPage',1),trendPage=1;
  const selectedStable=new Set(UI.read('selectedStable',[]));
  let trendData=null;
  const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let selected = '', trendingBusy = false, stableBusy = false, detailReady = false, stableRows = [], candidateRows = [], collectorRun = '', collectorPasteHandler = null, activeMediaTaskId = '';
  let batchAdvice=null,currentBatch='',batchTimer=null,trendAdvice=null,currentTrendBatch='',trendDiscoveryTimer=null;
  function ensureCollectorUI() {
    const nav=document.querySelector('.sidebar nav');
    if (nav && !document.querySelector('[data-module="collector"]')) nav.querySelector('[data-module="selection"]').insertAdjacentHTML('beforebegin','<a href="#collector" data-module="collector">01　数据采集 Agent</a>');
    if (nav) {
      const labels={selection:'02　选品 Agent',content:'03　内容生产 Agent',distribution:'04　铺货 Agent'};
      Object.entries(labels).forEach(([key,label])=>{const link=nav.querySelector(`[data-module="${key}"]`);if(link)link.textContent=label});
    }
    const moduleGrid=document.querySelector('#homeView .module-grid');
    if(moduleGrid && !moduleGrid.querySelector('[href="#collector"]')) moduleGrid.insertAdjacentHTML('afterbegin','<a class="module" href="#collector"><span class="number">01 / 已开放</span><b>数据采集 Agent →</b><span class="muted">1688商品事实、SKU与必要图片</span></a>');
    const main=document.querySelector('main');
    if (main && !$('collectorView')) main.insertAdjacentHTML('beforeend',`<section id="collectorView" hidden><h1>1688 数据采集 Agent</h1><p class="muted">只采集页面真实可见证据；缺失字段留空，全部结果需人工复核。</p><div class="panel"><label>1688 商品详情链接<input id="collectUrl" style="width:100%" placeholder="https://detail.1688.com/offer/123.html"></label><label>页面标题<input id="collectTitle" style="width:100%" placeholder="从当前商品页复制标题"></label><label>页面可见文字<textarea id="collectText" rows="8" style="width:100%" placeholder="复制商品页中可见的标题、价格、规格和供应商文字；不要粘贴Cookie或请求头"></textarea></label><label>图片地址（每行一个，可选）<textarea id="collectImages" rows="4" style="width:100%"></textarea></label><div class="actions"><button class="primary" id="collectButton">采集并归一化</button><button id="collectConfirm" disabled>人工确认本次结果</button></div><p id="collectStatus" class="status"></p><div id="collectResult"></div></div></section>`);
    $('collectButton').onclick=runCollector;$('collectConfirm').onclick=confirmCollector;
    $('collectButton').textContent='导入已填写的页面资料';
    const manualPanel=$('collectUrl').closest('.panel');
    const manual=document.createElement('details');manual.id='collectorManual';manual.innerHTML='<summary>备用：手动导入已有页面资料</summary><p class="muted">仅在浏览器读取失败且已有真实资料时使用，不会自动打开商品网页。</p>';
    ['collectUrl','collectTitle','collectText','collectImages'].forEach(id=>manual.append($(id).closest('label')));
    manual.append($('collectButton'));manualPanel.prepend(manual);
    $('collectorView').insertAdjacentHTML('afterbegin',`<div class="panel" style="margin-bottom:18px"><h2>采集记录与图片</h2><p class="muted">自动任务会选中本次记录。也可查看历史资料；图片使用权仍需另行确认。</p><div class="actions"><select id="collectorHistory" aria-label="选择采集记录"><option value="">加载中…</option></select><button id="collectorRefresh">刷新记录</button><button id="collectorDownloadAll">下载所有未下载图片</button></div><p id="collectorDownloadStatus" role="status"></p></div>`);
    $('collectorRefresh').onclick=()=>loadCollectorHistory().catch(e=>$('collectStatus').textContent=e.message);
    $('collectorHistory').onchange=()=>loadCollectorRun($('collectorHistory').value).catch(e=>$('collectStatus').textContent=e.message);
    $('collectorDownloadAll').onclick=()=>downloadCollectorImages();
  }
  ensureCollectorUI();
  function setupImageSearch(){
    if($('imageSearchPanel'))return;
    const panel=document.createElement('section');panel.id='imageSearchPanel';panel.className='card image-search';
    panel.innerHTML=`<div><h3>以图搜图</h3><p class="muted">上传或直接粘贴一张参考图。图片只会作为1688检索条件，不读取浏览器登录信息；结果仍按文字、SKU和评分流程处理。</p></div><div class="image-search-controls"><input id="imageSearchFile" type="file" accept="image/jpeg,image/png,image/webp" hidden><button id="imageSearchChoose" type="button">选择图片</button><div id="imageSearchPaste" class="image-search-paste" tabindex="0" role="button">点击此处后按 Ctrl+V 粘贴图片，或拖入图片</div><img id="imageSearchPreview" hidden alt="以图搜图参考图预览"><button id="imageSearchStart" class="primary" type="button" disabled>开始以图搜图采集</button></div><p id="imageSearchStatus" class="status"></p>`;
    $('batchAdvice').before(panel);let referenceFile=null;
    const setFile=file=>{
      if(!file)return;const accepted=['image/jpeg','image/png','image/webp'];
      if(!accepted.includes(file.type)||file.size>10*1024*1024){$('imageSearchStatus').textContent='请选择不超过 10 MB 的 PNG、JPG 或 WebP 图片。';return;}
      referenceFile=file;const preview=$('imageSearchPreview');preview.src=URL.createObjectURL(file);preview.hidden=false;
      $('imageSearchStart').disabled=false;$('imageSearchStatus').textContent=`已选择参考图：${file.name||'粘贴图片'}（${Math.ceil(file.size/1024)} KB）`;
    };
    $('imageSearchChoose').onclick=()=>$('imageSearchFile').click();$('imageSearchFile').onchange=e=>setFile(e.target.files?.[0]);
    const paste=$('imageSearchPaste');paste.onclick=()=>paste.focus();paste.ondragover=e=>{e.preventDefault();paste.classList.add('dragging')};paste.ondragleave=()=>paste.classList.remove('dragging');paste.ondrop=e=>{e.preventDefault();paste.classList.remove('dragging');setFile(e.dataTransfer.files?.[0])};
    paste.onpaste=e=>{const item=[...e.clipboardData.items].find(x=>x.type.startsWith('image/'));if(item){e.preventDefault();setFile(item.getAsFile())}};
    $('imageSearchStart').onclick=async()=>{
      if(!referenceFile)return;const button=$('imageSearchStart');button.disabled=true;$('imageSearchStatus').textContent='正在保存参考图并下发1688以图搜图任务…';
      try{const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(new Error('无法读取参考图'));reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.readAsDataURL(referenceFile)});
        const result=await batchRequest('/api/batch-selection/image',{original_keyword:$('batchKeyword').value.trim()||'以图搜图',filters:batchAdvice?collectBatchFilters():{},image_base64:data,mime_type:referenceFile.type,target_count:20,max_candidates:60});
        currentBatch=result.batch_id;await pollBatch(currentBatch);
      }catch(e){$('imageSearchStatus').textContent=e.message}finally{button.disabled=false}
    };
  }
  setupImageSearch();
  function ensureDistributionUI(){
    if($('distributionView'))return;
    const view=document.createElement('section');
    view.id='distributionView';view.hidden=true;
    view.innerHTML=`<div class="toolbar"><div><h1>铺货 Agent</h1><p class="muted">集中查看已完成的平台文案、成品图片/视频、授权状态和发布前检查项。不会自动发布到千牛、京麦或其他店铺后台。</p></div><button id="distributionRefresh">刷新成品</button></div><p id="distributionStatus" class="status" role="status"></p><div id="distributionPackages" class="grid"></div>`;
    $('contentView').before(view);
    $('distributionRefresh').onclick=()=>loadDistribution().catch(e=>$('distributionStatus').textContent=e.message);
  }
  ensureDistributionUI();
  [...document.querySelectorAll('#stable .toolbar button')].find(x=>x.textContent.includes('默认前三'))?.remove();
  UI.setup();
  document.addEventListener('click',async e=>{const b=e.target.closest('[data-cover-retry]');if(!b)return;b.disabled=true;b.textContent='正在补采…';try{await requestJson('/api/products/'+encodeURIComponent(b.dataset.coverRetry)+'/cover',{method:'POST'});b.textContent='已提交，稍后刷新列表'}catch(err){b.textContent='补采失败，点击重试';b.disabled=false}});
  const collectorScript=document.createElement('script');collectorScript.src='/collector-auto.js';document.body.append(collectorScript);
  collectorScript.onload=UI.setupCollectorBridge;
  window.addEventListener('pintan:collector-run',event=>{collectorRun=event.detail;loadCollectorHistory().catch(e=>$('collectStatus').textContent=e.message);});
  function navigate() {
    const route = routeFor(location.hash), [mod,tab] = route.split('/');
    if(location.hash.replace(/^#/,'')!==route)history.replaceState(null,'','#'+route);
    const names = {home:'工作台',collector:'数据采集',selection:'选品',content:'内容生产',distribution:'铺货'};
    $('homeView').hidden = mod !== 'home'; $('collectorView').hidden=mod !== 'collector'; $('selectionView').hidden = mod !== 'selection'; $('contentView').hidden = mod !== 'content'; $('distributionView').hidden=mod!=='distribution';
    $('placeholderView').hidden = true;
    $('placeholderTitle').textContent = names[mod];
    $('batch').hidden=false;$('stable').hidden = tab !== 'stable'; $('trending').hidden = tab !== 'trending';
    $('batchCollection').hidden=tab==='trending';$('trendCollection').hidden=tab!=='trending';
    document.querySelectorAll('[data-collection]').forEach(x=>x.classList.toggle('on',x.dataset.collection===tab));
    const tabNames={batch:'批量稳定采集',stable:'稳定选品',trending:mod==='collector'?'爆款平替采集':'爆款选品',product:'内容预览',package:'铺货准备'};
    $('breadcrumb').textContent = names[mod] + (tab ? ' / ' + tabNames[tab] : '');
    document.querySelectorAll('[data-module]').forEach(x => { x.classList.toggle('on', x.dataset.module === mod); x.setAttribute('aria-current',x.dataset.module === mod ? 'page' : 'false'); });
    document.querySelectorAll('[data-selection]').forEach(x => x.classList.toggle('on',x.dataset.selection === tab));
    if (mod === 'content') loadContent().catch(e => $('contentStatus').textContent=e.message);
    if (mod === 'distribution') loadDistribution().catch(e => $('distributionStatus').textContent=e.message);
    if (mod === 'collector'&&tab==='batch') {loadCollectorHistory().catch(e => $('collectStatus').textContent=e.message);loadLatestBatch().catch(e=>$('batchStatus').textContent=e.message);}
    if (mod === 'collector'&&tab==='trending') loadLatestTrendDiscovery().catch(e=>$('trendDiscoveryStatus').textContent=e.message);
    if (mod === 'selection' && tab === 'stable') loadStable().catch(e => $('stableStatus').textContent=e.message);
  }
  function batchRequest(url,body){return requestJson(url,body===undefined?undefined:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
  function collectTrendFilters(){
    const base=trendAdvice?.filters||{};return {...base,
      platforms:[...document.querySelectorAll('[data-trend-platform]:checked')].map(x=>x.dataset.trendPlatform),
      price_min:$('trendPriceMin')?.value===''?null:Number($('trendPriceMin')?.value),
      price_max:$('trendPriceMax')?.value===''?null:Number($('trendPriceMax')?.value),
      min_sales:$('trendMinSales')?.value===''?null:Number($('trendMinSales')?.value),
      min_engagement:$('trendMinEngagement')?.value===''?null:Number($('trendMinEngagement')?.value),
      trend_score_threshold:Number($('trendScoreThreshold')?.value||70),evidence_coverage_threshold:Number($('trendCoverageThreshold')?.value||60),
      exclude_words:($('trendExcludeWords')?.value||'').split(/[、,，|]/).map(x=>x.trim()).filter(Boolean),exclude_ads:true,max_per_platform:20};
  }
  function renderTrendAdvice(value){
    trendAdvice=value;const f=value.filters||{};
    $('trendDiscoveryAdvice').innerHTML=`<details open><summary>筛选条件</summary><p>${esc(value.explanation||'')}</p><div class="filters"><label>最低价格<input id="trendPriceMin" type="number" min="0" step="any" value="${esc(f.price_min??'')}"></label><label>最高价格<input id="trendPriceMax" type="number" min="0" step="any" value="${esc(f.price_max??'')}"></label><label>最低展示销量<input id="trendMinSales" type="number" min="0" value="${esc(f.min_sales??'')}"></label><label>最低评论/互动<input id="trendMinEngagement" type="number" min="0" value="${esc(f.min_engagement??'')}"></label><label>爆款晋级分<input id="trendScoreThreshold" type="number" min="0" max="100" value="${esc(f.trend_score_threshold??70)}"></label><label>最低证据覆盖率 %<input id="trendCoverageThreshold" type="number" min="0" max="100" value="${esc(f.evidence_coverage_threshold??60)}"></label><label>标题排除词<input id="trendExcludeWords" value="${esc((f.exclude_words||[]).join('、'))}"></label></div></details>`;
    $('trendStartButton').disabled=false;
  }
  function renderTrendDiscovery(batch){
    currentTrendBatch=batch.batch_id;const p=batch.progress||{},platforms=p.platforms||{};
    const names={taobao:'淘宝',jd:'京东',douyin:'抖音'};
    $('trendDiscoveryStatus').textContent=`${p.message||UI.status(batch.status)} · 共发现 ${batch.source_count||0} 条 · 晋级 ${batch.candidates.filter(x=>x.status==='晋级').length} 条`;
    const needsResume=['waiting_user','interrupted','partial','failed','paused'].includes(batch.status)||Object.values(platforms).some(x=>x.status==='waiting_user');
    $('trendPauseButton').hidden=!['queued','collecting','supply_search'].includes(batch.status);
    $('trendResumeButton').hidden=!needsResume;
    $('trendCancelButton').hidden=!['queued','collecting','supply_search','waiting_user','paused'].includes(batch.status);
    $('trendDiscoveryProgress').innerHTML=Object.entries(platforms).map(([key,value])=>`<article class="metric"><b>${names[key]||key}</b><strong>${value.found||0}</strong><span>${UI.status(value.status)}${value.message?' · '+esc(value.message):''}</span></article>`).join('')+
      `<article class="metric"><b>爆款评分</b><strong>${p.scored||0}</strong><span>晋级 ${p.qualified||0}</span></article><article class="metric"><b>1688平替</b><strong>${p.supply_scored||0}</strong><span>${p.supply_search?'正在匹配':'爆款晋级后启动'}</span></article>`;
    const list=batch.candidates||[];$('trendDiscoveryResults').innerHTML=list.length?`<div class="trend-discovery-grid">${list.map(x=>`<article class="list-card"><span class="cover-wrap">${/^\/api\//.test(x.cover_url||'')?`<img class="cover" src="${esc(x.cover_url)}" alt="商品主图">`:''}<span class="cover-placeholder">暂无主图</span></span><div><h3>${esc(x.title)}</h3><p>${(x.source_platforms||[]).map(y=>`<span class="badge">${esc(names[y]||y)}</span>`).join(' ')}</p><p>爆款分 <b>${esc(x.trend_score)}</b> · 证据覆盖 ${esc(x.evidence_coverage)}%</p><p class="${x.status==='晋级'?'success':'muted'}">${esc(x.status)}${x.blockers?.length?' · '+esc(x.blockers.join('；')):''}</p><details><summary>AI建议与评分详情</summary><p>销量相对分 ${esc(x.sales_score)} · 评论/互动 ${esc(x.engagement_score)} · 搜索排名 ${esc(x.rank_score)} · 跨平台佐证 ${esc(x.cross_platform_score)}</p><p>${esc(x.merge_basis||'')}</p></details></div>${x.status==='晋级'?'<a class="button" href="#selection/trending">查看平替</a>':''}</article>`).join('')}</div>`:'<p class="empty">开始采集后，跨平台爆款会显示在这里。</p>';
    clearTimeout(trendDiscoveryTimer);if(['queued','collecting','supply_search'].includes(batch.status))trendDiscoveryTimer=setTimeout(()=>pollTrendDiscovery(batch.batch_id),1500);
  }
  async function pollTrendDiscovery(id){try{renderTrendDiscovery(await requestJson('/api/trend-discovery/batches/'+encodeURIComponent(id)))}catch(e){$('trendDiscoveryStatus').textContent=e.message}}
  async function loadLatestTrendDiscovery(){const data=await requestJson('/api/trend-discovery/batches');if(data.items?.length)renderTrendDiscovery(data.items[0]);else{$('trendDiscoveryProgress').innerHTML='<p class="empty">尚无爆款采集任务。</p>';$('trendDiscoveryResults').innerHTML=''}}
  $('trendPlanButton').onclick=async()=>{const category=$('trendCategory').value.trim();if(!category){$('trendDiscoveryStatus').textContent='请先输入商品品类。';return}$('trendDiscoveryStatus').textContent='正在生成可编辑筛选条件…';try{renderTrendAdvice(await batchRequest('/api/trend-discovery/plans',{category}));$('trendDiscoveryStatus').textContent='请检查筛选条件后开始采集。'}catch(e){$('trendDiscoveryStatus').textContent=e.message}};
  $('trendStartButton').onclick=async()=>{const button=$('trendStartButton');button.disabled=true;try{const result=await batchRequest('/api/trend-discovery/batches',{category:$('trendCategory').value.trim(),filters:collectTrendFilters()});currentTrendBatch=result.batch_id;await pollTrendDiscovery(result.batch_id)}catch(e){$('trendDiscoveryStatus').textContent=e.message}finally{button.disabled=false}};
  $('trendPauseButton').onclick=async()=>{try{renderTrendDiscovery(await batchRequest('/api/trend-discovery/batches/'+encodeURIComponent(currentTrendBatch)+'/pause',{}))}catch(e){$('trendDiscoveryStatus').textContent=e.message}};
  $('trendResumeButton').onclick=async()=>{try{renderTrendDiscovery(await batchRequest('/api/trend-discovery/batches/'+encodeURIComponent(currentTrendBatch)+'/resume',{}))}catch(e){$('trendDiscoveryStatus').textContent=e.message}};
  $('trendCancelButton').onclick=async()=>{try{renderTrendDiscovery(await batchRequest('/api/trend-discovery/batches/'+encodeURIComponent(currentTrendBatch)+'/cancel',{}))}catch(e){$('trendDiscoveryStatus').textContent=e.message}};
  function renderBatchAdvice(value){
    batchAdvice=value;
    const fields=[['最低采购价','price_min','number'],['最高采购价','price_max','number'],['最高起订量','max_moq','number'],['最低销量','min_sales','number'],['最低经营年限','min_supplier_years','number'],['最低回头率 %','min_return_rate_pct','number']];
    $('batchAdvice').innerHTML=`<div class="card"><h3>建议搜索：${esc(value.normalized_keyword)}</h3><p>${esc(value.rationale||'')}</p><p class="status">${value.source==='ai'?'AI推荐':'AI不可用，采用保守规则建议'} · 相关词：${esc((value.related_keywords||[]).join('、')||'—')}</p><div class="filters">${fields.map(([label,key,type])=>`<label>${label}<input data-batch-filter="${key}" type="${type}" min="0" step="any" value="${esc(value[key]??'')}"></label>`).join('')}<label><input data-batch-filter="exclude_ads" type="checkbox" ${value.exclude_ads?'checked':''}>排除广告</label><label><input data-batch-filter="require_dropship" type="checkbox" ${value.require_dropship?'checked':''}>要求一件代发</label><label><input data-batch-filter="require_free_shipping" type="checkbox" ${value.require_free_shipping?'checked':''}>要求包邮</label><label>标题排除词<input id="batchExcludeWords" value="${esc((value.exclude_keywords||[]).join('、'))}"></label></div></div>`;
    const filters=$('batchAdvice').querySelector('.filters'),extra=document.createElement('details');extra.innerHTML='<summary>详细筛选条件与AI建议</summary>';[...filters.children].slice(3).forEach(x=>extra.append(x));$('batchAdvice').querySelectorAll('p').forEach(x=>extra.append(x));filters.after(extra);
    $('batchStartButton').disabled=false;
  }
  function collectBatchFilters(){
    const result={};$('batchAdvice').querySelectorAll('[data-batch-filter]').forEach(input=>{
      result[input.dataset.batchFilter]=input.type==='checkbox'?input.checked:(input.value===''?null:Number(input.value));
    });result.exclude_keywords=($('batchExcludeWords')?.value||'').split(/[、,，|]/).map(x=>x.trim()).filter(Boolean);return result;
  }
  function renderBatch(batch){
    currentBatch=batch.batch_id;
    const paged=UI.paginate(batch.items,batchPage);batchPage=paged.page;
    const kind=batch.search_kind==='image'?'以图搜图':'关键词搜索';
    $('batchStatus').textContent=`${kind} · ${batch.message||UI.status(batch.phase)} · 候选 ${batch.found} · 文字完成 ${batch.collected}/${batch.target_count} · 完整评分 ${batch.scored} · 需处理 ${batch.failed}`;
    $('batchResumeButton').hidden=!['waiting_user','interrupted','partial','failed'].includes(batch.status);
    $('batchCancelButton').hidden=!['search_queued','opening_search','reading_search','waiting_user','collecting','estimating'].includes(batch.status);
    $('batchResults').innerHTML=paged.items.length?`<div class="table-wrap"><table><thead><tr><th>商品</th><th>采购价</th><th>状态</th><th>下一步</th></tr></thead><tbody>${paged.items.map(x=>`<tr><td><div class="product-head">${UI.cover(x)}<a class="product-name" href="${esc(x.page_url)}" target="_blank" rel="noopener noreferrer">${esc(x.title||'等待读取商品')}</a></div></td><td>¥${esc(x.wholesale_price_cny||x.price||'—')}</td><td>${UI.status(x.status)}</td><td>${x.promotion_status==='eligible'||x.status==='scored'?'<a href="#selection/stable">前往选品</a>':x.product_id?`<a href="#selection/stable">查看评分</a><p class="status">${esc(x.missing_score_fields||'等待评分')}</p>`:x.error_type?'读取失败，可重试':'等待处理'}${x.run_id?`<br><button data-open-collector-run="${esc(x.run_id)}">查看资料</button>`:''}</td></tr>`).join('')}</tbody></table></div>`:'<p class="empty">开始采集后，商品会显示在这里。</p>';
    UI.pager($('batchResults'),paged,p=>{batchPage=p;UI.save('batchPage',p);renderBatch(batch)});
    $('batchResults').querySelectorAll('[data-open-collector-run]').forEach(button=>button.onclick=async()=>{
      collectorRun=button.dataset.openCollectorRun;location.hash='collector';
      $('collectorSupplement').open=true;
      try{await loadCollectorHistory();await loadCollectorRun(collectorRun)}catch(e){$('collectStatus').textContent=e.message}
    });
    clearTimeout(batchTimer);
    if(['search_queued','opening_search','reading_search','waiting_user','collecting','estimating'].includes(batch.status)&&routeFor(location.hash)==='collector/batch')batchTimer=setTimeout(()=>pollBatch(batch.batch_id),1800);
  }
  async function pollBatch(id){try{const batch=await batchRequest('/api/batch-selection/'+encodeURIComponent(id));if(currentBatch===id||!currentBatch)renderBatch(batch)}catch(e){$('batchStatus').textContent=e.message;clearTimeout(batchTimer)}}
  async function loadLatestBatch(){
    const data=await batchRequest('/api/batch-selection');if(data.items.length){renderBatch(data.items[0]);return;}
    $('batchStatus').textContent='尚无批量任务，请先生成查询建议。';$('batchResults').innerHTML='';
  }
  $('batchPlanButton').onclick=async()=>{const button=$('batchPlanButton');button.disabled=true;$('batchStatus').textContent='正在生成可编辑的搜索建议…';try{const advice=await batchRequest('/api/batch-selection/plans',{keyword:$('batchKeyword').value});renderBatchAdvice(advice);$('batchStatus').textContent=`已保留原始输入“${$('batchKeyword').value}”，请确认或调整条件。`}catch(e){$('batchStatus').textContent=e.message}finally{button.disabled=false}};
  $('batchStartButton').onclick=async()=>{const button=$('batchStartButton');button.disabled=true;try{const result=await batchRequest('/api/batch-selection',{original_keyword:$('batchKeyword').value,normalized_keyword:batchAdvice.normalized_keyword,filters:collectBatchFilters(),target_count:20,max_candidates:60});currentBatch=result.batch_id;await pollBatch(currentBatch)}catch(e){$('batchStatus').textContent=e.message}finally{button.disabled=false}};
  $('batchResumeButton').onclick=async()=>{try{renderBatch(await batchRequest('/api/batch-selection/'+encodeURIComponent(currentBatch)+'/resume',{}))}catch(e){$('batchStatus').textContent=e.message}};
  $('batchCancelButton').onclick=async()=>{try{renderBatch(await batchRequest('/api/batch-selection/'+encodeURIComponent(currentBatch)+'/cancel',{}))}catch(e){$('batchStatus').textContent=e.message}};
  async function runCollector(){
    if(!$('collectTitle').value.trim()&&!$('collectText').value.trim()&&!$('collectImages').value.trim()){$('collectStatus').textContent='尚未读取页面：请通过浏览器采集助手提交资料，或填写页面文字与图片地址。仅有链接不能导入。';return;}
    $('collectButton').disabled=true;$('collectStatus').textContent='正在归一化页面证据…';
    try{const result=await requestJson('/api/collector/capture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({page_url:$('collectUrl').value,title:$('collectTitle').value,visible_text:$('collectText').value,image_urls:$('collectImages').value.split(/\r?\n/).filter(Boolean),use_model:true})});collectorRun=result.run_id;await loadCollectorHistory();}
    catch(e){$('collectStatus').textContent=e.message}finally{$('collectButton').disabled=false}
  }
  async function loadCollectorHistory(){
    const data=await requestJson('/api/collector/runs');
    $('collectorHistory').innerHTML=data.items.map(r=>`<option value="${esc(r.run_id)}">${esc(r.title)} · ${esc(r.created_at)} · ${esc(r.status)}</option>`).join('')||'<option value="">暂无采集记录</option>';
    if(data.items.some(r=>r.run_id===collectorRun))$('collectorHistory').value=collectorRun;
    if($('collectorHistory').value)await loadCollectorRun($('collectorHistory').value);
  }
  async function loadCollectorRun(id){if(!id)return;collectorRun=id;const r=await requestJson('/api/collector/runs/'+encodeURIComponent(id));if(collectorRun===id){renderCollector(r);$('collectConfirm').disabled=!r.can_confirm||r.status==='已人工复核';}return r;}
  function renderMapping(r){
    const m=r.mapping;if(!m)return '';
    const labels={...Object.fromEntries(m.catalog.map(x=>[x.field_name,x.label])),spec_price_pairs:'规格与单价'};
    const table=(heads,rows)=>`<div class="table-wrap"><table><thead><tr>${heads.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(v=>`<td>${esc(v??'—')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    const sources=Object.fromEntries(m.evidence.map(e=>[e.evidence_id,e]));
    const regions={title:'商品标题',price:'价格区',sku:'SKU区',attributes:'属性区',packaging:'包装区',supplier:'供应商区',reviews:'评价区',detail:'详情区',legacy:'历史整页文字'};
    const conflict=m.conflicts.map(c=>`${labels[c.field]||c.field}：${c.values.join(' / ')}`).join('；');
    const review=m.review_fields||[];
    const reviewRows=review.map(f=>`<tr data-review-row data-field-id="${esc(f.field_id||'')}" data-source-kind="${esc(f.source_kind||'页面采集')}" data-confidence="${esc(f.confidence??1)}" data-rationale="${esc(f.rationale||'')}" data-based-on="${esc(JSON.stringify(f.based_on_field_ids||[]))}" data-risk-note="${esc(f.risk_note||'')}"><td><input type="checkbox" data-review-include ${f.included?'checked':''} ${r.status==='已人工复核'?'disabled':''}></td><td><select data-review-name ${f.field_id||r.status==='已人工复核'?'disabled':''}>${m.catalog.map(c=>`<option value="${esc(c.field_name)}" ${c.field_name===f.field_name?'selected':''}>${esc(c.label)}</option>`).join('')}</select></td><td><input data-review-value value="${esc(f.value)}" ${r.status==='已人工复核'?'disabled':''}></td><td><b>${esc(f.source_kind)}</b>${f.anomaly_reason?`<br><small class="status">疑似错位：${esc(f.anomaly_reason)}</small>`:''}${f.risk_note&&f.source_kind!=='AI推测'?`<br><small class="status">${esc(f.risk_note)}</small>`:''}${f.source_kind==='AI推测'?`<br><small>置信度 ${Math.round(Number(f.confidence||0)*100)}%</small><br><small>${esc(f.rationale||'需人工核验')}</small>`:''}${Number(f.duplicate_count||1)>1?`<br><small>已合并 ${Number(f.duplicate_count)} 条重复证据</small>`:''}</td><td><button data-review-remove ${r.status==='已人工复核'?'disabled':''}>排除</button></td></tr>`).join('');
    const featureParts=value=>{const parts=String(value||'').split(/[:：]/);return [parts.shift()||'未命名',parts.join('：')||'—']};
    const skuPreviews=(r.sku_records||[]).filter(s=>s.sku_preview_url).map(s=>`<article class="card"><a href="${esc(s.sku_preview_url)}" target="_blank" rel="noopener"><img loading="lazy" decoding="async" src="${esc(s.sku_preview_url)}" alt="${esc(s.spec.text||'SKU图片')}" loading="lazy" style="width:100%;height:150px;object-fit:contain"></a><b>${esc(s.spec.text||'未命名SKU')}</b><p>${esc(s.spec.dimension||'规格')} · ${s.spec.displayed_image_changed===false?'共用展示图':'选中后的展示图'}</p></article>`).join('');
    return `<section class="panel"><h3>标准商品资料与数据缺口</h3><p class="muted">${m.legacy_snapshot?'这条记录来自旧版整页快照；请重新加载 v2.4.0 扩展并采集，以获得SKU图片选择关系和评论图区域。':'资料按页面区域提取，每项均保留原文。'} 展示为“待复核”的参数需人工核对。</p>${conflict?`<p class="status">字段冲突，暂不能作为确定事实：${esc(conflict)}</p>`:''}
      <h3>商品通用属性</h3>${table(['属性','值','状态'],['title','category','material','minimum_order_text'].map(name=>{const f=(m.common_attributes||[]).find(x=>x.field_name===name);return [labels[name]||name,f?.value||'—',f?.status||'必须补充']}))}
      <h3>商品特征</h3>${(m.product_features||[]).length?table(['特征','值','状态'],m.product_features.map(f=>[...featureParts(f.value),f.status])):'<p class="muted">尚未读取商品特征；可从页面属性、信息图OCR或人工补录。</p>'}
      <h3>商家信息</h3>${(m.supplier_info||[]).length?table(['信息项','值','状态'],m.supplier_info.map(f=>{const p=f.field_name==='supplier_metrics'?featureParts(f.value):[labels[f.field_name]||f.field_name,f.value];return [...p,f.status]})):'<p class="muted">尚未读取商家卡片；请使用2.4.0扩展重新采集。</p>'}
      <h3>规格、单价与图片关系</h3>${r.sku_records?.length?table(['维度','规格','单价（元）','库存','图片关系','状态'],r.sku_records.map(s=>[s.spec.dimension||'规格',s.spec.text||s.spec.name||'未提供',s.price_cny,s.stock,s.image_url?(s.spec.image_relation_type==='selected_main_image'?(s.spec.displayed_image_changed===false?'与前一选项共用展示图':'已关联选中后的主图'):'已关联SKU选项图'):'暂无页面关系',s.verification_status])):'<p class="muted">未读取到能够一一对应的规格与单价；无规格归属的促销展示价不会写入。</p>'}
      ${skuPreviews?`<section id="skuImageRelations"><h3>SKU—图片关系预览（${(r.sku_records||[]).filter(s=>s.sku_preview_url).length}）</h3><p class="muted">这里按SKU关系显示；多个SKU可以指向同一份去重后的图片文件。下方“素材库分类”中的 SKU图数量不等于这里的SKU关联数。</p><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px">${skuPreviews}</div></section>`:''}
      <h3>人工复核草稿</h3><p class="muted">相同字段和相同值会自动合并（包含全半角、空格和常见列表分隔符差异）；同字段不同值会继续保留，避免误删价格阶梯或真实冲突。取消“保留”相当于从最终商品资料中删除；网页原始证据仍保留。</p>
      <div class="table-wrap"><table><thead><tr><th>保留</th><th>字段</th><th>复核值</th><th>来源</th><th>操作</th></tr></thead><tbody id="collectorReviewFields">${reviewRows}</tbody></table></div>
      ${r.status==='已人工复核'?'<p class="total">该版本已确认，需重新采集才能创建新版本。</p>':`<div class="actions"><select id="collectorAddField">${m.catalog.map(c=>`<option value="${esc(c.field_name)}">${esc(c.label)}</option>`).join('')}</select><input id="collectorAddValue" placeholder="补录真实值并在确认前复核"><button id="collectorAddFieldButton">添加字段</button><button id="collectorAiComplete">AI一键补全缺失字段</button><button class="primary" id="collectorSaveReview">保存复核草稿</button></div><p class="muted">AI只补可合理推导的类目、功能、材质、规格、颜色和使用信息；不会编造价格、销量、供应商指标或认证。生成项会明确标为“AI推测”。</p><p id="collectorReviewStatus" class="status"></p>`}
      <details><summary>查看机器提取原始结果</summary>
      ${table(['字段','原始值 / 单位','状态','证据区域'],m.fields.map(f=>[labels[f.field_name]||f.field_name,f.value,f.status,regions[sources[f.evidence_id]?.region]||'原文']))}
      </details>
      <details><summary>查看字段映射用途与缺口</summary>${table(['字段','用途','必要性','原因'],m.catalog.map(c=>[c.label,c.present?'已提取，待复核':'待补充',c.requirement,c.reason]))}</details>
      <h3>选品计算准备情况</h3><p>尚缺字段：${esc(m.selection_input.missing_fields.map(k=>labels[k]||k).join('、')||'页面字段已具备')}</p><p>待确定采购情境：${esc(m.selection_input.missing_context.join('；'))}</p>
      <details><summary>内容可用字段（${m.content_claims.length}）</summary>${table(['字段','值','使用状态'],m.content_claims.map(c=>[labels[c.field_name],c.value,c.usage_status]))}</details>
      <details><summary>评价与痛点候选（${m.review_candidates.length}）</summary><p>评价片段需复核为具体痛点；没有统计分母，不生成频率。</p>${m.review_candidates.map(p=>`<p>${esc(p.text)} · ${esc(p.status)}</p>`).join('')}</details>
      <details><summary>查看页面原文证据（${m.evidence.length}）</summary>${m.evidence.map(e=>`<details><summary>${esc(regions[e.region]||e.region)} · ${esc(e.evidence_id)}</summary><p class="muted">${esc(e.source_locator)}</p><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(e.text)}</pre></details>`).join('')}</details></section>`;
  }
  function renderCollector(r){
    const diagnostic=r.image_diagnostics;
    $('collectStatus').textContent=`${r.status} · ${r.product_id} · ${r.model}`+(diagnostic?` · ${diagnostic.message}（本次发现 ${diagnostic.found}，新增 ${diagnostic.added??'—'}，复用 ${diagnostic.reused??'—'}）`:'');
    if(!r.facts.length&&diagnostic?.found===0)$('collectStatus').textContent='本次是空记录：未收到商品文字或图片。下方展示的是该商品以前保存的素材，并非本次采集结果。请使用浏览器助手重新采集。';
    const missing=x=>!x.source_url&&!x.preview_url&&x.status!=='已找到';
    const assetKinds=['主图','SKU图','详情图','信息图','功能图','参数图','场景图','待分类商品图'];
    const card=x=>{const controls=!x.virtual_requirement&&(x.source_url||x.preview_url)&&r.status!=='已人工复核'?`<div class="actions"><select data-asset-kind="${esc(x.asset_id)}">${assetKinds.map(k=>`<option ${k===(x.review_asset_type||x.asset_type)?'selected':''}>${esc(k)}</option>`).join('')}</select><button data-asset-save="${esc(x.asset_id)}">保存分类</button><button data-asset-exclude="${esc(x.asset_id)}">${x.review_included===false?'恢复':'排除'}</button></div>`:'';return `<article class="card" ${x.review_included===false?'style="opacity:.5"':''}>${x.preview_url?`<a href="${esc(x.preview_url)}" target="_blank" rel="noopener"><img loading="lazy" decoding="async" src="${esc(x.preview_url)}" alt="${esc(x.asset_type)}" loading="lazy" style="width:100%;height:180px;object-fit:contain"></a>`:'<div class="empty">尚无本地图片</div>'}<b>${esc(x.review_asset_type||x.asset_type||'商品图')}</b><p>${esc(x.width||'—')} × ${esc(x.height||'—')} · ${esc(x.quality||'未下载')}</p><p>${x.review_included===false?'复核草稿已排除':esc(x.status)} · ${esc(x.authorization_status||'未确认')}</p>${x.ocr_text?`<details><summary>OCR识别文字（待复核）</summary><p>${esc(x.ocr_text)}</p></details>`:''}${controls}${missing(x)?`<button data-fill-type="${esc(x.asset_type)}">上传 / 粘贴${esc(x.asset_type)}</button>`:''}${x.source_url?`<a href="${esc(x.source_url)}" target="_blank" rel="noopener noreferrer">查看来源</a> <button data-download="${esc(x.asset_id)}">${x.preview_url?'尝试高清版本':'下载 / 重试'}</button>`:''}</article>`};
    const kept=x=>x.review_included!==false&&!x.auto_passed;
    const autoPassed=r.images.filter(x=>x.auto_passed);
    const formalAssets=r.images.filter(x=>kept(x)&&(x.source_url||x.preview_url)&&(x.quality!=='缩略图'||x.is_review_image));
    const displayKind=x=>x.review_asset_type||x.asset_type||'详情图';
    // 前台只用三个稳定素材桶。信息图、功能图、参数图、场景图等
    // 语义类别仍保留在卡片和数据库，但都属于“详情素材图”。
    const groups=[
      ['主图',formalAssets.filter(x=>displayKind(x)==='主图')],
      ['SKU图',formalAssets.filter(x=>displayKind(x)==='SKU图')],
      ['详情素材图',formalAssets.filter(x=>!['主图','SKU图'].includes(displayKind(x)))],
      ['复核草稿已排除',r.images.filter(x=>x.review_included===false&&!x.auto_passed)],
      ['缺失素材需求',r.images.filter(missing)],
      ['已找到的素材需求',r.images.filter(x=>!x.source_url&&!x.preview_url&&x.status==='已找到')]
    ];
    const hiddenThumbnails=r.images.filter(x=>kept(x)&&(x.source_url||x.preview_url)&&x.quality==='缩略图'&&!x.is_review_image).length;
    // 素材类型是可重复添加的素材池，绝不能因某个“缺失需求”已经被填补
    // 就从上传入口移除。例如一个商品可以同时有多张主图和多张功能图。
    const kinds=['信息图','主图','SKU图','详情图','功能图','参数图','场景图'];
    const fillPanel=`<section class="panel" id="collectorFillPanel" style="margin:16px 0"><h3>补充商品素材与信息图识别</h3><p class="muted">需要补充参数文字时优先选择“信息图”。上传或粘贴后会自动OCR，识别结果标记为待复核，并在AI补全时优先使用。每种类型均可上传多张；人工上传不会冒充1688采集图。</p><div class="actions"><label>素材类型 <select id="collectorFillType">${kinds.map(k=>`<option>${esc(k)}</option>`).join('')}</select></label><label class="button">选择图片<input id="collectorFillFile" type="file" accept="image/jpeg,image/png,image/webp" hidden></label></div><div id="collectorPasteZone" tabindex="0" role="button" class="empty" style="margin-top:10px;padding:22px;cursor:pointer">可直接在页面空白处 Ctrl+V，或点击此处粘贴／拖放图片</div><p id="collectorFillStatus" class="status"></p></section>`;
    const activeFacts=r.facts.filter(x=>!['price_text','specification'].includes(x.field_name));
    const skuLinked=(r.sku_records||[]).filter(s=>s.sku_preview_url);
    const skuUniqueFiles=new Set(skuLinked.map(s=>s.sku_asset_id||s.sku_preview_url)).size;
    const skuSummary=skuLinked.length?`<section class="panel"><h3>SKU图片关联已识别</h3><p><b>${skuLinked.length}</b> 条SKU—图片关系 · <b>${skuUniqueFiles}</b> 份去重图片文件</p><p class="muted">“素材库分类”仅表示图片本身的主要用途，不代表SKU关联数。因此下方可能显示“SKU图 1张”，但不会丢失上面的SKU对应关系。</p><p><a href="#skuImageRelations">查看全部SKU图片关系</a></p></section>`:'';
    $('collectResult').innerHTML=skuSummary+renderMapping(r)+`<details><summary>原有采集字段记录</summary><div class="table-wrap"><table><thead><tr><th>字段</th><th>值</th><th>来源</th></tr></thead><tbody>${activeFacts.map(x=>`<tr><td>${esc(x.field_name)}</td><td>${esc(x.normalized_value)}</td><td>${esc(x.source_locator)}</td></tr>`).join('')}</tbody></table></div></details>${fillPanel}<h3>正式商品素材</h3><p class="muted">前台按主图、SKU图、详情素材图三类展示。同图兼具主图和SKU证据时优先显示为SKU图；评论区缩略图并入详情素材，其他缩略图不展示且不做OCR/模型分类。</p>${hiddenThumbnails?`<p class="status">已过滤 ${hiddenThumbnails} 张非评论区缩略图。</p>`:''}${autoPassed.length?`<p class="status">已逻辑排除 ${autoPassed.length} 张缩略资源，来源记录仍保留供审计。</p>`:''}`+groups.map(([name,items])=>`<h3>${name}（${items.length}）</h3><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px">${items.map(card).join('')}</div>`).join('');
    $('collectResult').querySelectorAll('[data-download]').forEach(b=>b.onclick=()=>downloadCollectorImages(b.dataset.download));
    $('collectResult').querySelectorAll('[data-fill-type]').forEach(b=>b.onclick=()=>{const select=$('collectorFillType');if(select)select.value=b.dataset.fillType;$('collectorFillFile').click();});
    bindCollectorReview(r);
    bindCollectorFill(r.product_id);
    // 原图语义与网页区域各自显示；排序位置由内容模块另行规划。
    const analysisPanel=document.createElement('section');analysisPanel.className='panel';
    analysisPanel.innerHTML='<h3>原图 OCR 与 AI 分类</h3><p>所有有效原图均先OCR再分类；宽高均≤160px自动过滤。语义分类不代表最终轮播顺序。</p><button id="classifyAllImages">分类全部原图 / 重试失败项</button><div id="imageAnalysisResults"></div>';
    $('collectResult').prepend(analysisPanel);
    const showAnalyses=async()=>{
      const result=await requestJson('/api/collector/products/'+encodeURIComponent(r.product_id)+'/classifications');
      if(collectorRun!==r.run_id)return;
      $('imageAnalysisResults').innerHTML=Object.entries(result.items).map(([id,x])=>`<details><summary>${esc(x.primary_type||x.status)} · ${esc(id)} ${x.confidence!=null?Math.round(x.confidence*100)+'%':''}</summary><p>${esc((x.tags||[]).join('、'))}</p><p>${esc(x.reason||'')} ${esc(x.error_type||'')}</p><p>${esc(x.model||'')} · ${x.cache_hit?'缓存命中':'已记录分类结果'} ${x.ocr_error?'OCR失败，已继续视觉分类：'+esc(x.ocr_error):esc(x.ocr_status||'')}</p><p>${esc((x.risks||[]).join('；'))}</p></details>`).join('')||'<p>尚未分析原图</p>';
      const filtered=new Set(Object.entries(result.items).filter(([,x])=>x.status==='filtered').map(([id])=>id));
      $('collectResult').querySelectorAll('[data-download]').forEach(b=>{if(filtered.has(b.dataset.download))b.closest('article')?.remove()});
      $('collectResult').querySelectorAll('[data-download]').forEach(b=>{const a=result.items[b.dataset.download];if(!a||a.status!=='success')return;const card=b.closest('article');if(!card)return;const label=document.createElement('p');label.className='status';label.textContent='AI：'+a.primary_type+' · '+(a.tags||[]).join('、');card.querySelector('b')?.after(label)});
    };
    $('classifyAllImages').onclick=async()=>{const b=$('classifyAllImages');b.disabled=true;b.textContent='正在逐张 OCR 和分类…';try{const task=await post('/api/collector/products/'+encodeURIComponent(r.product_id)+'/classify',{});await pollMediaTask(task.task_id);await loadCollectorRun(r.run_id)}catch(e){b.textContent=e.message;b.disabled=false}};
    const forceClassify=document.createElement('button');forceClassify.textContent='强制重新分类';$('classifyAllImages').after(forceClassify);forceClassify.onclick=async()=>{forceClassify.disabled=true;try{const task=await post('/api/collector/products/'+encodeURIComponent(r.product_id)+'/classify',{force:true});await pollMediaTask(task.task_id);await loadCollectorRun(r.run_id)}catch(e){forceClassify.textContent=e.message}finally{forceClassify.disabled=false}};
    void showAnalyses().catch(e=>{$('imageAnalysisResults').textContent=e.message});
    $('collectResult').querySelectorAll('[data-download]').forEach(b=>{
      const image=r.images.find(x=>x.asset_id===b.dataset.download), meta=image?.capture_metadata||{};
      const note=document.createElement('p');note.className='status';
      const download=image?.current_download;
      note.textContent=[download&&(download.success?'本次下载成功':download.retained_history?'本次下载失败，当前预览为保留的历史文件':'本次下载失败'),meta.source_locator&&('来源区域：'+meta.source_locator),meta.sku&&('SKU：'+meta.sku),meta.download_error].filter(Boolean).join(' · ');
      b.closest('article').append(note);
    });
    if(diagnostic?.regions?.length){const details=document.createElement('details'),summary=document.createElement('summary'),body=document.createElement('p');summary.textContent='查看图片提取诊断';body.textContent=diagnostic.regions.join('；');details.append(summary,body);$('collectResult').prepend(details);}
    if(r.skus?.length){const list=document.createElement('div');list.innerHTML='<h3>本次SKU页面证据</h3>'+r.skus.map(s=>`<p>${esc(s.text||s.name||'SKU')} · ${esc(s.source_locator||'page.skus')}</p>`).join('');$('collectResult').prepend(list);}
    if(r.conflicts?.length){const note=document.createElement('p');note.className='status';note.textContent='字段冲突，待人工确认：'+r.conflicts.map(c=>c.field+'：'+c.values.join(' / ')).join('；');$('collectResult').prepend(note);}
  }
  function bindCollectorReview(r){
    $('collectResult').querySelectorAll('[data-review-remove]').forEach(b=>b.onclick=()=>{b.closest('tr').querySelector('[data-review-include]').checked=false;b.closest('tr').style.opacity=.5;});
    if($('collectorAddFieldButton'))$('collectorAddFieldButton').onclick=()=>{
      const name=$('collectorAddField').value,value=$('collectorAddValue').value.trim();if(!value){$('collectorReviewStatus').textContent='请先填写要补录的真实值。';return;}
      const label=$('collectorAddField').selectedOptions[0].textContent,row=document.createElement('tr');row.dataset.reviewRow='';row.dataset.fieldId='';row.dataset.sourceKind='人工补录';row.dataset.confidence='1';row.dataset.basedOn='[]';
      row.innerHTML=`<td><input type="checkbox" data-review-include checked></td><td><select data-review-name><option value="${esc(name)}">${esc(label)}</option></select></td><td><input data-review-value value="${esc(value)}"></td><td>人工补录</td><td><button data-review-remove>排除</button></td>`;
      $('collectorReviewFields').append(row);row.querySelector('[data-review-remove]').onclick=()=>{row.querySelector('[data-review-include]').checked=false;row.style.opacity=.5;};$('collectorAddValue').value='';
    };
    if($('collectorSaveReview'))$('collectorSaveReview').onclick=()=>void saveCollectorReview(true);
    if($('collectorAiComplete'))$('collectorAiComplete').onclick=()=>void completeCollectorWithAI();
    $('collectResult').querySelectorAll('[data-asset-save]').forEach(b=>b.onclick=()=>void saveAssetReview(r,b.dataset.assetSave,true));
    $('collectResult').querySelectorAll('[data-asset-exclude]').forEach(b=>b.onclick=()=>{const x=r.images.find(a=>a.asset_id===b.dataset.assetExclude);void saveAssetReview(r,b.dataset.assetExclude,x?.review_included===false);});
  }
  async function saveCollectorReview(reload){
    if(!$('collectorReviewFields'))return;
    const fields=[...$('collectorReviewFields').querySelectorAll('[data-review-row]')].map(row=>({field_id:row.dataset.fieldId||null,field_name:row.querySelector('[data-review-name]').value,value:row.querySelector('[data-review-value]').value.trim(),included:row.querySelector('[data-review-include]').checked,source_kind:row.dataset.sourceKind||'人工补录',confidence:Number(row.dataset.confidence||1),rationale:row.dataset.rationale||'',based_on_field_ids:JSON.parse(row.dataset.basedOn||'[]'),risk_note:row.dataset.riskNote||''}));
    const status=$('collectorReviewStatus');if(status)status.textContent='正在保存复核草稿…';
    await requestJson('/api/collector/runs/'+encodeURIComponent(collectorRun)+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fields})});
    if(status)status.textContent='复核草稿已保存；点击人工确认后才发布给后续Agent。';if(reload)await loadCollectorRun(collectorRun);
  }
  async function completeCollectorWithAI(){
    const button=$('collectorAiComplete'),status=$('collectorReviewStatus');if(!button||button.disabled)return;
    button.disabled=true;status.textContent='正在保存当前草稿并启动AI补全…';
    try{
      await saveCollectorReview(false);
      const task=await requestJson('/api/collector/runs/'+encodeURIComponent(collectorRun)+'/ai-complete',{method:'POST'});
      const result=await pollTask(task.task_id,s=>{status.textContent=s==='queued'?'AI补全已排队…':'AI正在分析缺失字段…'});
      await loadCollectorRun(collectorRun);
      const end=$('collectorReviewStatus');if(end)end.textContent=result.added?`AI已生成 ${result.added} 个待复核字段，排除 ${result.anomalies_excluded||0} 个明显错位字段，采用 ${result.ocr_evidence_count||0} 份OCR证据；请逐项检查。`:'没有可安全推导的缺失字段，未生成虚构数据。';
    }catch(e){status.textContent=e.message;}finally{if(button.isConnected)button.disabled=false;}
  }
  async function saveAssetReview(r,assetId,included){
    const select=$('collectResult').querySelector(`[data-asset-kind="${CSS.escape(assetId)}"]`);await requestJson('/api/collector/runs/'+encodeURIComponent(collectorRun)+'/assets/'+encodeURIComponent(assetId)+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({asset_type:select.value,included})});await loadCollectorRun(collectorRun);
  }
  function bindCollectorFill(productId){
    const input=$('collectorFillFile'),zone=$('collectorPasteZone'),status=$('collectorFillStatus'),kind=()=>$('collectorFillType').value;
    // 每次重绘采集记录都会重新绑定。先清掉旧的全页监听，避免一次粘贴
    // 触发多个已过期的上传闭包。
    if(collectorPasteHandler)document.removeEventListener('paste',collectorPasteHandler,true);
    const upload=async file=>{
      if(!file)return;
      if(!['image/jpeg','image/png','image/webp'].includes(file.type)){status.textContent='仅支持 JPEG、PNG 或 WEBP 图片';return;}
      if(file.size>10*1024*1024){status.textContent='图片不能超过10MB';return;}
      try{
        status.textContent=`正在上传${kind()}并执行图片校验…`;
        const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file)});
        const result=await requestJson('/api/assets/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:productId,run_id:collectorRun,mime_type:file.type,data_base64:data,asset_type:kind()})});
        status.textContent=result.deduplicated?'该图片已存在，未重复保存。':`已补充${result.asset_type}：${result.width} × ${result.height}；请另行确认图片使用权。`;
        await loadCollectorRun(collectorRun);
      }catch(e){status.textContent=e.message;}
    };
    input.onchange=()=>{void upload(input.files?.[0]);input.value='';};
    zone.onclick=()=>zone.focus();
    const imageFromClipboard=clipboard=>{
      const files=[...(clipboard?.files||[])];
      return files.find(x=>x.type?.startsWith('image/'))||[...(clipboard?.items||[])]
        .map(x=>x.kind==='file'&&x.type?.startsWith('image/')?x.getAsFile():null).find(Boolean);
    };
    const editable=target=>target?.closest?.('input,textarea,[contenteditable="true"]');
    // 不再要求虚线框获得焦点：在采集页任意非文本输入位置 Ctrl+V 都能上传。
    // 捕获阶段可兼容部分浏览器对可聚焦 div 的剪贴板事件差异。
    collectorPasteHandler=e=>{
      const file=imageFromClipboard(e.clipboardData);
      if(!file){if(e.target===zone||zone.contains(e.target))status.textContent='剪贴板中没有图片，请先复制图片后再粘贴。';return;}
      if(editable(e.target)&&!zone.contains(e.target))return;
      e.preventDefault();void upload(file);
    };
    document.addEventListener('paste',collectorPasteHandler,true);
    zone.ondragover=e=>{e.preventDefault();zone.classList.add('on');};
    zone.ondragleave=()=>zone.classList.remove('on');
    zone.ondrop=e=>{e.preventDefault();zone.classList.remove('on');void upload(e.dataTransfer.files?.[0]);};
  }
  async function downloadCollectorImages(single){
    if(!collectorRun||$('collectorDownloadAll').disabled)return;
    const run=collectorRun;$('collectorDownloadAll').disabled=true;$('collectorHistory').disabled=true;$('collectorRefresh').disabled=true;
    try{
      const r=await requestJson('/api/collector/runs/'+encodeURIComponent(run));
      const ids=single?[single]:r.images.filter(x=>x.source_url&&!x.preview_url).map(x=>x.asset_id);
      let done=0,failed=0;
      for(const id of ids){
        $('collectorDownloadStatus').textContent=`下载中 ${done+1}/${ids.length}，请保持页面打开`;
        try{
          const task=await requestJson('/api/collector/assets/'+encodeURIComponent(id)+'/tasks',{method:'POST'});
          const end=Date.now()+180000;let finished=false;
          while(Date.now()<end){const state=await requestJson('/api/media/tasks/'+encodeURIComponent(task.task_id));if(state.status==='completed'){if(state.result?.status==='下载失败')failed++;finished=true;break;}if(!['queued','running'].includes(state.status))throw Error('下载中断或失败');await new Promise(resolve=>setTimeout(resolve,1000));}
          if(!finished)throw Error('等待超时');
        }catch(e){failed++;}
        done++;await loadCollectorRun(run);
      }
      $('collectorDownloadStatus').textContent=`处理完成：${done} 张，失败 ${failed} 张。失败项可单张重试。`;
    }catch(e){$('collectorDownloadStatus').textContent=e.message;}finally{$('collectorDownloadAll').disabled=false;$('collectorHistory').disabled=false;$('collectorRefresh').disabled=false;}
  }
  async function confirmCollector(){try{if($('collectorReviewFields'))await saveCollectorReview(false);await requestJson('/api/collector/runs/'+encodeURIComponent(collectorRun)+'/confirm',{method:'POST'});await loadCollectorRun(collectorRun);$('collectStatus').textContent='已确认并进入待评分候选。三项评分完整且未触发淘汰规则后，系统才会加入稳定池。';const link=document.createElement('a');link.href='#selection/stable';link.textContent=' 前往选品评分 →';$('collectStatus').append(link)}catch(e){$('collectStatus').textContent=e.message}}
  // 等本脚本中的内容状态和媒体观察器初始化完毕，再恢复深链接页面。
  window.addEventListener('hashchange',navigate); queueMicrotask(navigate);
  function product(row) {
    // 后端校验后，浏览器再限制协议；标题与地址均转义。
    const url = row.detail_link_status === 'available' && /^https?:\/\//i.test(row.detail_url || '') ? row.detail_url : '';
    const pid=row.product_id||row.source_supply_id||row.stable_id;
    return `<div class="product-head">${UI.cover(row)}<div>${url ? `<a class="product-name" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(row.title)}</a>` : `<span class="product-name">${esc(row.title)}</span>`}${!row.cover_url&&pid?`<button class="cover-retry" data-cover-retry="${esc(pid)}">补采封面</button>`:''}</div></div>`;
  }
  const val = v => esc(v === '' || v == null ? '—' : v);
  function table(rows, type) {
    if (!rows.length) return '<p class="empty">暂无符合条件的产品</p>';
    const stable = type === 'stable';
    const fields = stable ? [['利润潜力','profit_score'],['市场验证','market_validation_score'],['经营适配','operating_fit_score'],['总分','total_score']] : [['价格优势','price_advantage_score'],['特征匹配','feature_match_score'],['痛点解决','pain_resolution_score'],['供应商','supplier_quality_score'],['匹配总分','match_score']];
    return `<div class="table-wrap"><table><thead><tr>${stable?'<th>选择</th>':''}<th class="product">商品 / 来源</th><th>${stable ? '综合排名' : '匹配排名'}</th><th>类目</th><th>批发价 ¥</th>${stable ? '<th>参考售价均值 ¥</th>' : ''}${fields.map(([label])=>`<th>${label}</th>`).join('')}${stable ? '' : '<th>推荐与证据</th>'}</tr></thead><tbody>${rows.map(r=>`<tr>${stable?`<td><input type="checkbox" data-stable-asset value="${esc(r.source_supply_id||r.stable_id)}" aria-label="选择 ${esc(r.title)}"></td>`:''}<td class="product">${product(r)}<small>${esc(r.verification_status)}</small></td><td>${val(stable ? r.stable_rank : r.match_rank)}</td><td>${val(r.category)}</td><td class="score">${val(stable?(r.wholesale_price_display||r.wholesale_price_cny):r.wholesale_price_cny)}</td>${stable ? `<td class="score">${val(r.reference_retail_price_cny)}</td>` : ''}${fields.map(([,k],i)=>`<td class="score ${i===fields.length-1 ? 'total' : ''}">${val(r[k])}</td>`).join('')}${stable ? '' : `<td class="evidence"><b>${val(r.recommendation)}</b><p>${esc(r.recommendation_reason)}</p><details><summary>证据与痛点</summary><p>证据：${val(r.evidence_fields)}</p><p>可能对应：${val(r.resolved_pain_points)}</p><p>未解决：${val(r.unresolved_pain_points)}</p><p>风险：${val(r.risk_flags)}</p></details></td>`}</tr>`).join('')}</tbody></table></div>`;
  }
  function renderStable() {
    const data = UI.paginate(filterStable(stableRows,$('stableSearch').value,$('stableSort').value,$('stableDirection').value,'all').items,stablePage);stablePage=data.page;
    $('stableGrid').hidden=$('stableScope').value!=='stable';
    $('stableGrid').innerHTML = table(data.items,'stable');
    const trs=$('stableGrid').querySelectorAll('tbody tr');
    trs.forEach((tr,i)=>{
      const row=data.items[i],cell=document.createElement('td'),button=document.createElement('button');
      button.textContent='创建内容';button.className='primary';
      button.onclick=()=>{sessionStorage.setItem('pintan.content.product',row.source_supply_id||row.stable_id);location.hash='#content'};
      cell.append(button);
      const edit=document.createElement('details'),summary=document.createElement('summary');summary.textContent='AI建议与评分详情';edit.className='row-details';edit.append(summary);
      const form=document.createElement('form');
      let specs=[];try{specs=JSON.parse(row.spec_price_pairs||'[]')}catch(e){}
      let skuPrices=row.sku_reference_prices||{};if(typeof skuPrices==='string'){try{skuPrices=JSON.parse(skuPrices)}catch(e){skuPrices={}}}
      const skuDetails=Array.isArray(row.sku_scores)?row.sku_scores:[];
      if(specs.length){const wrap=document.createElement('div');wrap.innerHTML='<b>各SKU参考价与利润潜力评分</b>';specs.filter(s=>s.price_cny!=null).forEach(s=>{const detail=skuDetails.find(x=>x.specification===s.text)||{};const label=document.createElement('label');label.textContent=`${s.text} · 采购价 ¥${s.price_cny} · 利润潜力 ${detail.profit_score||'—'}`;const input=document.createElement('input');input.type='number';input.min='0.01';input.step='0.01';input.className='sku-reference';input.dataset.spec=s.text;input.value=skuPrices[s.text]??'';input.placeholder='AI建议参考价';label.append(input);wrap.append(label)});form.append(wrap)}
      const names={feature_tags:'功能标签（用 | 分隔）',sales_30d:'销量下限（保留原统计口径）',repurchase_rate_pct:'复购率 %',estimated_shipping_cny:'预估运费 ¥',supplier_quality_proxy_pct:'供应商质量人工参考分（0–100）'};
      Object.entries(names).forEach(([key,title])=>{const label=document.createElement('label');label.textContent=title;const input=document.createElement('input');input.name=key;input.value=row[key]??'';input.type=key==='feature_tags'?'text':'number';if(input.type==='number'){input.min='0';input.step='any';if(key.endsWith('_pct'))input.max='100'}label.append(input);form.append(label)});
      const save=document.createElement('button');save.textContent='保存并计算';save.type='submit';form.append(save);
      const status=document.createElement('small');status.textContent='没有依据的数值请留空；人工参考值不是平台事实。';form.append(status);
      form.onsubmit=async event=>{event.preventDefault();save.disabled=true;try{const payload=Object.fromEntries(new FormData(form));const sku={};form.querySelectorAll('.sku-reference').forEach(x=>{if(x.value!=='')sku[x.dataset.spec]=x.value});if(Object.keys(sku).length)payload.sku_reference_prices=sku;const saved=await requestJson(`/api/stable/${encodeURIComponent(row.stable_id)}/inputs`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(saved.scoring_task){status.textContent='已保存，AI正在评估各SKU建议售价与供应商…';await pollTask(saved.scoring_task.task_id)}await loadStable()}catch(e){status.textContent=e.message}finally{save.disabled=false}};
      edit.append(form);cell.append(edit);
      const estimate=document.createElement('button');estimate.textContent='AI评估并计算';edit.append(estimate);
      estimate.onclick=async()=>{estimate.disabled=true;try{const task=await post(`/api/stable/${encodeURIComponent(row.stable_id)}/estimate`,{});await pollTask(task.task_id);await loadStable()}catch(e){status.textContent=e.message}finally{estimate.disabled=false}};
      if(row.ai_sku_reference_prices&&Object.keys(row.ai_sku_reference_prices).length){const restore=document.createElement('button');restore.textContent='恢复AI建议价';restore.onclick=async()=>{try{await post(`/api/stable/${encodeURIComponent(row.stable_id)}/restore-price`,{});await loadStable()}catch(e){status.textContent=e.message}};edit.append(restore)}
      const evidence=typeof row.scoring_evidence==='string'?JSON.parse(row.scoring_evidence||'{}'):(row.scoring_evidence||{});
      const sourceNote=document.createElement('small');sourceNote.textContent=Object.values(evidence).map(v=>`${v.source}：${v.raw}`).join('；')+(row.ai_estimate?`；AI建议：${row.ai_estimate.reason}`:'');edit.append(sourceNote);
      const note=document.createElement('small');note.textContent=(row.score_status||'')+` · 经营适配覆盖 ${row.operating_coverage||'—'}${row.shipping_assumption?' · '+row.shipping_assumption:''}`;edit.append(note);tr.append(cell);
      const breakdown=document.createElement('details');breakdown.innerHTML=`<summary>三级评分明细</summary><p>利润潜力：${val(row.profit_score)}（${esc(row.sku_score_count||0)}个SKU算术平均）</p><p>市场验证：${val(row.market_validation_score)}（销量 ${val(row.sales_score)} ×65% + 回头率 ${val(row.return_rate_score)} ×35%）</p><p>经营适配：${val(row.operating_fit_score)}（${Object.entries(row.operating_subscores||{}).map(([k,v])=>`${esc(k)} ${esc(v)}`).join('；')||'无可计算项'}）</p>`;cell.append(breakdown);
    });
    const header=$('stableGrid').querySelector('thead tr');if(header){const th=document.createElement('th');th.textContent='内容生产';header.append(th)}
    $('stableGrid').querySelectorAll('[data-stable-asset]').forEach(x=>{x.checked=selectedStable.has(x.value);x.onchange=()=>{x.checked?selectedStable.add(x.value):selectedStable.delete(x.value);UI.save('selectedStable',[...selectedStable]);$('stableAssetsButton').disabled=!selectedStable.size;$('stableCount').textContent=`已选 ${selectedStable.size} 个商品`}});
    $('stableAssetsButton').disabled=!selectedStable.size;
    $('stableCount').textContent = selectedStable.size?`已选 ${selectedStable.size} 个商品`:'';
    const bar=UI.pager($('stableGrid'),data,p=>{stablePage=p;UI.save('stablePage',p);renderStable()});bar.hidden=$('stableScope').value!=='stable';
    $('stableGrid').querySelectorAll('td:last-child>details:not(.row-details)').forEach(x=>x.previousElementSibling?.append(x));
  }
  ['stableSearch','stableSort','stableDirection','stableScope'].forEach(id=>{const saved=UI.read(id,null);if(saved!==null)$(id).value=saved;$(id).addEventListener(id==='stableSearch'?'input':'change',()=>{stablePage=candidatePage=1;UI.save('stablePage',1);UI.save('candidatePage',1);UI.save(id,$(id).value);renderStable();renderCandidates()})});
  $('stableAssetsButton').onclick=async()=>{const ids=[...selectedStable];if(!ids.length)return;const button=$('stableAssetsButton');button.disabled=true;try{const result=await requestJson('/api/collector/assets-tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_ids:ids})});sessionStorage.setItem('pintan.content.product',ids[0]);$('stableStatus').textContent=`已提交 ${result.items.length} 个素材准备任务`;location.hash='#content'}catch(e){$('stableStatus').textContent=e.message}finally{button.disabled=false}};
  function buttons() {
    $('matchButton').disabled = trendingBusy || !selected;
    $('forceButton').disabled = trendingBusy || !selected;
    $('detailButton').disabled = trendingBusy || !detailReady;
  }
  function card(row, type) {
    const fields = type === 'stable' ? [['利润','profit_score'],['市场','market_validation_score'],['适配','operating_fit_score'],['总分','total_score']] : [['特征','feature_match_score'],['价格','price_advantage_score'],['痛点','pain_resolution_score'],['供应商','supplier_quality_score'],['匹配','match_score']];
    return `<div class="card"><span class="rank">#${esc((type === 'stable' ? row.stable_rank : row.match_rank) || '—')}</span><h3>${esc(row.title)}</h3><small>${esc(row.verification_status)} · ¥${esc(row.wholesale_price_cny)}</small><div class="scores">${fields.map(([name,key]) => `<span>${name}<br><b>${esc(row[key] === '' ? '—' : row[key] ?? '—')}</b></span>`).join('')}</div><p>${esc(row.recommendation_reason)}</p><small>${esc(row.evidence_fields)}</small><p><small>${esc(row.risk_flags)}</small></p></div>`;
  }
  function renderCandidates(){
    let panel=$('candidatePanel');if(!panel){panel=document.createElement('section');panel.id='candidatePanel';panel.className='panel';$('stableGrid').after(panel)}
    panel.hidden=$('stableScope').value!=='pending';
    const page=UI.paginate(filterStable(candidateRows,$('stableSearch').value,$('stableSort').value,$('stableDirection').value,'all').items,candidatePage);candidatePage=page.page;
    const skuFields=row=>{let pairs=[];try{pairs=typeof row.spec_price_pairs==='string'?JSON.parse(row.spec_price_pairs):row.spec_price_pairs||[]}catch(e){}const prices=row.sku_reference_prices||{};return pairs.filter(x=>x.price_cny!=null).map(x=>`<label>${esc(x.text)}：采购 ¥${esc(x.price_cny)} / 参考售价<input data-sku="${esc(x.text)}" type="number" min="0.01" step="0.01" value="${esc(prices[x.text]??'')}"></label>`).join('')};
    panel.innerHTML=page.items.length?page.items.map(row=>`<article class="list-card" data-candidate="${esc(row.stable_id)}">${product(row)}<div class="scores"><span>利润潜力 <b>${val(row.profit_score)}</b></span><span>市场验证 <b>${val(row.market_validation_score)}</b></span><span>经营适配 <b>${val(row.operating_fit_score)}</b></span></div><p class="status">${row.promotion_status==='rejected'?'已淘汰：'+esc((row.hard_rejections||[]).join('；')):'待补充：'+esc(row.missing_score_fields||'等待重新计算')}</p><details><summary>补充资料与AI建议</summary><div class="filters"><label>销量下限<input data-field="sales_30d" type="number" min="0" value="${esc(row.sales_30d??'')}"></label><label>回头率 %<input data-field="repurchase_rate_pct" type="number" min="0" max="100" step="any" value="${esc(row.repurchase_rate_pct??'')}"></label>${skuFields(row)}</div><div class="actions"><button data-candidate-save>保存并重算</button><button data-candidate-ai>AI评估售价并重算</button></div><small>${esc(row.shipping_assumption||'')}</small></details></article>`).join(''):'<p class="empty">没有待补充商品。</p>';
    UI.pager(panel,page,p=>{candidatePage=p;UI.save('candidatePage',p);renderCandidates()}).hidden=panel.hidden;
    panel.querySelectorAll('[data-candidate]').forEach(card=>{const id=card.dataset.candidate,status=card.querySelector('.status');card.querySelector('[data-candidate-save]').onclick=async()=>{try{const payload={},sku={};card.querySelectorAll('[data-field]').forEach(x=>payload[x.dataset.field]=x.value);card.querySelectorAll('[data-sku]').forEach(x=>{if(x.value)sku[x.dataset.sku]=x.value});if(Object.keys(sku).length)payload.sku_reference_prices=sku;await requestJson(`/api/selection/candidates/${encodeURIComponent(id)}/inputs`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});await loadStable()}catch(e){status.textContent=e.message}};card.querySelector('[data-candidate-ai]').onclick=async()=>{try{const task=await post(`/api/selection/candidates/${encodeURIComponent(id)}/estimate`,{force:true});await pollTask(task.task_id);await loadStable()}catch(e){status.textContent=e.message}}});
  }
  async function loadStable() {
    const [data,candidates] = await Promise.all([requestJson('/api/stable'),requestJson('/api/selection/candidates')]);
    if(!$('stableSort').dataset.v5){$('stableSort').innerHTML='<option value="total_score">总分</option><option value="profit_score">利润潜力</option><option value="market_validation_score">市场验证</option><option value="operating_fit_score">经营适配</option>';$('stableSort').value=UI.read('stableSort','total_score');$('stableSort').dataset.v5='1'}
    $('stableStatus').textContent = data.scored ? '稳定池仅显示完整评分商品' : '暂无满足完整入池条件的商品';
    $('reportBox').hidden = !data.report; $('stableReport').textContent = data.report || '';
    stableRows = data.items || data.top10;candidateRows=candidates.items||[]; renderStable();renderCandidates();
  }
  function renderDetails(data, rows) {
    $('detailPanel').hidden = !data;
    $('detailContent').innerHTML = data ? data.explanations.map(x => `<div class="card"><h4>${esc(rows.find(r => r.search_id === x.search_id)?.title || x.search_id)}</h4><p>${esc(x.explanation)}</p><small>${esc(x.evidence_fields.join(' · '))}</small></div>`).join('') : '';
  }
  async function selectTrend(id) {
    if(selected!==id)trendPage=1;selected = id; detailReady = false; buttons();
    $('trendFact').textContent = '读取中…'; $('top3').innerHTML = ''; $('all10').innerHTML = '';
    $('matchMeta').textContent = ''; $('detailPanel').hidden = true;
    document.querySelectorAll('.trend-item').forEach(x => x.classList.toggle('on',x.dataset.id === id));
    const data = await requestJson('/api/trends/' + encodeURIComponent(id));
    if (selected !== id) return; // 用户切换商品时，不让较晚返回的旧请求覆盖当前商品。
    $('trendFact').innerHTML = `<div class="table-wrap"><table><thead><tr><th>爆款商品</th><th>平台 / 零售价</th><th>需求与痛点</th></tr></thead><tbody><tr><td class="product">${product(data.trend)}</td><td>${esc(data.trend.platform)}<br>¥${val(data.trend.retail_price_cny)}<br>${data.candidate_count} 条候选</td><td>${val(data.trend.core_features)}<p>${val(data.trend.negative_review_pain_points)}</p></td></tr></tbody></table></div>`;
    trendData=data;renderTrendRows();
    const meta = data.metadata;
    $('matchMeta').textContent = !meta ? '尚无新版匹配结果' : meta.stale ? '输入或版本已变化，以下为历史结果，请重新匹配' : `${meta.cache_hit ? '缓存命中' : '重新计算'} · 本次 ${meta.elapsed_seconds} 秒 · 生成时间 ${meta.generated_at} · 总分由本地工具计算`;
    detailReady = !!meta && !meta.stale && data.top3.length === 3;
    renderDetails(data.details, data.top3); buttons();
  }
  function renderTrendRows(){
    if(!trendData)return;
    const page=UI.paginate([...trendData.all10].sort((a,b)=>(Number(a.match_rank)||999)-(Number(b.match_rank)||999)),trendPage);trendPage=page.page;
    $('top3').innerHTML=table(page.items,'trend');
    $('top3').querySelectorAll('.evidence').forEach(cell=>{const details=document.createElement('details');details.innerHTML='<summary>AI建议与评分详情</summary>';while(cell.firstChild)details.append(cell.firstChild);cell.append(details)});
    UI.pager($('top3'),page,p=>{trendPage=p;renderTrendRows()});
  }
  window.runStable = async () => {
    if (stableBusy) return; stableBusy = true; $('stableButton').disabled = true;
    try {
      const task = await requestJson('/api/tasks/stable',{method:'POST'});
      await pollTask(task.task_id, status => $('stableStatus').textContent = status === 'queued' ? '排队中' : '评分中');
      await loadStable();
    } catch (error) { $('stableStatus').textContent = error.message; }
    finally { stableBusy = false; $('stableButton').disabled = false; }
  };
  async function runTrendTask(details, force = false) {
    if (trendingBusy || !selected) return;
    const id = selected; trendingBusy = true; buttons();
    $('trendStatus').textContent = details ? '正在生成 Top 3 详解…' : force ? '正在强制重算…' : '正在检查缓存并匹配…';
    try {
      const url = '/api/tasks/trending/' + encodeURIComponent(id) + (details ? '/explanations' : '?force=' + force);
      const task = await requestJson(url,{method:'POST'});
      await pollTask(task.task_id, status => $('trendStatus').textContent = status === 'queued' ? '排队中…' : details ? '生成详解中…' : '匹配中…');
      if (selected === id) await selectTrend(id);
      $('trendStatus').textContent = '任务完成';
    } catch (error) { $('trendStatus').textContent = error.message; }
    finally { trendingBusy = false; buttons(); }
  }
  window.runTrending = force => runTrendTask(false, force);
  window.runDetails = () => runTrendTask(true);
  let activeDiagnosis=null, activePlatform='', activeResult=null, activeProductId='', mediaBusy=false, activeImagePlan=null, activeVideoPlan=null, activeReadiness=null, readinessTimer=null, factAutofillRuns=new Set();
  function ensureMediaPanel(){
    if ($('mediaPanel')) return;
    const style=document.createElement('style');
    style.textContent='.media-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}.media-card{border:1px solid var(--line);border-radius:9px;padding:12px;background:#f9fbfa}.media-card img,.media-card video{width:100%;aspect-ratio:1/1;object-fit:contain;background:#edf2ef;border-radius:7px}.media-card video{aspect-ratio:16/9}.ocr{max-height:90px;overflow:auto;white-space:pre-wrap;font-size:12px}.step-note{border-left:4px solid #bd871b;padding:10px 14px;background:#fff8e8}.generation-modes{display:grid;grid-template-columns:repeat(2,minmax(240px,1fr));gap:12px;margin:14px 0}.mode-card{display:flex;gap:10px;padding:14px;border:1px solid var(--line);border-radius:10px;background:#fff;cursor:pointer}.mode-card:has(input:checked){border-color:#1b755d;box-shadow:0 0 0 2px #d6eee7}.mode-card small{display:block;color:#5f6f68;margin-top:5px}.mode-badge{display:inline-block;padding:3px 8px;border-radius:999px;background:#e8f2ee;color:#1b604e;font-size:12px}.plan-row{display:grid;grid-template-columns:1fr minmax(150px,220px);gap:12px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line)}.video-storyboard{display:grid;grid-template-columns:repeat(5,minmax(150px,1fr));gap:10px;margin:12px 0}.video-shot{border:1px solid var(--line);border-radius:9px;padding:10px;background:#f8faf9}.video-shot video{width:100%;aspect-ratio:9/16;max-height:280px;background:#17211e;border-radius:7px}.video-shot p{margin:7px 0}.video-result video{width:min(100%,520px);max-height:620px;background:#17211e;border-radius:10px}@media(max-width:1000px){.video-storyboard{grid-template-columns:repeat(2,minmax(150px,1fr))}}@media(max-width:760px){.generation-modes,.plan-row,.video-storyboard{grid-template-columns:1fr}}';
    document.head.appendChild(style);
    $('resultPanel').insertAdjacentHTML('beforeend','<div id="mediaPanel" hidden style="margin-top:24px"><hr><div class="toolbar"><div><h2>4　规划与生成图片</h2><p id="mediaStatus" class="status">平台文案完成后可生成整组图片</p></div><div class="actions"><label>审核强度 <select id="auditLevel"><option value="none">无规则（不评分）</option><option value="light">轻度规则（60分）</option><option value="normal" selected>正常规则（75分）</option><option value="strict">严谨规则（90分）</option></select></label><button id="imageButton" onclick="generateMediaImages()" disabled>生成平台图片</button></div></div><p class="step-note">审核强度由用户决定；单张失败不会删除同组成功图片，最多按档位自动返工三轮。</p><div id="auditHelp" class="status">正常规则：75分，无阻断问题；最多自动修复2轮。</div><h3>整批图片生成方式</h3><div class="generation-modes"><label class="mode-card"><input type="radio" name="generationMode" value="conservative" checked><span><b>保守改造</b><small>读取原图，保持商品、SKU与主要构图，只清理背景、光影和外围广告文字。</small></span></label><label class="mode-card"><input type="radio" name="generationMode" value="creative"><span><b>创意重构</b><small>本地抠出商品，只把白底主体交给模型，由模型重新设计场景和构图。</small></span></label></div><p class="status">痛点、厨房、办公等环境策划图会自动使用“纯文字场景”。</p><div id="sourceAssets" class="media-grid" hidden></div><h3>生成图片</h3><div id="generatedMedia" class="media-grid"></div></div>');
    $('resultPanel').after($('mediaPanel'));$('mediaPanel').classList.add('panel');
    $('mediaPanel').insertAdjacentHTML('beforeend','<section id="videoPanel" style="margin-top:28px"><hr><div class="toolbar"><div><h2>5　生成15秒短视频</h2><p id="videoStatus" class="status">先完成至少3张同模式平台图片，再规划5个镜头。</p></div><div class="actions"><label>画面比例 <select id="videoRatio"><option value="auto">自动适配平台</option><option value="9:16">竖屏 9:16</option><option value="1:1">方图 1:1</option><option value="16:9">横屏 16:9</option></select></label><button id="videoPlanButton">规划5个镜头</button><button class="primary" id="videoButton" disabled>并行生成15秒视频</button><button id="videoRetryButton" hidden>重试失败镜头</button></div></div><p class="step-note">每段3秒，最多同时生成2段。字幕只使用已审核商品事实；首版无视频模型时使用本地运镜合成，不产生视频模型费用。</p><div id="videoPlanPreview"></div><div id="videoResult" class="video-result"></div></section>');
    $('videoPlanButton').onclick=planMediaVideo;$('videoButton').onclick=generateMediaVideo;$('videoRetryButton').onclick=retryFailedVideoShots;
    const cancel=document.createElement('button');cancel.id='mediaCancelButton';cancel.hidden=true;cancel.textContent='中断当前任务';cancel.onclick=()=>window.cancelCurrentMediaTask();$('imageButton').after(cancel);
    const resume=document.createElement('button');resume.id='mediaResumeButton';resume.hidden=true;resume.textContent='恢复查看进度';cancel.after(resume);
    resume.onclick=async()=>{resume.hidden=true;try{await pollMediaTask(activeMediaTaskId);await loadMedia()}catch(e){$('mediaStatus').textContent=e.message}};
    const descriptions={none:'无规则：不调用AI视觉审核，只校验图片文件与尺寸；生成1轮。',light:'轻度规则：60分，只拦截明显换品、严重变形和虚构品牌参数；最多2轮。',normal:'正常规则：75分，检查关键外观、事实合规和当前单图用途；最多2轮。',strict:'严谨规则：90分，高一致性审核；最多3轮。'};
    $('auditLevel').addEventListener('change',e=>{$('auditHelp').textContent=descriptions[e.target.value];localStorage.setItem('pintan.audit.level',e.target.value)});
    const saved=localStorage.getItem('pintan.audit.level');if(descriptions[saved]){$('auditLevel').value=saved;$('auditHelp').textContent=descriptions[saved]}
    const savedMode=localStorage.getItem('pintan.image.mode');if(['conservative','creative'].includes(savedMode)){const radio=document.querySelector(`input[name="generationMode"][value="${savedMode}"]`);if(radio)radio.checked=true}
    document.querySelectorAll('input[name="generationMode"]').forEach(x=>x.addEventListener('change',()=>localStorage.setItem('pintan.image.mode',x.value)));
  }
  function ensureReadinessUI(){
    if($('materialPanel'))return;
    const first=$('contentCandidates').closest('.panel');
    first.id='productChoicePanel';UI.fold(first);
    $('diagnoseButton').textContent='AI推荐内容风格';$('diagnoseButton').disabled=true;
    $('diagnoseButton').before(Object.assign(document.createElement('button'),{id:'prepareButton',textContent:'开始完善素材'}));
    $('prepareButton').onclick=prepareSelectedProduct;
    first.insertAdjacentHTML('afterend',`<div class="panel" id="materialPanel" style="margin-top:18px"><div class="toolbar"><div><h2>2　采集文字与高清图片</h2><p id="materialStatus" class="status">请选择商品</p></div><div class="actions"><button id="resumePrepareButton" hidden>继续采集</button><button id="openCollectorButton" hidden>打开详细复核页</button></div></div><div id="materialStats"></div></div><div class="panel" id="factReviewPanel" style="margin-top:18px"><div class="toolbar"><div><h2>3　商品事实复核</h2><p class="status">页面事实优先；缺失且允许推断的字段由AI自动生成，并明确标记“AI推测”。</p></div><div class="actions"><button id="autoCompleteFactsButton" hidden>AI重新补齐缺失字段</button><button id="savePreparedFactsButton" disabled>保存复核修改</button><button id="confirmFactsButton" disabled>确认当前事实</button></div></div><div id="factReviewSummary"></div></div><div class="panel" id="assetAuthorizationPanel" style="margin-top:18px"><div class="toolbar"><div><h2>4　图片复核与使用权确认</h2><p class="status">一次确认当前商品所选图片；图片哈希变化后需重新确认。</p></div><div class="actions"><label class="button">上传补充图<input id="preparationUpload" type="file" accept="image/jpeg,image/png,image/webp" hidden></label><button id="authorizeProductAssets" disabled>确认当前全部图片使用权</button></div></div><div id="preparationAssets" class="media-grid"></div></div>`);
    ['materialPanel','factReviewPanel','assetAuthorizationPanel'].forEach(id=>UI.fold($(id)));UI.steps(1);
    $('assetAuthorizationPanel').insertAdjacentHTML('afterend','<div id="contentNextActions" class="actions"></div>');
    $('contentNextActions').append($('diagnoseButton'));$('materialPanel').querySelector('.actions').prepend($('prepareButton'));
    $('productChoicePanel').before($('contentStatus'));
    $('resumePrepareButton').onclick=resumePreparation;$('openCollectorButton').onclick=()=>{if(activeReadiness?.facts?.run_id){collectorRun=activeReadiness.facts.run_id;location.hash='#collector'}};
    $('autoCompleteFactsButton').onclick=()=>autoCompletePreparedFacts(true);$('savePreparedFactsButton').onclick=savePreparedFacts;$('confirmFactsButton').onclick=confirmPreparedFacts;$('authorizeProductAssets').onclick=authorizePreparedAssets;
    $('preparationUpload').onchange=uploadPreparationAsset;
    $('factReviewPanel').querySelector('h2').firstChild.textContent='商品事实复核';
    $('assetAuthorizationPanel').querySelector('h2').firstChild.textContent='图片复核与使用权确认';
    $('assetAuthorizationPanel').querySelector('.status').textContent='确认当前所选图片的使用权，新图片需要重新确认。';
    $('anglePanel').querySelector('h2').textContent='3　选择内容风格';
    $('platformPanel').querySelector('h2').textContent='选择目标平台并生成文案';
    $('resultPanel').querySelector('h2').textContent='平台文案预览';
    const editor=document.createElement('details');editor.innerHTML='<summary>编辑文案字段</summary>';
    $('visibleResult').before(editor);editor.append($('visibleResult'));
    editor.insertAdjacentHTML('beforebegin','<div id="platformReadable"></div>');
  }
  async function loadReadiness(productId){
    clearTimeout(readinessTimer);if(!productId)return;
    const state=await requestJson('/api/content/products/'+encodeURIComponent(productId)+'/readiness');
    if(mediaProductId()!==productId)return;activeReadiness=state;
    const t=state.task||{};
    $('materialStatus').textContent=t.status_label||state.blockers[0]?.message||'等待开始素材采集';
    $('materialStats').innerHTML=`<p>发现 ${esc(t.found??state.assets.discovered)} 张 · 下载 ${esc(t.downloaded??state.assets.downloaded)} 张 · 失败 ${esc(t.failed??0)} 张</p>${state.blockers.length?`<div class="step-note">${state.blockers.map(x=>`<p>${esc(x.message)}</p>`).join('')}</div>`:'<p class="total">素材与事实准备完成，可以生成内容。</p>'}`;
    $('prepareButton').hidden=state.next_action!=='prepare';$('prepareButton').disabled=false;
    $('resumePrepareButton').hidden=!['wait'].includes(state.next_action)||t.status!=='waiting_user';
    $('openCollectorButton').hidden=!state.facts.run_id;
    const f=state.facts;
    const catalog=f.catalog||[],options=name=>catalog.map(x=>`<option value="${esc(x.field_name)}" ${x.field_name===name?'selected':''}>${esc(x.label||x.field_name)}</option>`).join('');
    const review=[...f.review_fields,...f.missing_required.map(name=>({field_id:null,field_name:name,value:'',included:true,source_kind:'人工补录',confidence:1}))];
    $('factReviewSummary').innerHTML=`<p>已发布事实 ${esc(f.claim_count)} 条 · ${f.confirmed?'已确认':'待确认'}</p>${f.missing_required.length?`<p class="status">必须补齐：${esc(f.missing_required.join('、'))}</p>`:''}${f.conflicts.length?`<p class="status">冲突字段已默认排除，可在详细复核页选择保留项：${esc(f.conflicts.map(x=>x.field||x.field_name||'字段').join('、'))}</p>`:''}<div class="table-wrap"><table id="preparedFactTable"><thead><tr><th>采用</th><th>字段</th><th>值</th><th>来源</th></tr></thead><tbody>${review.slice(0,100).map(x=>`<tr data-prepared-fact data-field-id="${esc(x.field_id||'')}" data-source-kind="${esc(x.source_kind||'页面采集')}" data-confidence="${esc(x.confidence??1)}"><td><input data-prepared-include type="checkbox" ${x.included?'checked':''} ${f.confirmed?'disabled':''}></td><td><select data-prepared-name ${x.field_id||f.confirmed?'disabled':''}>${options(x.field_name)}</select></td><td><input data-prepared-value value="${esc(x.value)}" ${f.confirmed?'disabled':''}></td><td>${esc(x.source_kind)}</td></tr>`).join('')}</tbody></table></div>`;
    $('savePreparedFactsButton').disabled=!f.run_id||f.confirmed||['queued','opening','reading','waiting_user','normalizing','downloading'].includes(t.status);
    $('confirmFactsButton').disabled=!f.run_id||f.confirmed||f.missing_required.length>0||['queued','opening','reading','waiting_user','normalizing','downloading'].includes(t.status);
    $('autoCompleteFactsButton').hidden=f.confirmed||!f.run_id||!f.missing_required.length;
    const assetKinds=['主图','SKU图','详情图','信息图','功能图','参数图','场景图','待分类商品图'];
    const assetCard=x=>{const ai=x.semantic_classification||{};return `<article class="media-card" data-prepared-asset="${esc(x.asset_id)}"><img loading="lazy" decoding="async" src="${esc(x.preview_url)}" alt="${esc(x.asset_type)}"><label>人工用途分类 <select data-prepared-asset-kind>${assetKinds.map(k=>`<option value="${esc(k)}" ${k===x.asset_type?'selected':''}>${esc(k)}</option>`).join('')}</select></label><p>${esc(x.width)}×${esc(x.height)} · ${esc(x.authorization_status)}</p><p class="status">AI语义：${esc(ai.primary_type||({failed:'分类失败',filtered:'缩略图已过滤'}[ai.status]||'等待分类'))}${ai.confidence==null?'':` · ${esc(Math.round(ai.confidence*100))}%`}</p>${ai.reason?`<details><summary>分类依据</summary><p>${esc(ai.reason)}</p></details>`:''}<div class="actions"><button onclick="reviewPreparedAsset('${esc(x.asset_id)}',true)">保存分类</button><button class="danger" onclick="reviewPreparedAsset('${esc(x.asset_id)}',false)">排除</button></div></article>`};
    const groups=[['主图',state.assets.items.filter(x=>x.asset_type==='主图')],['SKU图',state.assets.items.filter(x=>x.asset_type==='SKU图')],['详情素材',state.assets.items.filter(x=>!['主图','SKU图'].includes(x.asset_type))]];
    $('preparationAssets').innerHTML=state.assets.items.length?groups.map(([name,items])=>`<section style="grid-column:1/-1"><h3>${esc(name)}（${items.length}）</h3><div class="media-grid">${items.length?items.map(assetCard).join(''):'<p class="empty">本次未发现此类素材</p>'}</div></section>`).join(''):'<p class="empty">尚无可用图片；可继续采集或人工上传。</p>';
    $('authorizeProductAssets').disabled=!f.confirmed||!state.assets.usable||state.assets.all_authorized;
    $('authorizeProductAssets').textContent=state.assets.all_authorized?'当前图片已全部授权':`确认当前 ${state.assets.usable} 张图片使用权`;
    $('diagnoseButton').disabled=!state.ready;
    UI.collapse('productChoicePanel',Boolean(f.run_id)||state.ready);
    UI.collapse('materialPanel',Boolean(f.confirmed)&&state.assets.usable>0&&!t.failed&&!['queued','opening','reading','waiting_user','normalizing','downloading','failed','interrupted'].includes(t.status));
    UI.collapse('factReviewPanel',f.confirmed);UI.collapse('assetAuthorizationPanel',state.assets.all_authorized);
    if(!activeResult)UI.steps(state.ready?2:1);
    $('contentStatus').textContent=state.ready?'准备完成，可以选择风格并生成文案':(state.blockers[0]?.message||'请完成素材准备');
    if(t.task_id&&['queued','opening','reading','waiting_user','normalizing','downloading'].includes(t.status))readinessTimer=setTimeout(()=>loadReadiness(productId).catch(e=>$('contentStatus').textContent=e.message),1600);
    else if(!f.confirmed&&f.run_id&&f.missing_required.length&&!factAutofillRuns.has(f.run_id))autoCompletePreparedFacts(false);
    return state;
  }
  async function prepareSelectedProduct(){const id=mediaProductId();if(!id)return;const b=$('prepareButton');b.disabled=true;try{await post('/api/content/products/'+encodeURIComponent(id)+'/prepare',{});await loadReadiness(id)}catch(e){$('contentStatus').textContent=e.message}finally{b.disabled=false}}
  async function resumePreparation(){const task=activeReadiness?.task;if(!task)return;try{await post('/api/collector/tasks/'+encodeURIComponent(task.task_id)+'/resume',{});await loadReadiness(mediaProductId())}catch(e){$('contentStatus').textContent=e.message}}
  async function autoCompletePreparedFacts(force){const id=mediaProductId(),run=activeReadiness?.facts?.run_id;if(!run)return;factAutofillRuns.add(run);$('contentStatus').textContent='AI正在根据已采集文字和图片OCR补齐缺失字段…';try{const task=await post('/api/collector/runs/'+encodeURIComponent(run)+'/ai-complete',{});await pollTask(task.task_id,status=>$('contentStatus').textContent=status==='queued'?'AI补全排队中…':'AI正在补全缺失字段…');await loadReadiness(id);$('contentStatus').textContent='AI补全草稿已生成，请复核后确认'}catch(e){$('contentStatus').textContent='AI补全失败：'+e.message+'；可点击“AI重新补齐缺失字段”重试';if(force)factAutofillRuns.delete(run)}}
  async function savePreparedFacts(){const id=mediaProductId(),run=activeReadiness?.facts?.run_id;if(!run)return;const fields=[...$('preparedFactTable').querySelectorAll('[data-prepared-fact]')].map(row=>({field_id:row.dataset.fieldId||null,field_name:row.querySelector('[data-prepared-name]').value,value:row.querySelector('[data-prepared-value]').value.trim(),included:row.querySelector('[data-prepared-include]').checked,source_kind:row.dataset.sourceKind||'人工补录',confidence:Number(row.dataset.confidence||1)})).filter(x=>x.value);try{await post('/api/collector/runs/'+encodeURIComponent(run)+'/review',{fields});await loadReadiness(id);$('contentStatus').textContent='复核草稿已保存，请确认事实'}catch(e){$('contentStatus').textContent=e.message}}
  async function confirmPreparedFacts(){const id=mediaProductId();try{await post('/api/content/products/'+encodeURIComponent(id)+'/confirm-facts',{run_id:activeReadiness?.facts?.run_id});await loadReadiness(id)}catch(e){$('contentStatus').textContent=e.message}}
  async function authorizePreparedAssets(){const id=mediaProductId();if(!confirm('请确认：你拥有当前商品所列图片的使用权，并同意用于生成营销素材。'))return;try{await post('/api/content/products/'+encodeURIComponent(id)+'/authorize-assets',{confirmed:true,statement:'我已确认拥有该商品当前所选图片的使用权',asset_ids:activeReadiness.assets.items.map(x=>x.asset_id)});await loadReadiness(id)}catch(e){$('contentStatus').textContent=e.message}}
  window.reviewPreparedAsset=async(assetId,included)=>{const run=activeReadiness?.facts?.run_id,card=document.querySelector(`[data-prepared-asset="${CSS.escape(assetId)}"]`);if(!run||!card)return;const kind=card.querySelector('[data-prepared-asset-kind]').value;try{await post('/api/collector/runs/'+encodeURIComponent(run)+'/assets/'+encodeURIComponent(assetId)+'/review',{asset_type:kind,included});await loadReadiness(mediaProductId());$('contentStatus').textContent=included?'图片分类已保存':'图片已从本次内容素材中排除'}catch(e){$('contentStatus').textContent=e.message}};
  async function uploadPreparationAsset(event){const input=event.target,file=input.files?.[0],id=mediaProductId();if(!file||!id)return;try{const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file)});await post('/api/assets/upload',{product_id:id,run_id:activeReadiness?.facts?.run_id||'',mime_type:file.type,data_base64:data});await loadReadiness(id)}catch(e){$('contentStatus').textContent=e.message}finally{input.value=''}}
  async function loadContent() {
    ensureReadinessUI();
    ensureMediaPanel();
    activeDiagnosis=null;activeResult=null;
    activeMediaTaskId='';activeImagePlan=null;$('mediaPanel').hidden=true;
    $('imagePlanPreview')?.remove();if($('generationProgress'))$('generationProgress').hidden=true;
    $('anglePanel').hidden=true;$('platformPanel').hidden=true;$('resultPanel').hidden=true;
    const query=selected ? '?trend_id='+encodeURIComponent(selected) : '';
    const candidates=await requestJson('/api/content/candidates'+query);
    const byProduct=new Map();
    for (const x of candidates.items) {
      const old=byProduct.get(x.product_id);
      if (!old) byProduct.set(x.product_id,x);
      else if (x.trend_id && (x.default_selected || !old.default_selected)) byProduct.set(x.product_id,{...x,default_selected:x.default_selected||old.default_selected});
      else old.default_selected=old.default_selected||x.default_selected;
    }
    const items=[...byProduct.values()];
    const requested=sessionStorage.getItem('pintan.content.product');
    activeProductId=items.find(x=>x.product_id===requested)?.product_id||items[0]?.product_id||'';
    let choiceLabel=$('currentProductTitle');if(!choiceLabel){choiceLabel=document.createElement('span');choiceLabel.id='currentProductTitle';choiceLabel.className='current-product';$('productChoicePanel').querySelector('h2').append(choiceLabel)}choiceLabel.textContent=items.find(x=>x.product_id===activeProductId)?.title||'';
    const renderChoices=n=>{const page=UI.paginate(items,n);UI.save('contentPage',page.page);
      $('contentCandidates').innerHTML=page.items.map(x=>`<label class="list-card">${UI.cover(x)}<input name="content-product" class="master-choice" type="radio" ${x.product_id===activeProductId?'checked':''} data-product="${esc(x.product_id)}" data-trend="${esc(x.trend_id||'')}"><span><b>${esc(x.title)}</b><small>${esc(x.source)} · 排名 ${esc(x.rank||'—')}</small></span></label>`).join('')||'<p class="empty">暂无可选商品，请先完成选品。</p>';
      UI.pager($('contentCandidates'),page,renderChoices);
      document.querySelectorAll('.master-choice').forEach(x=>x.onchange=()=>{activeDiagnosis=null;activeResult=null;activeProductId=x.dataset.product;activeMediaTaskId='';activeImagePlan=null;$('imagePlanPreview')?.remove();$('currentProductTitle').textContent=items.find(y=>y.product_id===activeProductId)?.title||'';sessionStorage.setItem('pintan.content.product',activeProductId);sessionStorage.removeItem('pintan.content.id');$('anglePanel').hidden=true;$('platformPanel').hidden=true;$('resultPanel').hidden=true;$('mediaPanel').hidden=true;UI.steps(1);loadReadiness(activeProductId).then(()=>restoreProductContent(activeProductId)).catch(e=>$('contentStatus').textContent=e.message)});
    };
    renderChoices(UI.read('contentPage',Math.floor(items.findIndex(x=>x.product_id===activeProductId)/10)+1));
    if(activeProductId){sessionStorage.setItem('pintan.content.product',activeProductId);await loadReadiness(activeProductId);await restoreProductContent(activeProductId)}
    const pending=UI.read('contentTask',null);
    if(pending&&pending.product===activeProductId){pollTask(pending.id).then(result=>{if(pending.product!==activeProductId)return;if(pending.kind==='angle'){activeDiagnosis=result;renderAngles(result.recommendation);$('anglePanel').hidden=false;$('platformPanel').hidden=false}else{activeResult=result;renderPlatform(result);$('resultPanel').hidden=false}}).catch(e=>$('contentStatus').textContent=e.message)}
  }
  async function restoreProductContent(id){
    const data=await requestJson('/api/content/products/'+encodeURIComponent(id)+'/latest');
    if(activeProductId!==id)return;
    const requested=sessionStorage.getItem('pintan.content.id');
    activePlatform=sessionStorage.getItem('pintan.content.platform')||data.result?.platform||'';
    document.querySelectorAll('#platformChoices button').forEach(x=>x.classList.toggle('primary',x.dataset.platform===activePlatform));
    if(requested){const preview=await requestJson('/api/distribution/products/'+encodeURIComponent(id));if(preview.items.some(x=>x.content_id===requested))data.result=await requestJson('/api/content/platform/'+encodeURIComponent(requested));}
    if(data.result){activeProductId=id;activeResult=data.result;renderPlatform(activeResult);$('resultPanel').hidden=false;$('contentStatus').textContent='已恢复该商品最近的平台草稿，可查看文案和图片；可重新诊断生成新版。'}
  }
  const post=(url,body)=>requestJson(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let mediaPolling=false;
  async function pollMediaTask(id){
    if(!id)throw new Error('未返回有效媒体任务编号');
    activeMediaTaskId=id;
    mediaPolling=true;
    const taskProduct=activeProductId;
    if($('mediaCancelButton'))$('mediaCancelButton').hidden=false;
    const deadline=Date.now()+10*60*1000;
    let terminal=false;
    try{
      while(Date.now()<deadline){
        const task=await requestJson('/api/media/tasks/'+encodeURIComponent(id));
        if(taskProduct!==activeProductId)return null;
        UI.progress(task);
        const done=task.completed_slots||0,total=task.total_slots||0,workers=task.parallel_workers||0;
        const elapsed=task.created_at?Math.max(0,Math.round((Date.now()-Date.parse(task.created_at))/1000)):null;
        terminal=['completed','failed','interrupted','cancelled'].includes(task.status);
        const activity={'image-plan':'AI正在规划图片','image-classify':'正在逐张识别与分类','video-plan':'AI正在规划15秒分镜',video:'正在生成短视频镜头'}[task.kind]||'图片处理中';
        const unit=task.kind==='video'?'段':'张';
        $('mediaStatus').textContent=task.status==='queued'?'排队中…':`${activity}${workers?`（${workers}个并行任务）`:''}${total?`：已结束 ${done}/${total} ${unit}`:' '}${elapsed!==null?` · 已耗时 ${elapsed} 秒`:''}。可中断；已完成的结果保留。`;
        if(task.kind==='video'&&$('videoStatus'))$('videoStatus').textContent=$('mediaStatus').textContent;
        if(task.status==='completed')return task.result;
        if(['failed','interrupted','cancelled'].includes(task.status))throw new Error(task.error||'媒体任务已中断');
        await new Promise(resolve=>setTimeout(resolve,1000));
      }
      throw new Error('媒体任务等待超时；可使用“强行中断当前任务”后重新开始。');
    }catch(error){UI.progress({kind:'images',status:'interrupted',error:error.message});throw error}finally{
      mediaPolling=false;
      if(activeMediaTaskId===id){
        if(terminal)activeMediaTaskId='';
        if($('mediaCancelButton'))$('mediaCancelButton').hidden=terminal;
        if($('mediaResumeButton'))$('mediaResumeButton').hidden=terminal;
      }
    }
  }
  function mediaProductId(){return document.querySelector('.master-choice:checked')?.dataset.product||activeProductId}
  function renderVideoPlan(plan){
    activeVideoPlan=plan||null;
    const host=$('videoPlanPreview'),result=$('videoResult');if(!host||!result)return;
    if(!plan){host.innerHTML='';result.innerHTML='';$('videoButton').disabled=true;$('videoRetryButton').hidden=true;return}
    const failed=(plan.shots||[]).filter(x=>x.status==='failed');
    const finished=(plan.shots||[]).filter(x=>x.status==='success').length;
    const mode=plan.generation_mode==='creative'?'创意重构':'保守改造';
    host.innerHTML=`<div class="toolbar"><p><b>${esc(mode)} · ${esc(plan.aspect_ratio)} · 5段×3秒</b><br><span class="status">已完成 ${finished}/5 个镜头</span></p></div><div class="video-storyboard">${(plan.shots||[]).map(x=>`<article class="video-shot">${x.clip_url?`<video controls preload="metadata" src="${esc(x.clip_url)}"></video>`:'<div class="cover-placeholder">等待生成</div>'}<p><b>${esc(x.index)}. ${esc(x.role)}</b></p><p>${esc(x.caption)}</p><small>${esc(x.source_slot)} · ${esc(x.motion)} · ${esc(UI.status(x.status))}</small></article>`).join('')}</div>`;
    $('videoButton').disabled=mediaBusy||!activeResult||finished===5;
    $('videoPlanButton').textContent=finished===5?'重新规划5个镜头':'规划5个镜头';
    $('videoRetryButton').hidden=!failed.length;$('videoRetryButton').textContent=`重试失败镜头（${failed.length}）`;
    const final=(window.__currentMedia||[]).find(x=>x.media_type==='video'&&x.metadata?.plan_id===plan.plan_id&&x.url);
    result.innerHTML=final?`<h3>15秒成片</h3><video controls preload="metadata" src="${esc(final.url)}"></video><p><a class="button" href="${esc(final.url)}" download>下载 MP4</a> <button onclick="confirmGeneratedMedia('${esc(final.media_id)}')">确认成片</button></p>`:'';
    $('videoStatus').textContent=final?'15秒成片已生成，可预览和下载。':failed.length?'部分镜头失败，成功镜头已保留，可只重试失败项。':finished?'镜头正在分批生成，成功结果会逐段保留。':'分镜已生成，请确认后开始并行制作。';
  }
  async function loadVideoPlan(){
    if(!activeResult?.content_id){renderVideoPlan(null);return}
    try{const data=await requestJson('/api/media/video/plan?content_id='+encodeURIComponent(activeResult.content_id));renderVideoPlan(data.plan)}
    catch(e){$('videoStatus').textContent='读取视频规划失败：'+e.message}
  }
  async function planMediaVideo(){
    if(activeMediaTaskId||mediaBusy){$('videoStatus').textContent='请先等待当前媒体任务结束，或中断后再规划。';return}
    const b=$('videoPlanButton');b.disabled=true;
    const mode=document.querySelector('input[name="generationMode"]:checked')?.value||'conservative';
    try{const task=await post('/api/media/video/plan',{content_id:activeResult?.content_id,generation_mode:mode,aspect_ratio:$('videoRatio').value,force:Boolean(activeVideoPlan)});const plan=await pollMediaTask(task.task_id);renderVideoPlan(plan);$('videoStatus').textContent='分镜已完成：5个镜头，每个3秒。'}
    catch(e){$('videoStatus').textContent=e.message}finally{b.disabled=false}
  }
  function generateMediaVideo(){if(!activeVideoPlan)return planMediaVideo();return runMedia('/api/media/video',{content_id:activeResult?.content_id,plan_id:activeVideoPlan.plan_id},'正在并行生成5个视频镜头，最多同时处理2段…')}
  function retryFailedVideoShots(){if(!activeVideoPlan)return;const indexes=activeVideoPlan.shots.filter(x=>x.status==='failed').map(x=>x.index);return runMedia('/api/media/video/'+encodeURIComponent(activeVideoPlan.plan_id)+'/retry',{shot_indexes:indexes},'正在只重试失败镜头…')}
  async function loadDistribution(){
    await UI.distribution(requestJson);
  }
  async function loadMedia(){
    if($('imageButton'))$('imageButton').textContent='一键生成完整图文';
    if(!$('imagePlanButton')){const b=document.createElement('button');b.id='imagePlanButton';b.textContent='查看 AI 图片规划';$('imageButton').before(b);b.onclick=async()=>{if(activeMediaTaskId||mediaBusy){$('mediaStatus').textContent='请先等待当前任务结束，或强行中断后再规划。';return}b.disabled=true;try{const task=await post('/api/media/plan',{content_id:activeResult?.content_id});const plan=await pollMediaTask(task.task_id);activeImagePlan=plan;let panel=$('imagePlanPreview');if(!panel){panel=document.createElement('div');panel.id='imagePlanPreview';$('sourceAssets').before(panel)}panel.innerHTML='<h3>图片规划与单图模式</h3>'+(plan.slots||[]).map(x=>{const label=`${x.section}-${x.index}`;const automatic=x.automatic_generation_mode==='text_only_concept';return `<div class="plan-row"><div><b>${esc(x.section)} ${esc(x.index)}</b> · ${esc(x.purpose)}<br><small>参考素材：${x.source_asset_ids.length ? x.source_asset_ids.length+' 张' : '纯文字场景'}</small></div>${automatic?'<span class="mode-badge">自动：纯文字场景</span>':`<label>此图模式 <select class="slot-mode" data-slot="${esc(label)}"><option value="">跟随整批</option><option value="conservative">保守改造</option><option value="creative">创意重构</option></select></label>`}</div>`}).join('')+((plan.gaps||[]).length?'<p class="risk">当前规划仍有阻断：'+esc(plan.gaps.join('；'))+'</p>':'<p>已按现有素材完成规划，未要求补齐无证据款式。</p>')+((plan.omitted_notes||[]).length?'<details><summary>本次未纳入的款式能力</summary><p>'+esc(plan.omitted_notes.join('；'))+'</p></details>':'')}catch(e){$('mediaStatus').textContent=e.message}finally{b.disabled=false}}}
    const productId=mediaProductId(); if(!productId)return;
    activeProductId=productId;
    const data=await requestJson('/api/media/products/'+encodeURIComponent(productId));
    if(mediaProductId()!==productId)return;
    const usable=data.assets.filter(x=>x.preview_url&&!(x.width>0&&x.height>0&&x.width<=160&&x.height<=160)&&x.status!=='复核排除'&&!x.status.startsWith('已合并:'));
    // 只展示当前文案版本的成品；旧版含风险文案的图片留库但不混入当前结果。
    const seenMedia=new Set();
    const historicalMedia=data.media.filter(x=>x.content_id===activeResult?.content_id&&x.media_type!=='video_clip');
    data.media=data.media.filter(x=>{
      if(x.media_type==='video_clip')return false;
      if(x.content_id!==activeResult?.content_id||x.metadata?.content_version!==activeResult?.version)return false;
      const key=x.media_type+':'+x.slot;if(seenMedia.has(key))return false;seenMedia.add(key);return true;
    });
    window.__currentMedia=data.media;
    const displayMedia=data.media.filter(x=>x.media_type==='image');
    const authorized=Boolean(activeReadiness?.ready&&activeReadiness?.assets?.all_authorized);
    const reviewDetails=x=>{const history=x.metadata?.review_history||[];const resolution=x.metadata?.resolution;const profile=x.metadata?.audit_profile||{};const copies=x.metadata?.text_overlay||[];return `<details><summary>审核与修复记录（${esc(profile.label||'历史规则')} · ${history.length||x.metadata?.attempts||1}轮）</summary>${copies.length?`<p>程序排版文案：${copies.map(c=>esc(c.text)+'（'+esc(c.source_field)+'）').join('；')}</p>`:''}${history.map(r=>`<div class="step-note">${r.review_id&&r.file?`<img loading="lazy" decoding="async" src="/api/media/reviews/${esc(r.review_id)}/file" alt="第${esc(r.attempt)}轮">`:''}<b>第 ${esc(r.attempt||'—')} 轮：${r.review_skipped?'未启用AI审核':r.passed?'通过':'未通过'}${r.total_score==null?'':` · ${esc(r.total_score)}分`}</b><p>${esc((r.issues||[]).join('；')||'无阻断问题')}</p>${(r.warnings||[]).length?`<p>警告：${esc(r.warnings.join('；'))}</p>`:''}${(r.suggestions||[]).length?`<p>优化建议：${esc(r.suggestions.join('；'))}</p>`:''}${r.feedback_sent_next?`<p>已反馈下一轮：${esc(r.feedback_sent_next.required_changes||'')}</p>`:''}</div>`).join('')||`<p>${esc((x.metadata?.last_review?.issues||[]).join('；'))}</p>`}${resolution?`<h4>处理建议</h4><p>${esc(resolution.summary)}</p>${(resolution.actions||[]).map(a=>`<p>• ${esc(a)}</p>`).join('')}`:''}<p>${esc(x.metadata?.message||'')}</p></details>`};
    $('sourceAssets').innerHTML=usable.length?usable.map(x=>`<article class="media-card"><img loading="lazy" decoding="async" src="${esc(x.preview_url)}" alt="${esc(x.asset_type)}"><b>${esc(x.asset_type)}</b><p>${esc(x.status)} · ${esc(x.authorization_status)}</p><details><summary>OCR待复核文本</summary><div class="ocr">${esc(x.ocr_text||'未识别到文字')}</div></details>${x.authorization_status==='已确认'?'<p class="total">已确认使用权</p>':`<button onclick="authorizeMediaAsset('${esc(x.asset_id)}')">确认我拥有使用权</button>`}</article>`).join(''):'<p class="empty">尚无可用原图。自动采集受登录限制时，请先核对商品链接。</p>';
    UI.steps(3);$('mediaPanel').hidden=false;
    const modeName=m=>({conservative:'保守改造',creative:'创意重构',text_only_concept:'纯文字场景','text-to-image-concept':'纯文字场景','evidence-image-edit':'历史保守模式'}[m]||'历史生成');
    $('generatedMedia').innerHTML=displayMedia.length?displayMedia.map(x=>`<article class="media-card">${!x.url?`<p class="status">${x.status==='generation_failed'?'生成调用失败，未产生可发布图片':'审核未通过，已生成修复建议'}</p>`:`<img loading="lazy" decoding="async" src="${esc(x.url)}" alt="${esc(x.slot)}">`}<b>${esc(x.slot)} v${esc(x.version)}</b><p><span class="mode-badge">${esc(modeName(x.metadata?.generation_mode))}</span></p><p>${UI.status(x.status)}${x.confirmed?' · 已确认':''}</p>${x.metadata?.input_warning?`<p class="status">${esc(x.metadata.input_warning)}</p>`:''}${x.metadata?.last_review?.error_message?`<p class="status">${esc(x.metadata.last_review.error_message)}</p>`:''}${x.url?`<a class="button" href="${esc(x.url)}" download>下载</a> <button onclick="confirmGeneratedMedia('${esc(x.media_id)}')">确认成品</button>`:'' }${reviewDetails(x)} <button onclick="retryMediaImage('${esc(x.media_id)}')">按原模式重新生成</button></article>`).join(''):'<p class="empty">尚未生成图片</p>';
    document.querySelectorAll('#generatedMedia .media-card').forEach((card,index)=>{
      const item=displayMedia[index];
      const history=historicalMedia.filter(x=>x.media_id!==item.media_id&&x.slot===item.slot&&x.media_type===item.media_type&&x.url);
      if(history.length){const details=document.createElement('details');details.innerHTML=`<summary>历史版本（${history.length}）</summary>`+history.map(x=>`<p><a href="${esc(x.url)}" target="_blank" rel="noopener">第 ${esc(x.version)} 版 · ${UI.status(x.status)}</a></p>`).join('');card.append(details)}
      const input=document.createElement('textarea');input.maxLength=1000;input.placeholder='输入改进意见，例如：商品放大，背景更简洁，保留印花';input.style.cssText='width:100%;min-height:75px;margin-top:12px';card.append(input);
      const regenerate=document.createElement('button');regenerate.textContent='根据意见重新生成';card.append(regenerate);
      regenerate.onclick=()=>{if(!input.value.trim()){input.focus();return}return runMedia('/api/media/images/'+encodeURIComponent(item.media_id)+'/retry',{user_feedback:input.value.trim()},'正在根据你的意见生成新版本…')};
      if(item.metadata?.parent_media_id){const previous=document.createElement('a');previous.href='/api/media/files/'+encodeURIComponent(item.metadata.parent_media_id);previous.target='_blank';previous.rel='noopener';previous.textContent='查看上一版';card.append(previous)}
    });
    UI.collapse('resultPanel',!activeResult?.issues?.length&&activeResult?.risk_level!=='高');
    $('imageButton').disabled=mediaBusy||!authorized||!activeResult;
    await loadVideoPlan();
    if(!activeMediaTaskId&&activeResult?.content_id){
      const pending=await requestJson('/api/media/tasks?include_finished=true&content_id='+encodeURIComponent(activeResult.content_id));
      activeMediaTaskId=pending.items?.find(x=>['queued','running'].includes(x.status))?.task_id||'';
      const latest=activeMediaTaskId||pending.items?.[0]?.task_id;
      if(latest)UI.progress(await requestJson('/api/media/tasks/'+encodeURIComponent(latest)));
    }
    if($('mediaCancelButton'))$('mediaCancelButton').hidden=!activeMediaTaskId;
    if($('mediaResumeButton'))$('mediaResumeButton').hidden=!activeMediaTaskId;
    if(activeMediaTaskId){
      $('imageButton').disabled=true;
      $('mediaStatus').textContent='当前有未结束的媒体任务，可恢复查看进度或强行中断。';
      if(!mediaPolling&&!mediaBusy)pollMediaTask(activeMediaTaskId).then(()=>loadMedia()).catch(e=>$('mediaStatus').textContent=e.message);
    }else $('mediaStatus').textContent=authorized?'原图已授权，可生成平台图片':usable.length?'请确认拥有原图使用权':'等待采集原图';
  }
  async function runMedia(url,body,message){
    if(activeMediaTaskId){$('mediaStatus').textContent='请先等待当前任务结束，或强行中断后再生成。';return}
    if(mediaBusy)return; mediaBusy=true; $('mediaStatus').textContent=message;
    let failure='';
    try{const task=await post(url,body);await pollMediaTask(task.task_id);await loadMedia();}
    catch(e){failure=e.message}
    finally{mediaBusy=false;await loadMedia().catch(()=>{});if(failure)$('mediaStatus').textContent=failure}
  }
  window.cancelCurrentMediaTask=async()=>{
    if(!activeMediaTaskId){$('mediaStatus').textContent='当前没有可中断的媒体任务。';return}
    const id=activeMediaTaskId;
    if(!confirm('确认强行中断当前图片规划或生成任务？已完成的历史图片不会删除。'))return;
    try{
      const result=await post('/api/media/tasks/'+encodeURIComponent(id)+'/cancel',{});
      if(activeMediaTaskId===id)activeMediaTaskId='';
      if($('mediaCancelButton'))$('mediaCancelButton').hidden=true;
      if($('mediaResumeButton'))$('mediaResumeButton').hidden=true;
      await loadMedia();
      $('mediaStatus').textContent=result.message||'已强行中断当前任务。';
    }catch(e){$('mediaStatus').textContent=e.message}
  };
  window.collectMediaAssets=()=>runMedia('/api/assets/collect',{product_id:mediaProductId()},'正在读取1688商品图片…');
  window.uploadMediaAsset=async input=>{const file=input.files?.[0];if(!file)return;if(file.size>10*1024*1024){$('mediaStatus').textContent='图片不能超过10MB';return}try{const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file)});$('mediaStatus').textContent='正在校验、OCR并抠图…';await post('/api/assets/upload',{product_id:mediaProductId(),mime_type:file.type,data_base64:data});await loadMedia()}catch(e){$('mediaStatus').textContent=e.message}finally{input.value=''}};
  window.authorizeMediaAsset=async id=>{if(!confirm('请确认：你拥有该图片的使用权，并同意用于生成营销素材。'))return;try{await post('/api/assets/'+encodeURIComponent(id)+'/authorize',{confirmed:true,statement:'我已确认拥有该图片的使用权'});await loadMedia()}catch(e){$('mediaStatus').textContent=e.message}};
  window.generateMediaImages=()=>{const level=$('auditLevel')?.value||'normal';const labels={none:'无规则',light:'轻度规则',normal:'正常规则',strict:'严谨规则'};const mode=document.querySelector('input[name="generationMode"]:checked')?.value||'conservative';const slot_modes=Object.fromEntries([...document.querySelectorAll('.slot-mode')].filter(x=>x.value).map(x=>[x.dataset.slot,x.value]));return runMedia('/api/media/images',{content_id:activeResult?.content_id,audit_level:level,generation_mode:mode,slot_modes},`正在按${mode==='creative'?'创意重构':'保守改造'}生成、排版并审核 · ${labels[level]}…`)};
  window.retryMediaImage=id=>runMedia('/api/media/images/'+encodeURIComponent(id)+'/retry',{},'正在重试单张图片…');
  window.planMediaVideo=planMediaVideo;window.generateMediaVideo=generateMediaVideo;window.retryFailedVideoShots=retryFailedVideoShots;
  window.confirmGeneratedMedia=async id=>{try{await post('/api/media/'+encodeURIComponent(id)+'/confirm',{});await loadMedia()}catch(e){$('mediaStatus').textContent=e.message}};
  const resultObserver=new MutationObserver(()=>{if(!$('resultPanel').hidden){
    let checklist=$('publicationChecklist');if(!checklist){checklist=document.createElement('div');checklist.id='publicationChecklist';$('resultPanel').append(checklist)}
    const profile=activeResult?.listing_profile||{};
    checklist.innerHTML=`<h3>发布前检查${profile.workbench?' · '+esc(profile.workbench):''}</h3><p>${esc(profile.source_type||'项目模板，待平台后台复核')}</p>${profile.source_url?`<a href="${esc(profile.source_url)}" target="_blank" rel="noopener noreferrer">发布参考资料</a>`:''}${(activeResult?.publication_checklist||[]).map(x=>`<p>□ ${esc(x)}</p>`).join('')}<p>图文为待审核上架素材，不会自动发布到店铺。</p>`;
    $('mediaPanel').hidden=false;loadMedia().catch(e=>$('mediaStatus').textContent=e.message)}});
  ensureMediaPanel(); resultObserver.observe($('resultPanel'),{attributes:true,attributeFilter:['hidden']});
  window.diagnoseSelected=async()=>{const id=mediaProductId();if(!id)return;$('diagnoseButton').disabled=true;$('contentStatus').textContent='AI正在诊断内容风格…';try{const task=await post('/api/tasks/content/diagnose',{product_id:id,trend_id:document.querySelector('.master-choice:checked')?.dataset.trend||null});activeDiagnosis=await pollTask(task.task_id);renderAngles(activeDiagnosis.recommendation);$('anglePanel').hidden=false;$('platformPanel').hidden=false;UI.steps(2);$('contentStatus').textContent='请选择推荐风格和目标平台';}catch(e){$('contentStatus').textContent=e.message}finally{$('diagnoseButton').disabled=false}};
  function renderAngles(r){const all=[{...r.primary,label:'主推荐'},...r.alternatives.map(x=>({...x,label:'备选'}))];$('angleCards').innerHTML=all.map((x,i)=>`<label class="module"><input type="checkbox" class="angle-choice" value="${esc(x.type)}" ${i===0?'checked':''}><span class="number">${esc(x.label)} · ${Math.round(x.confidence*100)}%</span><b>${esc(x.type)}</b><span>${esc(x.reason)}</span><details><summary>推荐依据</summary><small>综合 ${x.evidence_claim_ids.length} 项已确认商品事实</small></details></label>`).join('')+`<div class="card"><b>暂不建议</b><p>${r.not_recommended.map(x=>esc(x.type||x.angle||'')+'：'+esc(x.reason||'')).join('<br>')}</p><small>素材缺口：${esc(r.material_gaps.join('；'))}</small></div>`}
  document.querySelectorAll('#platformChoices button').forEach(b=>b.onclick=()=>{activePlatform=b.dataset.platform;document.querySelectorAll('#platformChoices button').forEach(x=>x.classList.toggle('primary',x===b));$('platformButton').disabled=false});
  window.generatePlatform=async()=>{const angles=[...document.querySelectorAll('.angle-choice:checked')].map(x=>x.value);if(!activeDiagnosis||(!angles.length&&!$('customAngle').value.trim())||!activePlatform)return;$('platformButton').disabled=true;$('contentStatus').textContent='正在生成并检查平台内容…';try{const choice=await post('/api/content/choices',{diagnosis_id:activeDiagnosis.diagnosis_id,selected_angles:angles,custom_angle:$('customAngle').value});const task=await post('/api/tasks/content/platform',{diagnosis_id:activeDiagnosis.diagnosis_id,choice_id:choice.choice_id,platform:activePlatform});activeResult=await pollTask(task.task_id);renderPlatform(activeResult);$('resultPanel').hidden=false;$('contentStatus').textContent='平台内容生成完成';}catch(e){$('contentStatus').textContent=e.message}finally{$('platformButton').disabled=false}};
  function renderPlatform(r){if($('platformReadable'))$('platformReadable').innerHTML=Object.entries(r.fields).map(([k,v])=>`<section class="copy-field"><b>${esc(k)}</b><p>${esc(UI.readable(v))}</p></section>`).join('');$('ruleMeta').innerHTML=`${esc(r.platform)} · 规则 ${esc(r.rule.version)} · ${esc(r.rule.source_type)} · 核验 ${esc(r.rule.verified_at)} ${r.rule.source_url?`· <a href="${esc(r.rule.source_url)}" target="_blank" rel="noopener">查看来源</a>`:''}`;$('visibleResult').innerHTML=Object.entries(r.fields).map(([k,v])=>`<label style="display:block;margin:14px 0"><b>${esc(k)}</b><textarea class="platform-field" data-key="${esc(k)}" style="display:block;width:100%;min-height:90px;margin-top:6px;padding:10px">${esc(typeof v==='string'?v:JSON.stringify(v,null,2))}</textarea></label>`).join('');$('riskResult').innerHTML=`<p>风险等级：<b>${esc(r.risk_level)}</b>　状态：${esc(r.status)}</p>`+(r.issues.length?r.issues.map(x=>`<p class="badge">${esc(x.rule)}：${esc(x.text)}</p>`).join(''):'<p class="status">确定性扫描未发现高风险表达</p>')+(r.changes.length?`<details><summary>查看模型修改记录</summary>${r.changes.map(x=>`<p>${esc(typeof x==='string'?x:JSON.stringify(x))}</p>`).join('')}</details>`:'')}
  function editedFields(){return Object.fromEntries([...document.querySelectorAll('.platform-field')].map(x=>[x.dataset.key,x.value]))}
  window.savePlatformVersion=async()=>{if(!activeResult)return;try{activeResult=await post(`/api/content/platform/${activeResult.content_id}/versions`,editedFields());renderPlatform(activeResult);$('contentStatus').textContent='已保存为新版本'}catch(e){$('contentStatus').textContent=e.message}};
  window.confirmPlatformVersion=async()=>{if(!activeResult)return;try{activeResult=await requestJson(`/api/content/platform/${activeResult.content_id}/confirm`,{method:'POST'});renderPlatform(activeResult);$('contentStatus').textContent='当前平台版本已确认'}catch(e){$('contentStatus').textContent=e.message}};
  loadStable().catch(e => $('stableStatus').textContent = e.message);
  requestJson('/api/trends').then(async data => {
    $('trendList').innerHTML = data.items.map(x => `<button class="trend-item" data-id="${esc(x.trend_id)}">${UI.cover(x)}<span>${esc(x.platform)} · ${esc(x.title)}</span></button>`).join('');
    document.querySelectorAll('.trend-item').forEach(b => b.onclick = () => selectTrend(b.dataset.id).catch(e => $('trendStatus').textContent = e.message));
    if (data.items[0]) await selectTrend(data.items[0].trend_id);
  }).catch(e => $('trendStatus').textContent = e.message);
}
