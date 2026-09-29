/* 仅填写可明确识别的草稿文本字段；不选择类目/运费/资质，不操作发布按钮。 */
(()=>{
  if(window.pintanListingFill)return;
  const visible=element=>{
    const box=element.getBoundingClientRect(),style=getComputedStyle(element);
    return box.width>0&&box.height>0&&style.visibility!=='hidden'&&style.display!=='none'&&!element.disabled&&!element.readOnly;
  };
  const description=element=>[
    element.getAttribute('aria-label'),element.getAttribute('placeholder'),element.getAttribute('name'),element.id,
    element.closest('label,[class*=form],[class*=field],[class*=item]')?.innerText
  ].filter(Boolean).join(' ').replace(/\s+/g,' ').slice(0,500);
  const candidates=()=>[...document.querySelectorAll('input:not([type]),input[type=text],textarea,[contenteditable=true]')]
    .filter(x=>visible(x)&&!/(搜索|查询|验证码|手机号)/.test(description(x)));
  const choose=words=>candidates().map(x=>({x,text:description(x),score:words.reduce((n,w)=>n+(description(x).includes(w)?w.length:0),0)}))
    .filter(x=>x.score).sort((a,b)=>b.score-a.score)[0]?.x;
  const setValue=(element,value)=>{
    element.focus();
    if(element.isContentEditable){element.textContent=value;}
    else{
      const proto=element.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
      const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;
      setter?setter.call(element,value):(element.value=value);
    }
    for(const type of ['input','change','blur'])element.dispatchEvent(new Event(type,{bubbles:true}));
  };
  window.pintanListingFill=fields=>{
    const filled=[],warnings=[];
    const title=choose(['商品标题','宝贝标题','标题']);
    if(title&&fields.title){setValue(title,String(fields.title).slice(0,120));filled.push('商品标题');}
    else warnings.push('未找到商品标题输入框；请先完成类目选择，再重新点击辅助填写。');
    const detail=choose(['商品描述','详情描述','宝贝描述','详情文案']);
    if(detail&&fields.description){setValue(detail,String(fields.description).slice(0,12000));filled.push('详情文案');}
    else warnings.push('详情编辑器未开放普通文本输入，请从上架包复制或在千牛编辑器中导入。');
    warnings.push('图片、类目、SKU、库存、运费模板和资质必须人工核对；助手没有点击发布。');
    return {filled,warnings};
  };
})();
