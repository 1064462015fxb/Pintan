// Real local UI; model/task responses intercepted, so no billable generation.
const assert=require('node:assert/strict');
const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  try{
    const page=await browser.newPage();const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    let running=false,cancelled=false,cancelCalls=0;
    await page.addInitScript(()=>sessionStorage.setItem('pintan.content.product','1688-953151089997'));
    await page.route('**/api/**',async route=>{
      const req=route.request(),url=new URL(req.url());
      if(url.pathname==='/api/media/plan'){
        running=true;cancelled=false;
        return route.fulfill({json:{task_id:'ui-control-fixture'}});
      }
      if(url.pathname==='/api/media/tasks')
        return route.fulfill({json:{items:running?[{task_id:'ui-control-fixture',status:'running',kind:'image-plan'}]:[]}});
      if(url.pathname==='/api/media/tasks/ui-control-fixture/cancel'){
        cancelCalls++;running=false;cancelled=true;
        return route.fulfill({json:{status:'cancelled',message:'测试：任务已取消，历史结果保留'}});
      }
      if(url.pathname==='/api/media/tasks/ui-control-fixture')
        return route.fulfill({json:{status:cancelled?'cancelled':'running',kind:'image-plan',error:cancelled?'测试：任务已取消':null,created_at:new Date().toISOString()}});
      // Read the current UI/data only; no enrichment or other model calls.
      if(req.method()!=='GET')return route.fulfill({status:409,json:{detail:'UI测试不执行业务写入'}});
      return route.continue();
    });
    await page.goto('http://127.0.0.1:8010/?responsiveness_v=1#content');
    await page.locator('#imagePlanButton').waitFor({state:'visible',timeout:15000});
    await page.locator('#imagePlanButton').click();
    await page.locator('#mediaCancelButton').waitFor({state:'visible'});
    assert.match(await page.locator('#mediaStatus').innerText(),/规划/);
    page.on('dialog',d=>d.accept());
    await page.locator('#mediaCancelButton').click();
    await page.waitForTimeout(1300);
    assert.equal(cancelCalls,1,'Cancel button must actually send a request');
    assert.match(await page.locator('#mediaStatus').innerText(),/取消/);
    assert.equal(await page.locator('#mediaCancelButton').isVisible(),false);
    // Refresh must expose cancel/resume even with no local polling promise.
    running=true;cancelled=false;
    await page.reload();
    await page.locator('#mediaResumeButton').waitFor({state:'visible',timeout:15000});
    assert.equal(await page.locator('#mediaCancelButton').isVisible(),true);
    await page.locator('#mediaCancelButton').click();
    await page.waitForTimeout(300);
    assert.equal(cancelCalls,2);
    assert.deepEqual(errors,[]);
    console.log('Browser controls: planning progress, cancel click, refresh recovery, second cancel: PASS');
  }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
