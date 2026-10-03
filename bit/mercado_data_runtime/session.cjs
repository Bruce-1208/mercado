// Private token rotation shared with the administrator's session importer.
const fs = require('node:fs');
const crypto = require('node:crypto');
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function persistToken(filename, previous, token) {
  if (typeof token !== 'string' || !token.trim() || token.length > 32768) throw Error('Invalid renewal response');
  const lock = filename + '.lock';
  const deadline = Date.now() + 2500;
  let fd;
  while (fd === undefined) {
    try { fd = fs.openSync(lock, 'wx', 0o600); }
    catch (e) { if (e.code !== 'EEXIST' || Date.now() >= deadline) throw Error('Session update locked'); await wait(25); }
  }
  let temporary;
  try {
    const current = JSON.parse(fs.readFileSync(filename, 'utf8'));
    // A late response must never overwrite a newer administrator login.
    if (current.token !== previous.token) return {saved:false, superseded:true};
    temporary = filename + '.' + crypto.randomBytes(8).toString('hex') + '.tmp';
    const next = {...current, token, renewed_at:new Date().toISOString()};
    fs.writeFileSync(temporary, JSON.stringify(next), {flag:'wx', mode:0o600});
    fs.renameSync(temporary, filename);
    previous.token = token;
    return {saved:true, superseded:false};
  } finally {
    if (temporary && fs.existsSync(temporary)) fs.unlinkSync(temporary);
    fs.closeSync(fd);
    fs.unlinkSync(lock);
  }
}
module.exports = {persistToken};
