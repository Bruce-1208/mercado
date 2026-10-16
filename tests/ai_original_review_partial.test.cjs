const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup(status, rows, failIds = []) {
  const requests = [], messages = [];
  const context = vm.createContext({
    document: {addEventListener() {}, getElementById(id) {
      return id === 'ai-original-review-status' ? {value: status} : null;
    }},
    fetch: async (url, options) => {
      const id = Number(url.split('/').pop());
      requests.push({id, body: JSON.parse(options.body)});
      return {ok: !failIds.includes(id), json: async () => failIds.includes(id)
        ? {status: 'error', message: '数据库保存失败'} : {status: 'success', data: {id, ...JSON.parse(options.body)}}};
    },
    record: (...args) => messages.push(args),
  });
  const run = code => vm.runInContext(code, context);
  run(fs.readFileSync('bit/static/ai-original-products.js', 'utf8'));
  run(`aiOriginalRows = ${JSON.stringify(rows)};
    aiOriginalSelected = new Set(aiOriginalRows.map(row => row.id));
    updateAiOriginalSelection = () => {}; renderAiOriginalProducts = () => {};
    aiOriginalStatus = record; renderAiOriginalFailureLog = record;`);
  return {run, requests, messages};
}
const row = (id, changes = {}) => ({id, category_id: 'CBT123', ai_status: 'completed', weight_g: 100, net_proceeds_usd: 5,
  package_length_cm:10, package_width_cm:10, package_height_cm:10,
  title_es:'Caja', title_pt:'Caixa', description_es:'Descripcion', description_pt:'Descricao',
  main_image_url:'/test-ai-white.jpg', ai_original:{status:'completed', category_id:'CBT123', image_generation_method:'local_background_removal',
    attributes:[{id:'MATERIAL',value_name:'Steel'}], validation:{errors:[],warnings:[]}}, ...changes});

test('invalid product and server failure do not block other approvals', async () => {
  const {run, requests, messages} = setup('approved', [row(1), row(2, {weight_g: 0}), row(3), row(4)], [3]);
  await run('updateSelectedAiOriginalReviewStatus()');
  assert.deepEqual(requests.map(r => r.id).sort(), [1, 3, 4]);
  assert.deepEqual(requests.filter(r => r.id === 1).map(r => r.body.review_status), ['approved']);
  assert.deepEqual(requests.filter(r => r.id === 4).map(r => r.body.review_status), ['approved']);
  assert.equal(requests.find(r => r.id === 3).body.review_status, 'approved');
  assert.equal(run('JSON.stringify([...aiOriginalSelected])'), '[2,3]');
  assert.equal(run('aiOriginalReviewRunning'), false);
  assert.match(messages.at(-1)[0], /已更新 2 件.*失败 2 件/);
  assert.equal(messages.at(-1)[1], 'partial');
});

test('other statuses do not require generation or numeric parameters', async () => {
  const {run, requests} = setup('rejected', [row(1, {ai_status: 'pending', weight_g: 0}), row(2)], [1]);
  await run('updateSelectedAiOriginalReviewStatus()');
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[1].body, {review_status: 'rejected'});
  assert.equal(run('JSON.stringify([...aiOriginalSelected])'), '[1]');
});

test('all invalid items remain selected and send no writes', async () => {
  const {run, requests, messages} = setup('approved', [row(1, {ai_status: 'failed'})]);
  await run('updateSelectedAiOriginalReviewStatus()');
  assert.equal(requests.length, 0);
  assert.equal(run('aiOriginalSelected.size'), 1);
  assert.equal(messages.at(-1)[1], 'error');
});

test('stale success readback fails only that product and never fabricates its review status', async () => {
  const {run, messages} = setup('approved', [row(1, {review_status: 'suspected'}), row(2)]);
  run(`fetch = async (url, options) => {
    const id = Number(url.split('/').pop());
    const changes = JSON.parse(options.body);
    return {ok: true, json: async () => ({status: 'success', data: {id, ...changes,
      review_status: id === 1 ? 'suspected' : changes.review_status}})};
  };`);
  await run('updateSelectedAiOriginalReviewStatus()');
  assert.equal(run('aiOriginalRows[0].review_status'), 'suspected');
  assert.equal(run('aiOriginalRows[1].review_status'), 'approved');
  assert.equal(run('JSON.stringify([...aiOriginalSelected])'), '[1]');
  assert.match(messages.at(-1)[0], /已更新 1 件.*失败 1 件.*回读/);
});

test('review requests really overlap and keep draining after an HTTP failure', async () => {
  const {run} = setup('approved', [row(1), row(2), row(3), row(4), row(5)]);
  run(`var pending = [], attempted = [];
    fetch = (url, options) => {
      const id = Number(url.split('/').pop()); attempted.push(id);
      return new Promise(resolve => pending.push(() => resolve({ok: id !== 1,
        json: async () => id === 1 ? {status:'error',message:'保存失败'}
          : {status:'success',data:{id,...JSON.parse(options.body)}}})));
    };
    var saving = updateSelectedAiOriginalReviewStatus();`);
  assert.equal(run('attempted.length'), 4);
  await run('pending.splice(0).forEach(finish => finish()); Promise.resolve()');
  await run('Promise.resolve()');
  assert.equal(run('attempted.length'), 5);
  await run(`(async () => {
    while (pending.length || attempted.length < 5) {
      pending.splice(0).forEach(finish => finish());
      await Promise.resolve(); await Promise.resolve();
    }
    pending.splice(0).forEach(finish => finish());
    await saving;
  })()`);
  assert.equal(run('attempted.length'), 5);
  assert.equal(run('JSON.stringify([...aiOriginalSelected])'), '[1]');
  assert.equal(run('aiOriginalSavingIds.size'), 0);
});
