// Name only complete, byte-identical modules. Sliced/transformed VM scripts
// remain anonymous: assigning original filenames would fabricate coverage.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const directory = path.resolve('dashboard/static');
const modules = new Map(fs.readdirSync(directory).filter(n => n.endsWith('.js'))
  .map(n => [fs.readFileSync(path.join(directory, n), 'utf8'), path.join(directory, n)]));
const original = vm.runInContext;
vm.runInContext = function(code, context, options) {
  if (modules.has(code)) {
    options = typeof options === 'string' ? {filename: options} : {...options};
    options.filename = modules.get(code);
  }
  return original.call(this, code, context, options);
};
