const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {persistToken} = require('../bit/mercado_data_runtime/session.cjs');
const signerPath = require.resolve('../bit/mercado_data_runtime/signer.cjs');
require.cache[signerPath] = {exports:{loadSigner:async()=>({iU:async()=>({})})}};
const {request} = require('../bit/mercado_data_runtime/worker.cjs');
function fixture(t) {
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'zeshun-renew-test-'));
 t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const filename=path.join(dir,'session.json');
 const session={token:'old-test-token',appclient:'3'};
 fs.writeFileSync(filename,JSON.stringify(session),{mode:0o600});
 return {filename,session};
}
test('rotation is private, atomic, and preserves session settings',async t=>{
 const {filename,session}=fixture(t);
 assert.deepEqual(await persistToken(filename,session,'new-test-token'),{saved:true,superseded:false});
 const saved=JSON.parse(fs.readFileSync(filename));
 assert.equal(saved.appclient,'3'); assert.equal(saved.token,'new-test-token');
 assert.equal(fs.statSync(filename).mode&0o777,0o600);
 assert(saved.renewed_at); assert(!fs.existsSync(filename+'.lock'));
});
test('late refresh cannot overwrite administrator replacement',async t=>{
 const {filename,session}=fixture(t);
 fs.writeFileSync(filename,JSON.stringify({token:'admin-new-login'}));
 assert.deepEqual(await persistToken(filename,session,'stale-reply'),{saved:false,superseded:true});
 assert.equal(JSON.parse(fs.readFileSync(filename)).token,'admin-new-login');
});
test('normal successful API response rotates refresh_token without exposing it',async t=>{
 const {filename,session}=fixture(t);
 const previous=process.env.ZESHUN_MERCADO_SESSION_FILE;
 const fetch=global.fetch;
 t.after(()=>{global.fetch=fetch;if(previous===undefined)delete process.env.ZESHUN_MERCADO_SESSION_FILE;else process.env.ZESHUN_MERCADO_SESSION_FILE=previous;});
 process.env.ZESHUN_MERCADO_SESSION_FILE=filename;
 global.fetch=async()=>new Response(JSON.stringify({code:200,data:[{Sku:'MLM6226808220'}]}),{headers:{refresh_token:'rotated-header-token'}});
 const data=await request('https://meli.zying.net/api/MeliBest/QueryMeliSkuInfos',{},session);
 assert.deepEqual(data,[{Sku:'MLM6226808220'}]);
 assert.equal(JSON.parse(fs.readFileSync(filename)).token,'rotated-header-token');
 global.fetch=async()=>new Response(JSON.stringify({code:200,token:'explicit-renewal-token'}));
 assert.deepEqual(await request('https://seller.zying.net/api/AuthV2/RefreshToken',{},session,true),{renewed:true,superseded:false});
 global.fetch=async()=>new Response(JSON.stringify({code:200,data:{}}));
 await assert.rejects(request('https://seller.zying.net/api/AuthV2/RefreshToken',{},session,true));
 global.fetch=async()=>new Response(JSON.stringify({code:401}),{headers:{refresh_token:'untrusted-failed-token'}});
 await assert.rejects(request('https://seller.zying.net/api/AuthV2/RefreshToken',{},session,true));
 assert.equal(JSON.parse(fs.readFileSync(filename)).token,'explicit-renewal-token');
});
