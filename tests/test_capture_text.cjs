/* 真实采集函数在可见DOM替身中运行，覆盖店铺标题误选和分区返回。 */
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(__dirname,'../extension/capture.js'),'utf8');
async function capture(product=true,lazy=false,resources=[],mode='full',pageText=''){
  const node=text=>({innerText:text,textContent:text,getClientRects:()=>[{}],closest:()=>null,
    querySelectorAll:()=>[],querySelector:()=>null,getAttribute:()=>null,tagName:'DIV',getBoundingClientRect:()=>({top:0,bottom:100})});
  const company=node('测试有限公司'),title=node('桌边自粘垃圾袋'),attr=node('材质：PE\n尺寸：25×30cm'),price=node('10包以上 ￥0.65'),gallery=node('');
  const lazyImage=node('');lazyImage.tagName='IMG';lazyImage.getAttribute=k=>k==='data-ks-lazyload'?'https://cbu01.alicdn.com/img/ibank/lazy.jpg':null;
  const detail=node('');detail.querySelectorAll=q=>q==='iframe'?[]:q.includes('data-ks-lazyload')?[lazyImage]:[];
  const all=q=>q==='h1'?[company]:q.includes('title-text')?(product?[title]:[]):q.includes('attribute')?[attr]:q.includes('price-component')?[price]:q.includes('gallery')&&!q.includes('thumbnail')?[gallery]:q.includes('description')&&lazy?[detail]:[];
  const sandbox={window:{},document:{title:product?'':'真实商品标题 - 阿里巴巴',body:{innerText:pageText},querySelectorAll:all,querySelector:q=>all(q)[0]||null},
    location:{href:'https://detail.1688.com/offer/123.html'},scrollX:0,scrollY:0,scrollTo:()=>{},
    getComputedStyle:()=>({visibility:'visible',backgroundImage:'none'}),performance:{getEntriesByType:()=>resources.map(name=>({name}))},URL,setTimeout:f=>f(),Date,Map,Set};
  vm.runInNewContext(source,sandbox);return sandbox.window.pintanCapture(mode);
}
test('商品标题与价格、属性区分开保存',async()=>{
  const r=await capture();assert.equal(r.title,'桌边自粘垃圾袋');
  assert.ok(r.text_blocks.some(b=>b.region==='price'&&b.text.includes('0.65')));
  assert.ok(r.text_blocks.some(b=>b.region==='attributes'&&b.text.includes('PE')));
});
test('公司h1不能作为标题，改用页面标题元数据',async()=>{
  const r=await capture(false);assert.equal(r.title,'真实商品标题');assert.equal(r.text_blocks[0].source_locator,'document.title');
});
test('详情区非常规懒加载属性会被回扫采集',async()=>{
  const r=await capture(true,true);const image=r.images.find(x=>x.url.includes('lazy.jpg'));
  assert.ok(image);assert.equal(image.region,'详情图');assert.equal(image.discovery,'data-ks-lazyload');
});
test('虚拟列表卸载后的阿里图片可由Performance资源回收',async()=>{
  const url='https://cbu01.alicdn.com/img/ibank/O1CN01detail-example.jpg';
  const r=await capture(true,false,[url]);const image=r.images.find(x=>x.url===url);
  assert.equal(r.capture_version,JSON.parse(require('node:fs').readFileSync('extension/manifest.json','utf8')).version);assert.ok(image);assert.equal(image.region,'待分类商品图');
  assert.equal(image.discovery,'Performance资源');
});
test('新版采集器包含基于点击选择状态的SKU图片关联',()=>{
  assert.match(source,/SKU选中后主图/);
  assert.match(source,/selected_main_image/);
  assert.match(source,/displayed_image_changed/);
  assert.match(source,/stableHero/);
});
test('快速文字模式单次读取页面文本并跳过完整素材流程',async()=>{
  const text='厨房收纳架\n已售100+件\n店铺回头率35%\n包邮';
  const r=await capture(true,false,[],'text_only',text);
  assert.equal(r.visible_text,text);
  assert.ok(r.diagnostics.some(x=>x.includes('跳过SKU点击')));
  assert.equal(r.regions.length,3);
});
