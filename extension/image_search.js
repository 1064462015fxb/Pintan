/* 仅把用户在本机工作台主动选择的参考图放入1688的图搜控件。
 * 不读取Cookie、Token、LocalStorage或页面请求头；找不到标准控件即返回失败，不猜测接口。
 */
window.pintanImageSearch=async function(referenceDataUrl){
  const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const visible=node=>!!node&&node.getClientRects().length>0&&getComputedStyle(node).visibility!=='hidden';
  const text=()=>document.body?.innerText||'';
  if(/请完成验证|滑动验证|访问过于频繁|验证码/.test(text()))throw Error('verification_required');
  const clickImageEntry=()=>{
    const candidates=[...document.querySelectorAll('button,a,[role="button"],span,div')]
      .filter(node=>visible(node)&&/以图搜|图搜|图片搜索|搜同款/.test((node.innerText||node.getAttribute('aria-label')||node.title||'').trim()));
    candidates.sort((a,b)=>(a.innerText||'').length-(b.innerText||'').length);
    candidates[0]?.click();return !!candidates[0];
  };
  clickImageEntry();
  let input;
  for(let i=0;i<16&&!input;i++){
    input=[...document.querySelectorAll('input[type="file"]')].find(node=>!node.disabled);
    if(!input){clickImageEntry();await pause(250);}
  }
  if(!input)throw Error('image_search_unavailable');
  const response=await fetch(referenceDataUrl,{credentials:'omit',cache:'no-store'});
  if(!response.ok)throw Error('image_search_unavailable');
  const blob=await response.blob();
  if(!/^image\/(jpeg|png|webp)$/.test(blob.type)||blob.size<32)throw Error('image_search_unavailable');
  const suffix=blob.type==='image/png'?'png':blob.type==='image/webp'?'webp':'jpg';
  const file=new File([blob],'pintan-reference.'+suffix,{type:blob.type});const data=new DataTransfer();data.items.add(file);
  try{input.files=data.files;}catch{throw Error('image_search_unavailable');}
  input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));
  // 有些实现选择文件后立即跳转，另一些需要点击同一弹窗中的确认按钮。
  await pause(500);
  const submit=[...document.querySelectorAll('button,[role="button"],input[type="submit"]')].find(node=>visible(node)&&/搜索|确认|开始|上传/.test((node.innerText||node.value||node.getAttribute('aria-label')||'').trim()));
  submit?.click();
  const initial=location.href;
  // 提交后页面可能在原标签异步刷新；给平台控件一个短暂的处理窗口。
  for(let i=0;i<20;i++){
    if(location.href!==initial||document.querySelectorAll('a[href*="detail.1688.com/offer/"]').length>0)return true;
    await pause(350);
  }
  return true;
};
