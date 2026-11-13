const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync('dashboard/static/workspace.js', 'utf8');
function setup(hash = '#overview') {
  const nodes = new Map();
  const element = () => {
    const classes = new Set();
    return {textContent: '', hidden: false, attributes: {}, value: 'unsent-event',
      classList: {toggle(k, force) { const on = force ?? !classes.has(k); on ? classes.add(k) : classes.delete(k); return on; }, remove(k) { classes.delete(k); }, contains(k) { return classes.has(k); }},
      setAttribute(k,v) { this.attributes[k] = v; }, removeAttribute(k) { delete this.attributes[k]; },
      getAttribute(k) { return this.attributes[k]; }, focus() {}, addEventListener(k,fn) { this[k] = fn; },
      appendChild(child) { child.parentNode = this; }, insertBefore(child) { child.parentNode = this; }};
  };
  const get = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
  const names = ['overview','events','recovery','monitoring','settings'];
  const panels = names.map(name => ({...element(), dataset:{page:name}}));
  const links = names.map(name => { const e = element(); e.setAttribute('href', '#' + name); return e; });
  const home = element(), capture = element(); capture.parentNode = home; capture.nextElementSibling = element();
  const callbacks = {};
  const location = {hash};
  const context = vm.createContext({document:{getElementById:get, querySelector:()=>capture,
    querySelectorAll:selector=>selector === '[data-page]' ? panels : links},
    location, history:{replaceState(_,__,hash) { location.hash=hash; }},
    window:{addEventListener(name,fn) { callbacks[name]=fn; }, scrollTo() {} }});
  // Execute the actual routing and menu code; network/rendering is tested separately.
  vm.runInContext(source.slice(0, source.indexOf('  function card(')) + '})();', context);
  return {panels,links,location,callbacks,capture,home,get};
}
test('all five routes isolate pages and expose the active navigation entry', () => {
  const s = setup();
  for (const name of ['overview','events','recovery','monitoring','settings']) {
    s.location.hash = '#' + name; s.callbacks.hashchange();
    assert.deepEqual(s.panels.filter(p=>!p.hidden).map(p=>p.dataset.page), [name]);
    assert.equal(s.links.filter(l=>l.attributes['aria-current']==='page').length,1);
    assert.equal(s.links.find(l=>l.attributes['aria-current']==='page').attributes.href,'#'+name);
  }
});
test('legacy and unknown fragments resolve safely', () => {
  for (const [hash,expected] of [['#edge','#events'],['#telemetry','#monitoring'],['#outage-simulator','#recovery'],['#unknown','#overview']]) {
    assert.equal(setup(hash).location.hash,expected);
  }
});
test('full capture form stays in Events while Recovery has only the quick generator', () => {
  const html = fs.readFileSync('dashboard/static/index.html', 'utf8');
  const events = html.slice(html.indexOf('data-page="events"'), html.indexOf('data-page="recovery"'));
  const recovery = html.slice(html.indexOf('data-page="recovery"'), html.indexOf('data-page="monitoring"'));
  assert.match(events, /id="capture-edge"/);
  assert.match(events, /id="edge-prerequisites"/);
  assert.match(recovery, /id="quick-generate"/);
  assert.doesNotMatch(recovery, /id="capture-edge"|id="edge-priority"|id="edge-prerequisites"/);
  const s=setup('#events');
  s.location.hash='#recovery'; s.callbacks.hashchange();
  assert.equal(s.capture.parentNode,s.home);
  assert.equal(s.capture.value,'unsent-event');
});
test('menu toggle announces expansion and route changes collapse it', () => {
  const s=setup(); const menu=s.get('menu-toggle');
  menu.click(); assert.equal(menu.attributes['aria-expanded'],'true');
  assert.equal(s.get('workspace-nav').classList.contains('open'),true);
  s.location.hash='#events'; s.callbacks.hashchange();
  assert.equal(menu.attributes['aria-expanded'],'false');
});
