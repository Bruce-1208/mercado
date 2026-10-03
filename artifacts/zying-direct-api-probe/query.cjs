#!/usr/bin/env node
// Single-product diagnostic. Uses the user's installed/downloaded signing runtime
// and an existing account session captured through DevTools; no browser is launched.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const { performance } = require('node:perf_hooks');

const ENDPOINT = 'https://meli.zying.net/api/MeliBest/QueryMeliSkuInfos';
const API_PATH = '/api/MeliBest/QueryMeliSkuInfos';

function argumentsMap(argv) {
  const result = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--offline') result.offline = true;
    else if (['--extension-dir', '--session-har', '--sku', '--site'].includes(argv[i])) {
      if (!argv[i + 1] || argv[i + 1].startsWith('--')) throw Error('Missing argument value');
      result[argv[i].slice(2)] = argv[++i];
    } else throw Error('Unknown argument');
  }
  if (!result['extension-dir']) throw Error('--extension-dir is required');
  return result;
}

async function loadSigner(extensionDir) {
  const source = fs.readFileSync(path.join(extensionDir, 'assets/js/background.js'), 'utf8');
  // These boundaries are verified for the downloaded 5.1.21 bundle.
  const marker = '7175(e,t,r){';
  const start = source.indexOf(marker);
  const end = source.indexOf('},9520(e,t,r)', start);
  if (start < 0 || end < 0) throw Error('Unsupported plugin build; recheck signing module boundaries');
  const body = source.slice(start + marker.length, end);
  const exportsObject = {};
  const req = {
    p: 'local-extension:/',
    d(out, entries) {
      for (const [key, get] of Object.entries(entries)) Object.defineProperty(out, key, { get });
    },
  };
  const sandbox = {
    TextEncoder, TextDecoder, Uint8Array, ArrayBuffer, DataView, WebAssembly, URL, Response,
    crypto: webcrypto, performance, console, exportsObject, req,
    localStorage: { getItem: key => key === 'rt_integrity' ? 'strict' : null },
    fetch: async url => {
      if (url !== 'local-extension:/assets/img/static-chunk.png') throw Error('Unexpected signing resource');
      return new Response(fs.readFileSync(path.join(extensionDir, 'assets/img/static-chunk.png')));
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(`(function(e,t,r){${body}})({},exportsObject,req)`, sandbox, { timeout: 3000 });
  await exportsObject.WH();
  return exportsObject;
}

async function main() {
  const args = argumentsMap(process.argv.slice(2));
  const signer = await loadSigner(args['extension-dir']);
  if (args.offline) {
    const headers = await signer.iU({ data: { Skus: ['MLM4567444350'], site: 1 }, method: 'POST', url: API_PATH },
      { getToken: () => 'offline-test-token', appClient: '3', version: 'v1.1' });
    console.log(JSON.stringify({ mode: 'offline', ready: signer.q3().ready, signatureLength: headers.signature.length, networkRequests: 0 }, null, 2));
    return;
  }
  if (!args['session-har'] || !args.sku) throw Error('--session-har and --sku are required for a live query');
  const sku = args.sku.replace(/-/g, '').toUpperCase();
  if (!/^(MLM|MLB|MLC|MLA|MLU|MCO)\d+$/.test(sku)) throw Error('Expected a listing ID, not a variant-family ID');
  // The HAR is private input. Never print, copy into the project, or serve it.
  const har = JSON.parse(fs.readFileSync(args['session-har'], 'utf8'));
  const entry = har.log.entries.find(e => e.request.url === ENDPOINT);
  if (!entry) throw Error('No matching query in capture');
  const previousBody = JSON.parse(entry.request.postData.text);
  const previousHeaders = Object.fromEntries(entry.request.headers.map(h => [h.name.toLowerCase(), h.value]));
  if (!previousHeaders.token) throw Error('Capture has no account token');
  const site = Number(args.site ?? previousBody.site);
  if (!Number.isInteger(site) || site <= 0) throw Error('Invalid site identifier');
  const data = { Skus: [sku], site };
  const headers = await signer.iU({ data, method: 'POST', url: API_PATH }, {
    getToken: () => previousHeaders.token,
    appClient: previousHeaders.appclient,
    version: previousHeaders.version,
  });
  for (const key of ['ext_version', 'client_identify']) {
    if (previousHeaders[key]) headers[key] = previousHeaders[key];
  }
  headers.Accept = 'application/json';
  headers['Content-Type'] = 'application/json';
  const start = performance.now();
  // No retries, concurrency or browser fallback. Authentication/rate errors stop the probe.
  const response = await fetch(ENDPOINT, {
    method: 'POST', headers, body: JSON.stringify(data),
    signal: AbortSignal.timeout(20000), redirect: 'error',
  });
  let result;
  try { result = await response.json(); }
  catch { throw Error(`Non-JSON response: HTTP ${response.status}`); }
  const records = Array.isArray(result.data) ? result.data : [];
  const output = {
    timestamp: new Date().toISOString(), mode: 'standalone-node',
    request: { url: ENDPOINT, body: data },
    httpStatus: response.status, businessCode: result.code,
    elapsedMs: Math.round(performance.now() - start), recordCount: records.length,
    records: records.map(x => ({ sku: x.Sku, weight: x.Weight, size: x.Size, uptime: x.Uptime })),
  };
  console.log(JSON.stringify(output, null, 2));
  if (!response.ok || Number(result.code) !== 200) process.exitCode = 1;
}

main().catch(error => {
  console.error(error.name === 'TimeoutError' ? 'Query timed out' : error.message);
  process.exitCode = 1;
});
