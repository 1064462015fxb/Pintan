// 真实本地页面展示验证；截获生成提交，避免UI验收重复消耗图片额度。
const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:8010/#selection/stable');
  await page.getByRole('button',{name:'AI评估并计算'}).first().waitFor();
  assert.match(await page.locator('#stableGrid').innerText(),/完整参考评分/);
  await page.getByRole('button',{name:/恢复全部SKU的AI建议价/}).first().waitFor();
  await page.goto('http://127.0.0.1:8010/#content');
  const choice=page.locator('.master-choice[data-product="1688-1066393076121"]');await choice.waitFor();await choice.check();
  const input=page.locator('#generatedMedia textarea').first();await input.waitFor();
  await input.fill('测试意见：减少留白');
  let payload=null;
  await page.route('**/api/media/images/*/retry',async route=>{
   payload=route.request().postDataJSON();await route.fulfill({status:422,contentType:'application/json',body:JSON.stringify({detail:'验收已截获，不调用模型'})});
  });
  await page.getByRole('button',{name:'根据意见重新生成'}).first().click();
  await page.waitForTimeout(300);
  assert.equal(payload.user_feedback,'测试意见：减少留白');assert.deepEqual(errors,[]);
  console.log('PASS: scoring inputs, AI price restore control, image feedback payload, no page errors');
 }finally{await browser.close()}
})().catch(e=>{console.error(e.message);process.exit(1)});
