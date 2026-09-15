// 隔离浏览器仅验证本机工作台，不读取登录信息、不发起付费生成。
const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:8010/#collector');
  await page.locator('#classifyAllImages').waitFor({timeout:20000});
  await page.waitForFunction(()=>document.querySelector('#imageAnalysisResults')?.textContent.includes('95%'));
  assert.ok(await page.locator('#imageAnalysisResults details').count()>=3);
  await page.screenshot({path:'outputs/image-workflow-collector.png',fullPage:false});
  await page.reload();await page.locator('#classifyAllImages').waitFor();
  assert.equal(errors.length,0,errors.join('\n'));
  await page.goto('http://127.0.0.1:8010/#content');
  const choice=page.locator('.master-choice[data-product="1688-1066393076121"]');
  await choice.waitFor();await choice.check();
  await page.locator('#imagePlanButton').waitFor({timeout:20000});
  assert.equal(await page.locator('input[name="generationMode"]').count(),2);
  assert.ok(await page.locator('input[name="generationMode"][value="conservative"]').isChecked());
  await page.locator('input[name="generationMode"][value="creative"]').check();
  assert.equal(await page.evaluate(()=>localStorage.getItem('pintan.image.mode')),'creative');
  await page.locator('#imagePlanButton').click();
  await page.locator('#imagePlanPreview').waitFor({timeout:20000});
  assert.match(await page.locator('#imagePlanPreview').innerText(),/轮播图/);
  assert.ok(await page.locator('#imagePlanPreview .slot-mode').count()>=1);
  assert.match(await page.locator('#imagePlanPreview').innerText(),/自动：纯文字场景/);
  assert.ok(await page.locator('#generatedMedia .mode-badge').count()>=3);
  await page.screenshot({path:'outputs/image-workflow-content.png',fullPage:false});
  assert.equal(errors.length,0,errors.join('\n'));
  console.log('PASS: collector classification button, persisted results, reload, no JS errors');
 }finally{await browser.close()}
})().catch(e=>{console.error(e.message);process.exit(1)});
