// Entry point so `node --test tests/` works on Node 22+, which no longer expands a directory
// argument into its test files (it tries to run `tests` as a module). `npm test` and a bare
// `node --test` still pick up each *.test.js file directly and do not load this file.
const fs = require('node:fs');
const path = require('node:path');

for (const file of fs.readdirSync(__dirname).filter(f => f.endsWith('.test.js')).sort()) {
  require(path.join(__dirname, file));
}
