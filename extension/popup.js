const status=document.getElementById('status');
async function action(type,extra={}) {
  try { const result=await chrome.runtime.sendMessage({type,...extra});status.textContent=result.message; }
  catch {status.textContent='后台暂不可用，请在扩展管理页重新加载。';}
}
document.getElementById('pair').onclick=()=>action('pair',{code:document.getElementById('code').value.trim().toUpperCase()});
document.getElementById('connect').onclick=()=>action('connect');
action('status');
