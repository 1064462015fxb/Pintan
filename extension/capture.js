/* 从商品区域读取证据；扩展隔离世界执行，不读取Cookie或站点存储。 */
window.pintanCapture=async function(mode='full'){
  window.__pintanCaptureCancel=false;
  const originalUrl=location.href, position={x:scrollX,y:scrollY}, restore=[];
  const CAPTURE_VERSION='2.7.0',textOnly=mode==='text_only';
  const deadline=Date.now()+(textOnly?45000:180000), pause=ms=>new Promise(r=>setTimeout(r,ms));
  const roots=[['主图','[class*="gallery"], [id*="gallery"], [class*="main-img"], [class*="mainImg"]'],
    ['SKU图','[class*="skuSelection"], [id*="skuSelection"], [class*="sku-list"], [class*="skuList"]'],
    ['详情图','[class*="description"], [id*="description"], #desc-lazyload-container, #offer-template-0'],
    ['评论图','[class*="review"], [class*="evaluation"], [id*="review"], [id*="evaluation"]']];
  const excluded='[class*="recommend"], [class*="Recommend"], [class*="avatar"], [class*="shopSign"], header, footer';
  const images=new Map(),skus=new Map(),diagnostics=[],skipped=new Set();
  const lazyAttrs=['src','data-src','data-lazy-src','data-lazyload-src','data-ks-lazyload','data-original','data-original-src','data-image','data-url'];
  const srcsetAttrs=['srcset','data-srcset','data-lazy-srcset'];
  let detailObserver=null,detailMutations=0;
  const visible=node=>!!node&&node.getClientRects().length>0&&getComputedStyle(node).visibility!=='hidden';
  // 不使用页面第一个 h1：1688 店铺装修常将公司名称放在 h1 中。
  function productTitle(){
    const valid=text=>text.length>2&&text.length<=300&&
      !/^(?:商品评价|商品详情|商品属性|包装信息|规格参数|产品参数|价格说明|采购须知|店铺信息|公司介绍)$/.test(text)&&
      !/(?:有限公司|有限责任公司|旗舰店|专营店)$/.test(text);
    const selectors=['[class*="title-text"]','[class*="offer-title"]','[class*="offerTitle"]','[data-title="商品标题"]'];
    for(const selector of selectors)for(const node of document.querySelectorAll(selector)){
      const text=(node.innerText||'').trim();
      if(visible(node)&&!node.closest(excluded)&&valid(text))return {text,source_locator:selector};
    }
    // 新版1688部分商品标题不在稳定DOM节点中；使用页面元数据/标签标题
    // 作为只读证据，并去掉平台后缀。泛化栏目标题仍会被上面的白名单拒绝。
    for(const selector of ['meta[property="og:title"]','meta[name="title"]']){
      const text=(document.querySelector(selector)?.content||'').trim();if(valid(text))return {text,source_locator:selector};
    }
    const pageTitle=(document.title||'').replace(/\s*[-_–—]\s*(?:阿里巴巴|1688).*$/,'').trim();
    if(valid(pageTitle))return {text:pageTitle,source_locator:'document.title'};
    // h1最后兜底，避免先命中详情区的“包装信息”等栏目标题。
    for(const node of document.querySelectorAll('h1')){
      const text=(node.innerText||'').trim();
      if(visible(node)&&!node.closest(excluded)&&valid(text))return {text,source_locator:'h1'};
    }
    return null;
  }
  function stop(){
    if(window.__pintanCaptureCancel||location.href!==originalUrl)throw Error('page_changed');
    if(Date.now()>deadline)throw Error('capture_timeout');
    if([...document.querySelectorAll('#nc_1_wrapper, #baxia-dialog-content, [id*="captcha"]')].some(visible)
      ||/请完成验证|滑动验证|访问过于频繁/.test(document.body.innerText))throw Error('verification_required');
    if([...document.querySelectorAll('iframe[src*="login"], [class*="login-dialog"]')].some(visible))throw Error('login_required');
  }
  function add(raw,region,selector,method,sku=''){
    let url;try{url=new URL(raw,location.href);}catch{return;}
    if(url.protocol!=='https:'||!['cbu01.alicdn.com','img.alicdn.com'].includes(url.hostname)||/\.svg(?:\?|$)/.test(url.href))return;
    // 评论实拍属于可用素材，但不是商品详情区证据，统一进入待分类素材。
    const storedRegion=region==='评论图'?'待分类商品图':region;
    const key=storedRegion+'|'+url.href;
    if(images.size<300)images.set(key,{url:url.href,region:storedRegion,source_locator:selector.slice(0,500),discovery:method,sku:sku.slice(0,500)});
    else skipped.add(region);
  }
  function optionDimension(node){
    const known=/^(?:颜色|颜色分类|规格|款式|型号|材质|尺寸)$/;
    for(let current=node.parentElement,depth=0;current&&depth<5;current=current.parentElement,depth++){
      for(let sibling=current.previousElementSibling;sibling;sibling=sibling.previousElementSibling){
        const text=(sibling.innerText||'').trim();if(known.test(text))return text;
        if(text.length>20)break;
      }
      const label=(current.getAttribute('aria-label')||current.getAttribute('data-label')||'').trim();
      if(known.test(label))return label;
    }
    return 'SKU选项';
  }
  function optionImage(node){
    const image=node.querySelector('img');
    return image?.currentSrc||image?.getAttribute('data-src')||image?.src||'';
  }
  function optionText(node){
    const image=node.querySelector?.('img');
    return (node.innerText||node.textContent||node.getAttribute?.('title')||
      node.getAttribute?.('aria-label')||image?.getAttribute?.('alt')||'').trim();
  }
  function selectedOption(node,root){
    for(let current=node;current&&current!==root.parentElement;current=current.parentElement){
      if(current.getAttribute?.('aria-selected')==='true'||current.getAttribute?.('aria-pressed')==='true'||
        /selected|active|checked/i.test(String(current.className||'')))return true;
      if(current===root)break;
    }
    return false;
  }
  function skuOptionNodes(){
    /* 1688新版SKU经常用无语义div/span承载选项，既没有button也没有
     * role=option。以SKU区的可见逐行文字为边界，再反查“文本完全相等”
     * 的最深DOM节点；点击该节点会按正常冒泡触发页面自己的选择处理。 */
    const result=[],seen=new Set(),known=/^(?:颜色|颜色分类|规格|款式|型号|材质|尺寸)$/;
    for(const root of document.querySelectorAll(roots[1][1])){
      if(!visible(root)||root.closest(excluded))continue;
      let dimension='SKU选项';const entries=[];
      for(const line of (root.innerText||'').split(/\r?\n/).map(x=>x.trim()).filter(Boolean)){
        if(known.test(line)){dimension=line;continue;}
        if(/^[¥￥]\s*\d|^库存\s*\d|^[+\-]?\d+$/.test(line)||line.length>180)continue;
        entries.push({text:line,dimension});
      }
      const descendants=[...root.querySelectorAll('*')];
      for(const entry of entries){
        const matches=descendants.filter(node=>visible(node)&&!node.closest(excluded)&&optionText(node)===entry.text);
        // DOM越深越接近真实可点击项；若叶节点只是span，click仍会向父级冒泡。
        const node=matches.sort((a,b)=>b.querySelectorAll('*').length-a.querySelectorAll('*').length).pop();
        if(!node)continue;
        let holder=node;
        for(let depth=0;holder.parentElement&&holder.parentElement!==root&&depth<3;depth++){
          const parent=holder.parentElement,txt=optionText(parent);
          if(txt!==entry.text)break;
          holder=parent;
        }
        const key=entry.dimension+'|'+entry.text;if(seen.has(key))continue;seen.add(key);
        result.push({node:holder,root,text:entry.text,dimension:entry.dimension,
          source_locator:'sku-root exact-text'});
      }
    }
    return result;
  }
  function displayedHero(){
    const selector=roots[0][1],candidates=[];
    for(const root of document.querySelectorAll(selector))for(const image of root.querySelectorAll('img')){
      if(!visible(image)||image.closest(excluded))continue;
      const box=image.getBoundingClientRect(),url=image.currentSrc||image.getAttribute('data-src')||image.src||'';
      if(url)candidates.push({url,score:Math.max(0,box.width)*Math.max(0,box.height),locator:selector});
    }
    candidates.sort((a,b)=>b.score-a.score);return candidates[0]||null;
  }
  async function stableHero(){
    let prior='',same=0,current=null;
    for(let i=0;i<10;i++){
      await pause(180);stop();current=displayedHero();const url=current?.url||'';
      if(url&&url===prior){if(++same>=2)return current}else same=0;
      prior=url;
    }
    return current;
  }
  const hasUrl=raw=>{
    try{const href=new URL(raw,location.href).href;return [...images.values()].some(item=>item.url===href);}catch{return true;}
  };
  function candidateUrls(node){
    const found=[];
    for(const key of [...lazyAttrs,'poster','data-poster']){
      const raw=node.getAttribute?.(key);if(raw&&(node.tagName==='IMG'||node.tagName==='SOURCE'||node.tagName==='VIDEO'||key!=='src'))found.push([raw,key]);
    }
    if(node.currentSrc)found.push([node.currentSrc,'currentSrc']);
    for(const attr of srcsetAttrs)for(const match of (node.getAttribute?.(attr)||'').matchAll(/((?:https?:)?\/\/[^\s,]+)/g))found.push([match[1],attr]);
    try{
      const view=node.ownerDocument?.defaultView,style=view?.getComputedStyle?view.getComputedStyle(node):getComputedStyle(node);
      for(const match of (style?.backgroundImage||'').matchAll(/url\(["']?([^"')]+)["']?\)/g))found.push([match[1],'CSS背景图']);
    }catch{/* 已卸载节点无需处理 */}
    return found;
  }
  /* 新版详情组件可能没有稳定类名，或滚动后从DOM卸载。
   * 因此回收浏览器已实际看到的阿里图片资源；缺少区域证据时只标“待分类商品图”。 */
  function scanFallback(){
    stop();let domAdded=0,resourceAdded=0,posterAdded=0,shadowRoots=0;
    const visited=new Set();
    const scanRoot=(root,label)=>{
      if(!root||visited.has(root))return;visited.add(root);
      const nodes=[...root.querySelectorAll('img, picture source, video[poster], [data-poster], [style], [data-src], [data-lazy-src], [data-lazyload-src], [data-ks-lazyload], [data-original], [data-original-src], [data-image], [data-url], [data-srcset], [data-lazy-srcset]')];
      for(const node of nodes){
        if(node.closest?.(excluded))continue;
        for(const [raw,method] of candidateUrls(node)){
          if(hasUrl(raw))continue;
          const before=images.size;add(raw,'待分类商品图',label,method);
          if(images.size>before){domAdded++;if(/poster/i.test(method))posterAdded++;}
        }
      }
      for(const host of root.querySelectorAll('*'))if(host.shadowRoot){shadowRoots++;scanRoot(host.shadowRoot,label+' open-shadow');}
    };
    scanRoot(document,'document fallback');
    for(const entry of (globalThis.performance?.getEntriesByType?.('resource')||[])){
      const raw=String(entry?.name||'');let url;try{url=new URL(raw,location.href);}catch{continue;}
      if(!['cbu01.alicdn.com','img.alicdn.com'].includes(url.hostname)||!/\.(?:jpe?g|png|webp)(?:_|$|\?)/i.test(url.href)||hasUrl(url.href))continue;
      const before=images.size;add(url.href,'待分类商品图','performance.resource','Performance资源');
      if(images.size>before)resourceAdded++;
    }
    return {domAdded,resourceAdded,posterAdded,shadowRoots};
  }
  function scan(){
    stop();
    for(const [region,selector] of roots)for(const root of document.querySelectorAll(selector)){
      if(root.closest(excluded))continue;
      const sku=region==='SKU图'?(root.querySelector('[aria-selected="true"], [aria-pressed="true"], [class*="selected"]')?.textContent||''):'';
      for(const node of [root,...root.querySelectorAll('img, picture source, [style], [data-src], [data-lazy-src], [data-lazyload-src], [data-ks-lazyload], [data-original], [data-original-src], [data-image], [data-url], [data-srcset], [data-lazy-srcset]')]){
        if(node.closest(excluded))continue;
        for(const key of lazyAttrs){
          const raw=node.getAttribute(key);if(raw&&(node.tagName==='IMG'||key!=='src'))add(raw,region,selector,key,sku);
        }
        if(node.currentSrc)add(node.currentSrc,region,selector,'currentSrc',sku);
        for(const attr of srcsetAttrs)for(const match of (node.getAttribute(attr)||'').matchAll(/((?:https?:)?\/\/[^\s,]+)/g))add(match[1],region,selector,attr,sku);
        const view=node.ownerDocument?.defaultView,style=view?.getComputedStyle?view.getComputedStyle(node):getComputedStyle(node);
        for(const match of (style?.backgroundImage||'').matchAll(/url\(["']?([^"')]+)["']?\)/g))add(match[1],region,selector,'CSS背景图',sku);
      }
      // 同源详情iframe允许按浏览器同源规则读取；跨域时只记录限制，不尝试绕过。
      if(region==='详情图')for(const frame of root.querySelectorAll('iframe')){
        try{
          const doc=frame.contentDocument;if(!doc?.documentElement)continue;
          for(const node of doc.querySelectorAll('img, picture source, [style], [data-src], [data-lazy-src], [data-ks-lazyload], [data-original]')){
            for(const key of lazyAttrs){const raw=node.getAttribute(key);if(raw)add(raw,region,selector+' iframe',key);}
            for(const attr of srcsetAttrs)for(const match of (node.getAttribute(attr)||'').matchAll(/((?:https?:)?\/\/[^\s,]+)/g))add(match[1],region,selector+' iframe',attr);
          }
        }catch{skipped.add('详情图');}
      }
    }
  }
  try{
    stop();
    for(let i=0;i<20;i++){
      if(document.querySelector('h1, [class*="title-text"]')&&roots.some(([,q])=>document.querySelector(q)))break;
      await pause(500);stop();
    }
    const titleEvidence=productTitle();
    if(!roots.some(([,q])=>document.querySelector(q)))throw Error('unrecognized_page');
    if(!titleEvidence)diagnostics.push('商品标题：未找到可靠商品标题；公司名称不能代替商品名称');
    scan();
    if(textOnly){
      // 批量预筛只需评分事实。一次读取页面文本并限制区域节点数量，避免在大型
      // 详情DOM中对每个div反复计算innerText造成分钟级阻塞。
      const pageText=(document.body?.innerText||'').slice(0,40000),text_blocks=[];
      if(titleEvidence)text_blocks.push({region:'title',...titleEvidence});
      const fastRoots=[['price','[class*="price-component"], [class*="priceComponent"], [class*="price-range"]'],
        ['sku',roots[1][1]],['attributes','[class*="attribute"], [class*="Attribute"], [class*="product-prop"]'],
        ['packaging','[class*="packaging"], [class*="package-info"]'],
        ['supplier','[class*="supplier"], [class*="shop-info"], [class*="company-info"]']];
      const seenText=new Set();
      for(const [region,selector] of fastRoots)for(const node of [...document.querySelectorAll(selector)].slice(0,12)){
        const text=(node.innerText||'').trim().slice(0,8000);if(!text||seenText.has(region+'|'+text))continue;
        seenText.add(region+'|'+text);text_blocks.push({region,text,source_locator:selector});
      }
      diagnostics.push('快速文字模式：单次读取页面文本；跳过SKU点击、全DOM文本扫描、详情滚动、OCR和视觉处理');
      const regions=roots.filter(([region])=>region!=='评论图').map(([region,selector])=>{
        const recognized=!!document.querySelector(selector),found=[...images.values()].filter(i=>i.region===region).length;
        return {region,recognized,complete:false,found,reason:'快速文字阶段仅保存当前可见引用，选品后再完整采集'};
      });
      return {capture_version:CAPTURE_VERSION,page_url:originalUrl,title:titleEvidence?.text||'',visible_text:pageText,
        text_blocks:text_blocks.slice(0,100),images:[...images.values()],skus:[],regions,diagnostics:diagnostics.slice(0,20)};
    }
    // 主图缩略图仍使用稳定区域选择器，只为触发全部轮播资源加载。
    const gallerySelector='[class*="gallery"] [class*="thumbnail"]';
    const galleryNodes=[...document.querySelectorAll(gallerySelector)].filter(n=>visible(n)&&!n.closest(excluded)&&!n.disabled);
    const selectedGallery=galleryNodes.find(n=>selectedOption(n,n.parentElement));
    if(selectedGallery)restore.push(selectedGallery);
    if(!textOnly)for(const node of galleryNodes.slice(0,100)){stop();node.click();await stableHero();scan();}

    // SKU关系来自真实选择状态；缩略图仅作证据，切换后的主图才是高清生产素材。
    const skuOptions=skuOptionNodes();
    const selectedSku=skuOptions.filter(x=>selectedOption(x.node,x.root));
    restore.push(...selectedSku.map(x=>x.node).filter(n=>!restore.includes(n)));
    if(!skuOptions.length){skipped.add('SKU图');diagnostics.push('SKU图：识别到区域，但没有定位到可点击选项');}
    else if(!selectedSku.length)diagnostics.push('SKU图：未识别原选择状态；已采集选项关系，但无法恢复原选择');
    for(const item of skuOptions.slice(0,100)){
      stop();const {node,text,dimension,source_locator}=item;
      if(/购买|下单|客服|收藏|铺货|采购|购物车/.test(text))continue;
      if(textOnly){
        const direct=optionImage(node);if(direct)add(direct,'SKU图',source_locator,'SKU选项图',text);
        skus.set(dimension+'|'+text,{text:text.slice(0,1000),dimension,source_locator,image_url:direct,
          image_relation_type:direct?'option_image':'text_only_option',displayed_image_changed:false});continue;
      }
      const before=displayedHero()?.url||'',direct=optionImage(node);
      node.click();const current=await stableHero();scan();
      const displayed=current?.url||'',imageUrl=displayed||direct;
      if(displayed)add(displayed,'SKU图',source_locator,'SKU选中后主图',text);
      if(direct)add(direct,'SKU图',source_locator,'SKU选项图',text);
      skus.set(dimension+'|'+text,{text:text.slice(0,1000),dimension,source_locator,
        image_url:imageUrl,image_relation_type:(displayed?'selected_main_image':'option_image'),
        displayed_image_changed:!!displayed&&displayed!==before});
    }
    const detail=document.querySelector(roots[2][1]);
    if(detail&&!textOnly){
      if(typeof MutationObserver!=='undefined'){
        detailObserver=new MutationObserver(records=>{detailMutations+=records.length});
        detailObserver.observe(detail,{subtree:true,childList:true,attributes:true,attributeFilter:[...lazyAttrs,...srcsetAttrs,'style','class']});
      }
      let steps=0;
      let y=detail.getBoundingClientRect().top+scrollY,stableRounds=0,lastBottom=0,lastCount=images.size;
      while(steps<150){
        if(++steps>150){skipped.add('详情图');diagnostics.push('详情图：达到安全滚动上限');break;}
        stop();scrollTo(0,y);await pause(400);scan();
        const bottom=detail.getBoundingClientRect().bottom+scrollY;
        if(y>=bottom-650){
          await pause(600);scan();
          const newBottom=detail.getBoundingClientRect().bottom+scrollY;
          stableRounds=(Math.abs(newBottom-lastBottom)<20&&images.size===lastCount)?stableRounds+1:0;
          lastBottom=newBottom;lastCount=images.size;
          if(stableRounds>=2)break;
        }
        y=Math.min(y+650,Math.max(y,bottom));
      }
      if(detailMutations)diagnostics.push('详情图：滚动期间发现 '+detailMutations+' 次懒加载DOM更新，已完成回扫');
      if(detail.querySelector('iframe'))diagnostics.push('详情图：iframe仅按同源规则读取；跨域内容无法访问');
    }
    scan();
    const fallback=textOnly?{domAdded:0,resourceAdded:0,posterAdded:0,shadowRoots:0}:scanFallback();
    if(textOnly)diagnostics.push('快速文字模式：未切换轮播图、未滚动详情图、未下载或分析图片');
    diagnostics.push(`资源回收：DOM新增${fallback.domAdded}张、Performance新增${fallback.resourceAdded}张、视频封面${fallback.posterAdded}张、开放Shadow DOM ${fallback.shadowRoots}个`);
    const sections=[],text_blocks=[],seenBlocks=new Set();let textBudget=80000;
    const textRoots=[['price','[class*="price-component"], [class*="priceComponent"], [class*="price-range"]'],
      ['sku',roots[1][1]],['attributes','[class*="attribute"], [class*="Attribute"], [class*="product-prop"]'],
      ['packaging','[class*="packaging"], [class*="package-info"]'],
      ['supplier','[class*="supplier"], [class*="shop-info"], [class*="company-info"]'],
      ['reviews','[class*="review"], [class*="evaluation"]'],['detail',roots[2][1]]];
    if(titleEvidence)text_blocks.push({region:'title',...titleEvidence});
    const textCounts={};
    for(const [region,selector] of textRoots){
      let count=0;
      for(const node of document.querySelectorAll(selector)){
        if(node.closest(excluded)||!visible(node))continue;
        const text=(node.innerText||'').trim(),key=region+'|'+text;
        if(!text||seenBlocks.has(key)||text_blocks.length>=100||textBudget<=0)continue;
        seenBlocks.add(key);const kept=text.slice(0,Math.min(8000,textBudget));textBudget-=kept.length;
        sections.push(kept);text_blocks.push({region,text:kept,source_locator:selector});count++;
        if(kept.length<text.length)diagnostics.push(region+'：区域文字超过长度上限，已标记截断');
      }
      textCounts[region]=count;
      if(!count&&region!=='supplier')diagnostics.push(region+'：未读取到可见文字，字段保留为空');
    }
    // 1688新版商家卡经常使用哈希类名，不能依赖 supplier/shop-info。
    // 在 document 与开放 Shadow DOM 中寻找同时包含至少两个商家指标的
    // 小型可见文本块；只读取文字，不读取 Cookie、存储或请求头。
    const supplierSignals=/供应商|有限公司|入驻\s*\d+\s*年|主营\s*[:：]|品质达标率|支撑率|店铺回头率|揽收率|代发热度|代发数量|铺货分销商|下游铺货|商家榜|送至|承诺\s*\d+\s*小时发货|包邮|包赔|晚发必赔|退货包运费|无理由退货|限时\s*\d+(?:\.\d+)?\s*折|首单减|先采后付/g;
    const rootQueue=[document],visitedTextRoots=new Set(),supplierCandidates=[];
    while(rootQueue.length){
      const root=rootQueue.shift();if(!root||visitedTextRoots.has(root))continue;visitedTextRoots.add(root);
      for(const host of root.querySelectorAll('*'))if(host.shadowRoot)rootQueue.push(host.shadowRoot);
      for(const node of root.querySelectorAll('section, article, div')){
        if(node.closest?.(excluded)||!visible(node))continue;
        const text=(node.innerText||node.textContent||'').trim();
        if(text.length<12||text.length>1800)continue;
        const hits=new Set(text.match(supplierSignals)||[]);
        if(hits.size>=2)supplierCandidates.push({text,node});
      }
    }
    supplierCandidates.sort((a,b)=>a.text.length-b.text.length);
    let anchored=0;
    for(const item of supplierCandidates){
      if(anchored>=8||textBudget<=0)break;
      const key='supplier|'+item.text;
      if(seenBlocks.has(key)||[...seenBlocks].some(x=>x.startsWith('supplier|')&&(x.endsWith(item.text)||item.text.endsWith(x.slice(9)))))continue;
      const kept=item.text.slice(0,Math.min(1800,textBudget));textBudget-=kept.length;
      seenBlocks.add(key);sections.push(kept);text_blocks.push({region:'supplier',text:kept,source_locator:'text-anchor:supplier-card'});anchored++;
    }
    textCounts.supplier=(textCounts.supplier||0)+anchored;
    // 独立销量徽标可能只有“已售100+件”，不满足商家卡的双锚点条件。
    for(const node of document.querySelectorAll('span,div')){
      if(!visible(node)||node.closest(excluded))continue;
      const text=(node.innerText||'').trim();
      if(text.length>80||!/^已售\s*\d+(?:\.\d+)?\s*万?\s*\+?\s*(件|包|个|笔)$/.test(text))continue;
      if(seenBlocks.has('sales|'+text))continue;
      seenBlocks.add('sales|'+text);sections.push(text);
      text_blocks.push({region:'price',text,source_locator:'text-anchor:sold-count'});
    }
    diagnostics.push(textCounts.supplier?`supplier：已读取 ${textCounts.supplier} 个商家信息块`:'supplier：未读取到可见文字，字段保留为空');
    const regions=roots.filter(([region])=>region!=='评论图').map(([region,selector])=>{
      const recognized=[...document.querySelectorAll(selector)].some(r=>!r.closest(excluded));
      const found=[...images.values()].filter(i=>i.region===region).length;
      return {region,recognized,complete:recognized&&found>0&&!skipped.has(region),found,
        reason:!recognized?'未识别区域':!found?'区域已识别但未发现图片':skipped.has(region)?'部分切换或加载被跳过':'已读取可访问区域'};
    });
    return {capture_version:CAPTURE_VERSION,page_url:originalUrl,title:titleEvidence?.text||'',visible_text:[...new Set(sections)].join('\n').slice(0,40000),text_blocks,
      images:[...images.values()],skus:[...skus.values()].slice(0,100),regions,diagnostics:diagnostics.slice(0,20)};
  }catch(error){return {error_type:error.message};}
  finally{detailObserver?.disconnect();for(const node of restore)if(node.isConnected)node.click();scrollTo(position.x,position.y);}
};
