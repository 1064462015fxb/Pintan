/* 淘宝/京东/抖音只读搜索页采集器。仅返回页面可见字段，不读取Cookie或站点存储。 */
(()=>{
  const clean=value=>String(value||'').replace(/\s+/g,' ').trim();
  const visible=node=>{const box=node?.getBoundingClientRect?.();return !!box&&box.width>20&&box.height>20;};
  const platformOf=host=>host.includes('taobao.com')||host.includes('tmall.com')?'taobao':host.includes('jd.com')?'jd':'douyin';
  const absolute=value=>{try{return new URL(value,location.href).href;}catch{return '';}};
  const first=(text,patterns)=>{for(const pattern of patterns){const found=text.match(pattern);if(found)return clean(found[1]||found[0]);}return '';};
  const productId=(platform,url)=>{
    if(platform==='taobao')return new URL(url).searchParams.get('id')||url.match(/item\/(\d+)/)?.[1]||'';
    if(platform==='jd')return url.match(/\/(\d+)\.html/)?.[1]||'';
    return url.match(/(?:product|goods|item)[\/_-]?(\d{6,})/)?.[1]||new URL(url).searchParams.get('id')||'';
  };
  const coverOf=card=>{
    const img=[...card.querySelectorAll('img')].find(image=>visible(image)&&!String(image.src).startsWith('data:image/svg'));
    return absolute(img?.currentSrc||img?.src||img?.dataset?.src||img?.dataset?.lazySrc||'');
  };
  const titleOf=(card,anchor)=>{
    const img=card.querySelector('img[alt]');
    const explicit=card.querySelector('[class*="title" i],[class*="name" i],[class*="desc" i]');
    return clean(img?.alt||anchor?.title||explicit?.textContent||anchor?.textContent).slice(0,500);
  };
  const normalizeUrl=(platform,value)=>{
    const url=absolute(value);if(!url)return '';
    try{const parsed=new URL(url);parsed.hash='';
      if(platform==='taobao'){const id=parsed.searchParams.get('id');if(id)return `https://item.taobao.com/item.htm?id=${id}`;}
      if(platform==='jd'){const id=url.match(/\/(\d+)\.html/)?.[1];if(id)return `https://item.jd.com/${id}.html`;}
      ['spm','scm','abbucket','utm_source','utm_medium','from','source'].forEach(key=>parsed.searchParams.delete(key));return parsed.href;
    }catch{return '';}
  };
  window.pintanTrendSearchCapture=(expectedPlatform,maxCandidates=20)=>{
    const platform=platformOf(location.hostname);
    if(platform!==expectedPlatform)return {error_type:'page_changed',platform,candidates:[]};
    const body=clean(document.body?.innerText).slice(0,10000);
    if(/验证码|安全验证|滑动验证|访问过于频繁|verify/i.test(body))return {error_type:'verification_required',platform,candidates:[]};
    if(/登录后查看|请登录|扫码登录/.test(body)&&document.querySelectorAll('img').length<5)return {error_type:'login_required',platform,candidates:[]};
    let anchors=[];
    if(platform==='taobao')anchors=[...document.querySelectorAll('a[href*="item.taobao.com/item.htm"],a[href*="detail.tmall.com/item.htm"]')];
    else if(platform==='jd')anchors=[...document.querySelectorAll('a[href*="item.jd.com/"]')];
    else anchors=[...document.querySelectorAll('a[href*="product"],a[href*="goods"],a[href*="item"]')];
    const seen=new Set(),candidates=[];
    for(const anchor of anchors){
      const url=normalizeUrl(platform,anchor.href);const id=productId(platform,url);if(!url||!id||seen.has(id))continue;
      let card=anchor.closest(platform==='jd'?'li.gl-item,li[class*="item"]':'[class*="card" i],[class*="item" i],[data-index]')||anchor.parentElement;
      for(let i=0;i<2&&card&&clean(card.innerText).length<20;i++)card=card.parentElement;
      if(!card||!visible(card))continue;
      const text=clean(card.innerText).slice(0,1600);const title=titleOf(card,anchor);const cover=coverOf(card);
      if(!title||title.length<2||!cover)continue;
      const price=first(text,[/[¥￥]\s*([0-9]+(?:\.[0-9]+)?)/,/价格\s*([0-9]+(?:\.[0-9]+)?)/]);
      const sales=first(text,[/(?:已售|付款|销量|售出|人付款)\s*([0-9]+(?:\.[0-9]+)?\s*[万w千k]?\+?(?:件|人)?)/i,/([0-9]+(?:\.[0-9]+)?\s*[万w千k]?\+?)\s*(?:人付款|件已售)/i]);
      const engagement=first(text,[/(?:评价|评论|互动)\s*([0-9]+(?:\.[0-9]+)?\s*[万w千k]?\+?)/i,/([0-9]+(?:\.[0-9]+)?\s*[万w千k]?\+?)\s*(?:条评价|条评论)/i]);
      const shop=clean(card.querySelector('[class*="shop" i],[class*="store" i]')?.textContent).slice(0,200);
      const isAd=/广告|推广|hotsearch/i.test(text)||!!card.querySelector('[class*="ad" i]');
      seen.add(id);candidates.push({product_id:id,url,title,price_text:price,sales_text:sales,
        engagement_text:engagement,shop,cover_url:cover,rank:candidates.length+1,is_ad:isAd,
        capture_method:'visible_dom',evidence:{card_text:text,source_locator:`search-card:${candidates.length+1}`}});
      if(candidates.length>=Math.min(20,Number(maxCandidates)||20))break;
    }
    return {platform,page_url:location.href,candidates,diagnostics:candidates.length?[]:['未从当前可见区域识别到商品卡']};
  };
  window.pintanTrendDetailCapture=expectedPlatform=>{
    const platform=platformOf(location.hostname),rawBody=String(document.body?.innerText||'').slice(0,50000),body=clean(rawBody).slice(0,30000);
    if(platform!==expectedPlatform)return {error_type:'page_changed'};
    if(/验证码|安全验证|滑动验证|访问过于频繁|verify/i.test(body))return {error_type:'verification_required'};
    if(/登录后查看|请登录|扫码登录/.test(body)&&document.querySelectorAll('img').length<5)return {error_type:'login_required'};
    const sales=first(body,[/(?:已售|付款|销量|售出|人付款)\s*([0-9]+(?:\.[0-9]+)?\s*[万w千k]?\+?(?:件|人)?)/i,/([0-9]+(?:\.[0-9]+)?\s*[万w千k]?\+?)\s*(?:人付款|件已售)/i]);
    const engagement=first(body,[/(?:评价|评论|互动)\s*([0-9]+(?:\.[0-9]+)?\s*[万w千k]?\+?)/i,/([0-9]+(?:\.[0-9]+)?\s*[万w千k]?\+?)\s*(?:条评价|条评论)/i]);
    const lines=rawBody.split(/\n+/).map(clean).filter(x=>x.length>=4&&x.length<=180);
    const specs=lines.filter(x=>/规格|参数|尺寸|材质|颜色|型号|容量|重量|适用/.test(x)).slice(0,18);
    const selling=lines.filter(x=>/卖点|特点|功能|加厚|耐用|防水|收纳|便携|可折叠|易清洁/.test(x)).slice(0,12);
    const reviewNodes=[...document.querySelectorAll('[class*="review" i],[class*="comment" i],[class*="rate" i]')].filter(visible);
    const reviews=[];for(const node of reviewNodes){const value=clean(node.innerText).slice(0,250);if(value.length>=6&&!reviews.includes(value))reviews.push(value);if(reviews.length>=10)break;}
    const shop=clean(document.querySelector('[class*="shop" i],[class*="store" i],[class*="seller" i]')?.textContent).slice(0,200);
    return {sales_text:sales,engagement_text:engagement,shop,specs_text:specs.join('；').slice(0,1500),
      selling_points:selling.join('；').slice(0,1000),review_snippets:reviews,detail_status:'已读取详情页',
      detail_evidence:{page_url:location.href,visible_text:body.slice(0,1000)}};
  };
})();
