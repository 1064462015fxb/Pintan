// 新建隔离的无头浏览器验证本机页面，不读取用户浏览器Cookie、不触发模型。
const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:8010/?stable_v=5#selection/stable');
  await page.locator('#stableGrid table').waitFor();
  await page.locator('#candidatePanel').waitFor();
  const stableText=await page.locator('#stableGrid').innerText();
  const candidateText=await page.locator('#candidatePanel').innerText();
  assert.match(stableText,/利润潜力/);assert.match(stableText,/市场验证/);assert.match(stableText,/经营适配/);
  assert.match(candidateText,/待评分候选/);assert.match(candidateText,/销量与回头率必须同时存在/);
  assert.equal(await page.locator('#stableGrid tbody tr').count(),10);
  assert.ok(await page.locator('#candidatePanel [data-candidate]').count()>0);
  assert.deepEqual(errors,[]);
  console.log('PASS: 稳定池只显示完整评分；三级分数、待评分候选和缺项说明均正常展示');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
