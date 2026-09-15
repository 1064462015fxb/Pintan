const assert = require('node:assert/strict');
const {filterStable,routeFor} = require('../src/pintan/static/app.js');
for (const field of ['total_score','heat_score','competition_score','margin_score','differentiation_score','trust_score']) {
  const rows = [{stable_id:'C',[field]:''},{stable_id:'B',[field]:50},{stable_id:'A',[field]:50},{stable_id:'D',[field]:0}];
  assert.deepEqual(filterStable(rows,'',field,'desc','all').items.map(r=>r.stable_id),['A','B','D','C']);
  assert.deepEqual(filterStable(rows,'',field,'asc','all').items.map(r=>r.stable_id),['D','A','B','C']);
  assert.equal(rows[0].stable_id,'C');
}
const rows = Array.from({length:30},(_,i)=>({stable_id:String(i),title:'风扇',category:'电器',feature_tags:'数显',total_score:i}));
for (const n of [10,20,30]) assert.equal(filterStable(rows,'风扇','total_score','desc',String(n)).items.length,n);
assert.equal(filterStable(rows,'数显','total_score','desc','all').total,30);
assert.equal(filterStable(rows,'不存在').total,0);
for (const route of ['home','selection/batch','selection/stable','selection/trending','content','distribution']) assert.equal(routeFor('#'+route),route);
assert.equal(routeFor('#compliance'),'content');
assert.equal(routeFor('#bad'),'home');
console.log('Workspace sorting, search, quantity and routing checks passed');
