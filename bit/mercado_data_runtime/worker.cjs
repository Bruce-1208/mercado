// Private JSON-lines worker. stdout contains only sanitized responses.
const fs = require('node:fs');
const readline = require('node:readline');
const {loadSigner} = require('./signer.cjs');
const {persistToken} = require('./session.cjs');
let signer;
async function request(url, data, session, refresh = false) {
  signer ||= await loadSigner(process.env.ZESHUN_MERCADO_EXTENSION_DIR);
  const path = new URL(url).pathname + new URL(url).search;
  const headers = await signer.iU({data, method: 'POST', url: path}, {
    getToken: () => session.token, appClient: session.appclient || '3', version: session.version || 'v1.1'
  });
  for (const k of ['ext_version', 'client_identify']) if (session[k]) headers[k] = session[k];
  headers['Content-Type'] = 'application/json';
  const res = await fetch(url, {method: 'POST', headers, body: JSON.stringify(data),
    redirect: 'error', signal: AbortSignal.timeout(12000)});
  if (!res.ok) throw Object.assign(Error('upstream'), {status: res.status});
  const result = await res.json();
  if (Number(result.code) !== 200) throw Object.assign(Error('upstream'), {status: Number(result.code)});
  const newToken = res.headers.get('refresh_token') || (refresh ? result.token || result.data?.token : null);
  if (newToken) {
    const outcome = await persistToken(process.env.ZESHUN_MERCADO_SESSION_FILE, session, newToken);
    if (refresh) return {renewed:outcome.saved, superseded:outcome.superseded};
  }
  if (refresh) throw Error('Refresh response contains no token');
  return result.data;
}
async function handle(msg) {
  const session = JSON.parse(fs.readFileSync(process.env.ZESHUN_MERCADO_SESSION_FILE, 'utf8'));
  if (!session.token) throw Object.assign(Error('session'), {status: 401});
  if (msg.action === 'refresh') {
    return request('https://seller.zying.net/api/AuthV2/RefreshToken', {}, session, true);
  }
  if (msg.action === 'products') {
    const data = await request('https://meli.zying.net/api/MeliBest/QueryMeliSkuInfos',
      {Skus: msg.skus, site: msg.site}, session);
    if (!Array.isArray(data)) throw Error('schema');
    // Explicit public listing allowlist excludes account-specific selection and private fields.
    const fields = ['Sku', 'Weight', 'Size', 'Uptime', 'Fee', 'Brand', 'Sellername', 'SellerName',
      'SellerId', 'SellerAddressCountry', 'SellerLevel', 'PowerSellerStatus', 'Storagetype',
      'Orders', 'Rate', 'Comment', 'Cateid'];
    return data.map(row => Object.fromEntries(fields.filter(k => row[k] !== undefined).map(k => [k, row[k]])));
  }
  if (msg.action === 'freight') {
    const data = await request('https://meli.zying.net/api/CmdHandler?cmd=meli_freight.select', {}, session);
    if (!Array.isArray(data?.list)) throw Error('schema');
    return data.list.map(row => Object.fromEntries(['abbr','start','ended','freight','below_freight'].map(k => [k,row[k]])));
  }
  if (msg.action === 'exchange') {
    const data = await request('https://seller.zying.net/api/CmdHandler?cmd=mpush.select', {name:'exchange'}, session);
    if (!Array.isArray(data?.exchange)) throw Error('schema');
    return Object.fromEntries(data.exchange.map(row => [row.cur, row.money]));
  }
  throw Error('action');
}
if (require.main === module) (async () => {
  for await (const line of readline.createInterface({input:process.stdin})) {
    try { process.stdout.write(JSON.stringify({ok:true, data:await handle(JSON.parse(line))})+'\n'); }
    catch (e) { process.stdout.write(JSON.stringify({ok:false, code:[401,403,429].includes(e.status)?e.status:502})+'\n'); }
  }
})();

module.exports = {handle, request};
