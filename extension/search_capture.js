/* 1688搜索结果只读采集。只返回详情链接和卡片可见文字，不读取Cookie或浏览器存储。 */
window.pintanSearchCapture=async function(keyword,maxCandidates=60){
  const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const visible=node=>!!node&&node.getClientRects().length>0&&getComputedStyle(node).visibility!=='hidden';
  const bodyText=()=>document.body?.innerText||'';
  const blocked=()=>/请完成验证|滑动验证|访问过于频繁|验证码/.test(bodyText())||
    [...document.querySelectorAll('#nc_1_wrapper,#baxia-dialog-content,[id*="captcha"]')].some(visible);
  if(blocked())return {error_type:'verification_required'};

  const original={x:scrollX,y:scrollY};let unchanged=0,lastHeight=0;
  for(let i=0;i<14&&unchanged<3;i++){
    const height=Math.max(document.documentElement.scrollHeight,document.body?.scrollHeight||0);
    window.scrollTo({top:Math.min(height,(i+1)*innerHeight*.9),behavior:'instant'});
    await pause(400);if(blocked())return {error_type:'verification_required'};
    const next=Math.max(document.documentElement.scrollHeight,document.body?.scrollHeight||0);
    unchanged=next===lastHeight?unchanged+1:0;lastHeight=next;
  }
  window.scrollTo(original.x,original.y);

  // 1688搜索页会在普通DOM、开放Shadow DOM和同源iframe之间切换实现。
  const roots=[document],seenRoots=new Set(roots);
  for(let cursor=0;cursor<roots.length&&cursor<300;cursor++){
    let elements=[];try{elements=[...roots[cursor].querySelectorAll('*')];}catch{continue;}
    for(const element of elements){
      if(element.shadowRoot&&!seenRoots.has(element.shadowRoot)){seenRoots.add(element.shadowRoot);roots.push(element.shadowRoot);}
      if(element.tagName==='IFRAME')try{
        const frame=element.contentDocument;if(frame&&!seenRoots.has(frame)){seenRoots.add(frame);roots.push(frame);}
      }catch{/* 跨域iframe不可读，保持平台边界 */}
    }
  }

  const limit=Math.min(60,Math.max(1,Number(maxCandidates)||60));
  const seen=new Map();let anchorCount=0,attributeCount=0,embeddedCount=0;
  const offerOf=raw=>{
    const value=String(raw||'');
    return value.match(/detail\.1688\.com\/offer\/(\d{6,})\.html/i)?.[1]||
      value.match(/(?:offerId|offer_id|offer-id)["'\s:=_-]{1,12}(\d{6,})/i)?.[1]||null;
  };
  const directOfferOf=raw=>{try{
    const url=new URL(raw,location.href);return url.hostname==='detail.1688.com'?
      url.pathname.match(/^\/offer\/(\d{6,})\.html$/)?.[1]||null:null;
  }catch{return null;}};
  const normalizeUrl=id=>`https://detail.1688.com/offer/${id}.html`;
  const cardFor=node=>node?.closest?.('[class*="offer-card"],[class*="offer-item"],[class*="sm-offer"],[data-offer-id],[data-offerid],li')||node?.parentElement||node;
  const add=(id,node,method)=>{
    if(!id||seen.has(id)||seen.size>=limit)return;
    const card=cardFor(node);const text=(card?.innerText||node?.innerText||'').replace(/\s+/g,' ').trim().slice(0,1200);
    const image=card?.querySelector?.('img');
    const titleOptions=[card?.querySelector?.('[class*="title"]')?.innerText,image?.getAttribute?.('alt'),
      node?.innerText,node?.getAttribute?.('title')].map(x=>String(x||'').replace(/\s+/g,' ').trim())
      .filter(x=>x.length>=2&&!/点此可以直接和卖家交流|网购体验|语音视频/.test(x));
    let title=(titleOptions[0]||'').slice(0,300);
    if(!title||title.length<2)title=`1688候选商品 ${id}`;
    const priceMatch=text.match(/(?:¥|￥|价格\s*)\s*([0-9]+(?:\.[0-9]+)?)/);
    const salesMatch=text.match(/(?:已售|成交|付款)\s*([^\s]{1,20})/);
    const moqMatch=text.match(/([0-9]+\s*(?:件|个|套|包|箱)\s*起(?:批|订)?)/);
    seen.set(id,{page_url:normalizeUrl(id),title,price:priceMatch?Number(priceMatch[1]):null,
      sales_text:salesMatch?.[0]||'',moq_text:moqMatch?.[1]||'',
      is_ad:/广告|推广|hotad|p4p/i.test(text+' '+String(card?.className||'')),
      relevance_rank:seen.size+1,discovery_method:method});
  };

  for(const root of roots){
    let anchors=[];try{anchors=[...root.querySelectorAll('a[href],area[href]')];}catch{/* ignored */}
    anchorCount+=anchors.length;
    for(const anchor of anchors){const id=directOfferOf(anchor.href||anchor.getAttribute('href'));if(id)add(id,anchor,'anchor');}
    let attributed=[];try{attributed=[...root.querySelectorAll('[data-offer-id],[data-offerid],[data-offer],[data-trace],[data-log],[data-spm-anchor-id]')];}catch{/* ignored */}
    attributeCount+=attributed.length;
    for(const element of attributed){
      const raw=[...element.attributes].map(x=>`${x.name}=${x.value}`).join(' ');const id=offerOf(raw);
      if(id)add(id,element,'attribute');
    }
  }

  // 闭合Shadow DOM或虚拟列表无法遍历时，从当前页面序列化证据恢复offer ID。
  // 这里只生成搜索候选，标题和事实仍必须在下一阶段详情页重新读取。
  if(seen.size<limit){
    const html=(document.documentElement?.innerHTML||'').slice(0,12000000);
    const pattern=/(?:detail\.1688\.com\/offer\/(\d{6,})\.html|["']?offerId["']?\s*[:=]\s*["']?(\d{6,}))/gi;
    let match;while(seen.size<limit&&(match=pattern.exec(html))){embeddedCount++;add(match[1]||match[2],null,'embedded-state');}
  }

  if(!seen.size&&/登录后|请登录|账号登录/.test(bodyText()))return {error_type:'login_required'};
  return {page_url:location.href,keyword:String(keyword||'').slice(0,80),candidates:[...seen.values()],
    diagnostics:[`扫描根节点 ${roots.length} 个`,`链接元素 ${anchorCount} 个`,`候选属性节点 ${attributeCount} 个`,
      `内嵌商品ID命中 ${embeddedCount} 次`,`去重候选 ${seen.size} 个`]};
};
