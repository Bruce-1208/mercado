// Loads the separately provisioned, account-owned vendor runtime. No vendor assets or sessions are bundled.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const { performance } = require('node:perf_hooks');

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
    crypto: webcrypto, performance, console: {log(){}, warn(){}, error(){}}, exportsObject, req,
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

module.exports = {loadSigner};
