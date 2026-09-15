const assert = require('node:assert/strict');
const {pollTask, requestJson} = require('../src/pintan/static/app.js');
(async () => {
  let calls = 0;
  global.fetch = async () => { calls++; return {ok:false,status:404,json:async()=>({detail:'任务不存在'})}; };
  await assert.rejects(pollTask('missing'), /任务不存在/);
  assert.equal(calls,1);
  await assert.rejects(pollTask(undefined), /有效任务编号/);
  assert.equal(calls,1);
  global.fetch = async () => ({ok:true,json:async()=>({status:'unexpected'})});
  await assert.rejects(pollTask('bad'), /未知任务状态/);
  global.fetch = async () => { throw new Error('network failure'); };
  await assert.rejects(pollTask('offline'), /network failure/);
  let states = ['queued','running','completed'];
  global.fetch = async () => ({ok:true,json:async()=>({status:states.shift(),result:{value:1}})});
  assert.deepEqual(await pollTask('good',()=>{},async()=>{}),{value:1});
  global.fetch = async () => ({ok:false,status:409,json:async()=>({detail:'正在运行'})});
  await assert.rejects(requestJson('/submit'), /正在运行/);
  global.fetch = async (_, options) => new Promise((resolve,reject) => {
    options.signal.addEventListener('abort',()=>reject(new Error('aborted')));
  });
  await assert.rejects(requestJson('/slow',{timeoutMs:20}), /服务请求超过等待时间/);
  console.log('Frontend network/polling tests: 7 passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
