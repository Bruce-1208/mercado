const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../bit/bit_zying_caiji.py'), 'utf8');
const script = source.split('    API_CALL_SCRIPT = r"""')[1].split('"""')[0];
function invoke(prefix, action, rows, handler) {
  const win = {__mercadoZyingProductApi: {handleProduct: handler}};
  return new Promise(resolve => {
    const run = new Function('window', `return function(){${script}}`)(win);
    run(prefix, action, rows, true, resolve);
  });
}
test('six official API reads overlap and retain input order', async () => {
  const pending = [];
  const rows = Array.from({length: 6}, (_, id) => ({id}));
  const result = invoke('sale', 'detail', rows, (prefix, action, row) => {
    assert.equal(prefix, 'sale'); assert.equal(action, 'detail');
    return new Promise(resolve => pending.push(() => resolve({code: 200, data: row})));
  });
  await new Promise(setImmediate);
  assert.equal(pending.length, 6, 'all six requests must start before any finishes');
  pending.reverse().forEach(resolve => resolve());
  assert.deepEqual((await result).responses.map(r => r.response.data), rows);
});
test('failed request keeps its position and error status', async () => {
  const result = await invoke('sale', 'detail', [{id: 1}, {id: 2}], async (p, a, row) => {
    if (row.id === 2) throw {response: {status: 429, data: {message: 'rate limit'}}};
    return {code: 200, data: row};
  });
  assert.equal(result.responses[0].ok, true);
  assert.deepEqual(result.responses[1], {ok: false, status: 429, error: 'rate limit'});
});
test('write commands and batches larger than six never reach upstream', async () => {
  const handler = () => assert.fail('must not issue requests');
  assert.equal((await invoke('sale', 'update', [{}], handler)).ok, false);
  assert.equal((await invoke('sale', 'detail', Array(7).fill({}), handler)).ok, false);
});
