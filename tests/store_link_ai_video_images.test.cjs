const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const template = fs.readFileSync('bit/templates/index.html', 'utf8');
function setup() {
  const elements = new Map();
  const c = vm.createContext({URL, aiVideoAssets: [], aiVideoUploadBusy:false,
    storeLinkRows: [], escapeHtml:String, switchTab(){}, setAiVideoTarget(){}, updateAiVideoMode(){},
    renderAiVideoAssets(){}, aiVideoSetMessage(message){c.message=message;},
    document:{getElementById(id){if(!elements.has(id)) elements.set(id,{value:'',focus(){},checked:true}); return elements.get(id);}}});
  vm.runInContext(template.slice(template.indexOf('        function openStoreLinkAIVideo('), template.indexOf('        function aiVideoLocalTranscodeEnabled(')), c);
  return c;
}
test('button imports all images, prefers secure URL, deduplicates on repeat', () => {
  const c=setup();
  c.storeLinkRows=[{id:1,item_id:'MLM1',pictures:JSON.stringify([{secure_url:'https://img.test/a.jpg',url:'http://img.test/a.jpg'},{url:'https://img.test/b.jpg'},{url:'https://img.test/a.jpg'}])}];
  c.openStoreLinkAIVideo(1);
  assert.deepEqual(Array.from(c.aiVideoAssets, a=>a.url),['https://img.test/a.jpg','https://img.test/b.jpg']);
  c.openStoreLinkAIVideo(1);
  assert.equal(c.aiVideoAssets.length,2);
  assert.equal(c.document.getElementById('ai-video-local-transcode').checked,false);
});
test('switching listing replaces imported images and retains manual assets',()=>{
  const c=setup(); c.aiVideoAssets=[{source:'url',url:'https://img.test/manual.jpg',kind:'image'}];
  c.importStoreLinkAiVideoImages({id:1,thumbnail_url:'https://img.test/one.jpg'});
  c.importStoreLinkAiVideoImages({id:2,pictures:[{url:'https://img.test/two.jpg'}]});
  assert.deepEqual(Array.from(c.aiVideoAssets,a=>a.url),['https://img.test/manual.jpg','https://img.test/two.jpg']);
});
test('all images remain visible beyond model limit; invalid URLs are ignored',()=>{
  const c=setup();
  c.importStoreLinkAiVideoImages({id:1,pictures:[...Array.from({length:12},(_,i)=>({url:`https://img.test/${i}.jpg`})),{url:'javascript:alert(1)'}]});
  assert.equal(c.aiVideoAssets.length,12); assert.match(c.message,/删减/);
});
test('missing and malformed image data falls back safely',()=>{
  const c=setup(); c.importStoreLinkAiVideoImages({id:1,pictures:'invalid',thumbnail_url:'https://img.test/a.jpg'});
  assert.equal(c.aiVideoAssets.length,1);
  c.importStoreLinkAiVideoImages({id:2}); assert.equal(c.aiVideoAssets.length,0); assert.match(c.message,/暂无图片/);
});
