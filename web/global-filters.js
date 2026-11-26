/* Impilo portal: one set of filters for the whole system.
 *
 * The bar lives in the portal shell (#impilo-global-filters) above every section. Its state is
 * remembered across sections and visits (localStorage). Pages embedded in the portal (the Overview
 * dashboard and every register) receive it by postMessage and hide their own filter controls:
 *   portal -> page   {type:'impilo-filters', filters, resolved}
 *   page   -> portal {type:'impilo-filters-request'}           (on load)
 *   page   -> portal {type:'impilo-set-filters', patch}        (drill-down clicks in the Overview)
 * `resolved` carries the concrete dates and which facilities in scope report data, so every page
 * applies the selection the same way.
 */
(function () {
  'use strict';
  var API = (window.IMPILO_API_ORIGIN||"http://127.0.0.1:8765")+'/api';
  var KEY = 'impilo.filters.v1';
  var DEFAULTS = { province: '', district: '', type: '', facility: '', period: '12', from: '', to: '' };
  var TYPES = [['', 'All facility types'], ['CLINIC', 'Clinics'], ['HOSPITAL', 'All hospitals'],
    ['RURAL_HOSPITAL', 'Rural hospitals'], ['DISTRICT_HOSPITAL', 'District hospitals'], ['PROVINCIAL_HOSPITAL', 'Provincial hospitals'],
    ['CENTRAL_HOSPITAL', 'Central hospitals'], ['OTHER_HOSPITAL', 'Other hospitals']];
  var PERIODS = [['1', 'Last month of data'], ['3', 'Last 3 months of data'], ['6', 'Last 6 months of data'], ['12', 'Last 12 months of data'], ['custom', 'Custom dates']];
  var APPLIES_TO = { overview: true, registers: true, ask: true, geo: true };

  var filters = load();
  var geo = null, latestDate = null, lastUpdated = null, facilities = [], resolved = null, seq = 0, ready = false;

  function load() {
    try { return Object.assign({}, DEFAULTS, JSON.parse(localStorage.getItem(KEY) || '{}')); } catch (e) { return Object.assign({}, DEFAULTS); }
  }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(filters)); } catch (e) { /* storage unavailable: filters still work for this visit */ } }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function fmt(n) { return Number(n).toLocaleString('en-GB'); }
  function day(d) { return new Date(d + 'T00:00:00').toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }); }
  function monthsBack(d, n) { var y = +d.slice(0, 4), m = +d.slice(5, 7) - (n - 1); while (m < 1) { m += 12; y--; } return y + '-' + String(m).padStart(2, '0') + '-01'; }

  // ------------------------------------------------------------------ styles
  var css = document.createElement('style');
  css.textContent = '#impilo-global-filters{position:sticky;top:78px;z-index:30;margin-bottom:16px}' +
    '.igf{display:flex;flex-wrap:wrap;align-items:end;gap:10px;background:rgba(255,255,255,.96);backdrop-filter:blur(6px);border:1px solid #e3e9f3;border-radius:16px;padding:12px 14px;box-shadow:0 10px 25px -18px rgba(11,31,58,.35);font-family:Inter,-apple-system,"Segoe UI",sans-serif}' +
    '.igf label{display:block;font-size:11px;font-weight:700;color:#465f88;margin-bottom:4px}' +
    '.igf select,.igf input{font:inherit;font-size:13.5px;height:38px;padding:0 12px;border:1px solid #e2e5ea;border-radius:10px;background:#fff;color:#111827;max-width:240px;box-shadow:0 1px 2px rgba(16,24,40,.06);transition:border-color .15s,box-shadow .15s}' +
    '.igf select{-webkit-appearance:none;appearance:none;padding-right:34px;background:#fff url(\"data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%2716%27 height=%2716%27 viewBox=%270 0 24 24%27 fill=%27none%27 stroke=%27%236b7280%27 stroke-width=%272%27 stroke-linecap=%27round%27 stroke-linejoin=%27round%27%3E%3Cpath d=%27M8 9l4-4 4 4M16 15l-4 4-4-4%27/%3E%3C/svg%3E\") no-repeat right 10px center;cursor:pointer}' +
    '.igf select:hover,.igf .combo:hover{border-color:#cfd5dd}' +
    '.igf select:focus,.igf input:focus{outline:0;border-color:#218a39;box-shadow:0 0 0 3px rgba(33,138,57,.14)}' +
    '.igf select:disabled{opacity:.55}' +
    '.igf .btn{font:inherit;font-size:13.5px;font-weight:600;border-radius:10px;height:38px;padding:0 14px;border:1px solid #218a39;background:#218a39;color:#fff;cursor:pointer}' +
    '.igf .btn.ghost{background:#fff;color:#218a39}.igf .btn.ghost:hover{background:#ecf7ee}' +
    '.igf .custom{display:none;gap:8px;align-items:end}.igf.is-custom .custom{display:flex}' +
    '.igf .scope{flex-basis:100%;display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;font-size:12px;color:#637797;border-top:1px solid #edf1f7;padding-top:8px;margin-top:2px}' +
    '.igf .scope b{color:#0b1f3a;font-weight:700}' +
    '.igf .pill{font-size:11px;font-weight:600;border-radius:999px;padding:2px 8px}' +
    '.igf .upd{margin-left:auto;font-weight:600;color:#465f88;white-space:nowrap}.igf .upd+.note{margin-left:12px}.igf .pill.area{background:#eef2ff;color:#3730a3}.igf .pill.on{background:#ecf7ee;color:#176c2b}.igf .pill.off{background:#fdf1d6;color:#8a5a00}' +
    '.igf .note{margin-left:auto;font-style:italic}' +
    '.igf .tag{font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#218a39}' +
    '.igf .field{position:relative}' +
    '.igf .combo{display:flex;align-items:center;justify-content:space-between;gap:10px;font:inherit;font-size:13.5px;height:38px;padding:0 10px 0 12px;border:1px solid #e2e5ea;border-radius:10px;background:#fff;color:#111827;cursor:pointer;text-align:left;box-shadow:0 1px 2px rgba(16,24,40,.06);transition:border-color .15s,box-shadow .15s}' +
    '.igf .combo span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '.igf .combo:after{content:"";flex:none;width:16px;height:16px;background:url(\"data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%2716%27 height=%2716%27 viewBox=%270 0 24 24%27 fill=%27none%27 stroke=%27%236b7280%27 stroke-width=%272%27 stroke-linecap=%27round%27 stroke-linejoin=%27round%27%3E%3Cpath d=%27M8 9l4-4 4 4M16 15l-4 4-4-4%27/%3E%3C/svg%3E\") no-repeat center}' +
    '.igf .combo:focus-visible,.igf .combo[aria-expanded=true]{outline:0;border-color:#218a39;box-shadow:0 0 0 3px rgba(33,138,57,.14)}' +
    '.igf .combo.set{border-color:#9fd2ab}' +
    '.igf .pop{position:absolute;left:0;top:calc(100% + 4px);z-index:40;width:max(100%,300px);background:#fff;border:1px solid #d7dfec;border-radius:12px;box-shadow:0 18px 40px -16px rgba(11,31,58,.35);padding:8px}' +
    '.igf .pop input{width:100%;max-width:none;margin-bottom:6px}' +
    '.igf .pop ul{list-style:none;margin:0;padding:0;max-height:320px;overflow:auto}' +
    '.igf .pop li{padding:7px 9px;border-radius:7px;cursor:pointer;font-size:13px;color:#0b1f3a;display:flex;justify-content:space-between;gap:10px}' +
    '.igf .pop li small{color:#637797;font-size:11px;white-space:nowrap}' +
    '.igf .pop li.active{background:#ecf7ee}' +
    '.igf .pop li[aria-selected=true]{font-weight:700;color:#176c2b}' +
    '.igf .pop li.group{cursor:default;font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#637797;padding:9px 9px 3px}' +
    '.igf .pop .more{font-size:11.5px;color:#637797;padding:6px 9px 2px}';
  document.head.appendChild(css);

  // ------------------------------------------------------------------ bar
  // ------------------------------------------------------------------ searchable dropdown
  // A button that opens a list with a search box. Options: {value,label,sub,group}. Type to filter
  // (every word must match the name or the detail), arrow keys to move, Enter to pick, Esc to close.
  var MAX_SHOWN = 200;
  function Combo(key, label, placeholder, getOptions, onPick) {
    this.key = key; this.label = label; this.placeholder = placeholder; this.getOptions = getOptions; this.onPick = onPick; this.active = 0; this.shown = [];
  }
  Combo.prototype.html = function () {
    var k = this.key;
    return '<div class="field"><label id="igf-' + k + '-label">' + esc(this.label) + '</label>' +
      '<button type="button" class="combo" id="igf-' + k + '" aria-haspopup="listbox" aria-expanded="false" aria-labelledby="igf-' + k + '-label igf-' + k + '"><span></span></button>' +
      '<div class="pop" id="igf-' + k + '-pop" hidden><input type="search" id="igf-' + k + '-q" placeholder="' + esc(this.placeholder) + '" autocomplete="off" role="combobox" aria-controls="igf-' + k + '-list" aria-expanded="true">' +
      '<ul role="listbox" id="igf-' + k + '-list" aria-labelledby="igf-' + k + '-label"></ul><div class="more" id="igf-' + k + '-more"></div></div></div>';
  };
  Combo.prototype.setValue = function (value) {
    var o = this.getOptions().find(function (x) { return x.value === value; }) || this.getOptions()[0];
    var b = el('igf-' + this.key); if (!b) return;
    b.querySelector('span').textContent = o ? o.label : '';
    b.classList.toggle('set', !!value); b.title = o ? o.label + (o.sub ? ' · ' + o.sub : '') : '';
    this.value = value;
  };
  Combo.prototype.setDisabled = function (d) { var b = el('igf-' + this.key); if (b) b.disabled = d; };
  Combo.prototype.open = function () {
    closeAll(this);
    el('igf-' + this.key + '-pop').hidden = false; el('igf-' + this.key).setAttribute('aria-expanded', 'true');
    var q = el('igf-' + this.key + '-q'); q.value = ''; this.render(''); q.focus();
  };
  Combo.prototype.close = function (focusButton) {
    var pop = el('igf-' + this.key + '-pop'); if (!pop || pop.hidden) return;
    pop.hidden = true; el('igf-' + this.key).setAttribute('aria-expanded', 'false');
    if (focusButton) el('igf-' + this.key).focus();
  };
  Combo.prototype.render = function (query) {
    var words = query.toLowerCase().split(/\s+/).filter(Boolean), self = this;
    var matches = this.getOptions().filter(function (o) {
      var hay = (o.label + ' ' + (o.sub || '') + ' ' + (o.group || '')).toLowerCase();
      return words.every(function (w) { return hay.indexOf(w) >= 0; });
    });
    this.shown = matches.slice(0, MAX_SHOWN);
    var idx = this.shown.findIndex(function (o) { return o.value === self.value; });
    this.active = words.length ? 0 : Math.max(0, idx);
    var html = '', lastGroup = null, k = this.key;
    this.shown.forEach(function (o, i) {
      if (o.group && o.group !== lastGroup) { html += '<li class="group" role="presentation">' + esc(o.group) + '</li>'; lastGroup = o.group; }
      html += '<li role="option" id="igf-' + k + '-o' + i + '" data-i="' + i + '" aria-selected="' + (o.value === self.value) + '"' + (i === self.active ? ' class="active"' : '') + '><span>' + esc(o.label) + '</span>' + (o.sub ? '<small>' + esc(o.sub) + '</small>' : '') + '</li>';
    });
    el('igf-' + k + '-list').innerHTML = html || '<li class="group" role="presentation">No matches</li>';
    var real = matches.filter(function (o) { return o.value; }).length;
    el('igf-' + k + '-more').textContent = matches.length > MAX_SHOWN ? 'Showing the first ' + MAX_SHOWN + ' of ' + fmt(real) + ' — keep typing to narrow the list' : '';
    this.highlight();
  };
  Combo.prototype.highlight = function () {
    var list = el('igf-' + this.key + '-list'), self = this;
    list.querySelectorAll('li[role=option]').forEach(function (li) { li.classList.toggle('active', +li.dataset.i === self.active); });
    var a = el('igf-' + this.key + '-o' + this.active);
    el('igf-' + this.key + '-q').setAttribute('aria-activedescendant', a ? a.id : '');
    if (a) a.scrollIntoView({ block: 'nearest' });
  };
  Combo.prototype.pick = function (i) {
    var o = this.shown[i]; if (!o) return;
    this.close(true); this.onPick(o);
  };
  Combo.prototype.bind = function () {
    var self = this, k = this.key;
    el('igf-' + k).addEventListener('click', function () { if (el('igf-' + k + '-pop').hidden) self.open(); else self.close(); });
    el('igf-' + k + '-q').addEventListener('input', function () { self.render(this.value); });
    el('igf-' + k + '-q').addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); self.active = Math.min(self.shown.length - 1, self.active + 1); self.highlight(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); self.active = Math.max(0, self.active - 1); self.highlight(); }
      else if (e.key === 'Enter') { e.preventDefault(); self.pick(self.active); }
      else if (e.key === 'Escape') { e.preventDefault(); self.close(true); }
    });
    el('igf-' + k + '-list').addEventListener('mousedown', function (e) { var li = e.target.closest('li[role=option]'); if (li) { e.preventDefault(); self.pick(+li.dataset.i); } });
  };
  var combos = {};
  function closeAll(except) { Object.keys(combos).forEach(function (k) { if (combos[k] !== except) combos[k].close(); }); }
  document.addEventListener('mousedown', function (e) { if (!e.target.closest || !e.target.closest('.igf .field')) closeAll(); });
  // Clicks inside the Overview or register frames never reach this document; the portal window
  // loses focus instead, so close any open list then. Same when focus leaves a dropdown by keyboard.
  window.addEventListener('blur', function () { closeAll(); });
  document.addEventListener('focusin', function (e) { if (!e.target.closest || !e.target.closest('.igf .field')) closeAll(); });

  // ------------------------------------------------------------------ the account's area
  // People with provincial, district or facility access only see (and can only pick) their own area.
  var allowedFacilities = [];
  function area() {
    var u = window.ImpiloAuth && window.ImpiloAuth.user;
    var a = u && u.role !== 'admin' && u.access;
    return a && a.level && a.level !== 'national' && a.areas && a.areas.length ? a : null;
  }
  function provinceOfDistrict(id) { var p = (geo ? geo.provinces : []).find(function (p) { return p.districts.some(function (d) { return d.id === id; }); }); return p ? p.id : ''; }
  function allowedProvinceIds() {
    var a = area(); if (!a) return null;
    if (a.level === 'province') return a.areas;
    if (a.level === 'district') return a.areas.map(provinceOfDistrict);
    return allowedFacilities.map(function (f) { return f.provinceId; });
  }
  function clamp(next) {
    var a = area(); if (!a || !geo) return next;
    if (a.level === 'province') {
      if (a.areas.indexOf(next.province) < 0) { next.province = a.areas[0]; next.district = ''; next.facility = ''; }
      if (next.district && provinceOfDistrict(next.district) !== next.province) { next.district = ''; next.facility = ''; }
    } else if (a.level === 'district') {
      if (a.areas.indexOf(next.district) < 0) { next.district = a.areas[0]; next.facility = ''; }
      next.province = provinceOfDistrict(next.district);
    } else {
      if (a.areas.indexOf(next.facility) < 0) next.facility = a.areas[0];
      var f = allowedFacilities.find(function (x) { return x.id === next.facility; });
      if (f) { next.province = f.provinceId || ''; next.district = f.districtId || ''; }
    }
    return next;
  }
  function areaLabel() {
    var a = area(); if (!a) return '';
    var names = a.level === 'province' ? a.areas.map(function (id) { var p = geo.provinces.find(function (x) { return x.id === id; }); return p ? p.name : id; })
      : a.level === 'district' ? a.areas.map(function (id) { var d; geo.provinces.some(function (p) { d = p.districts.find(function (x) { return x.id === id; }); return d; }); return d ? d.name : id; })
      : a.areas.map(function (id) { var f = allowedFacilities.find(function (x) { return x.id === id; }); return f ? f.name : id; });
    return names.join(', ');
  }

  function provinceOptions() {
    var allowed = allowedProvinceIds();
    var provs = (geo ? geo.provinces : []).filter(function (p) { return !allowed || allowed.indexOf(p.id) >= 0; });
    return (allowed ? [] : [{ value: '', label: 'All provinces' }]).concat(provs.map(function (p) { return { value: p.id, label: p.name, sub: p.districts.length + ' districts' }; }));
  }
  function districtOptions() {
    // With a province chosen: its districts. With all provinces: every district, grouped by province.
    var provs = geo ? geo.provinces.filter(function (p) { return !filters.province || p.id === filters.province; }) : [];
    var a = area(), only = a && a.level === 'district' ? a.areas : a && a.level === 'facility' ? allowedFacilities.map(function (f) { return f.districtId; }) : null;
    var opts = only ? [] : [{ value: '', label: filters.province ? 'All districts' : 'All districts (all provinces)' }];
    provs.forEach(function (p) { p.districts.forEach(function (d) { if (only && only.indexOf(d.id) < 0) return; opts.push({ value: d.id, label: d.name, sub: filters.province ? '' : p.name, group: filters.province ? null : p.name, provinceId: p.id }); }); });
    return opts;
  }
  function typeOptions() {
    return TYPES.map(function (t) { return { value: t[0], label: t[1], group: /HOSPITAL/.test(t[0]) ? 'Hospitals' : null }; });
  }
  function facilityOptions() {
    var ar = area(), only = ar && ar.level === 'facility' ? ar.areas : null;
    var list = facilities.filter(function (f) { return !only || only.indexOf(f.id) >= 0; }).sort(function (a, b) { return (b.hasData - a.hasData) || a.name.localeCompare(b.name); });
    return (only ? [] : [{ value: '', label: 'All facilities (' + fmt(list.length) + ')' }]).concat(list.map(function (f) {
      return { value: f.id, label: f.name, sub: (f.hasData ? 'Reporting · ' : '') + (f.typeLabel || '') + ' · ' + (f.district || '') + (filters.province ? '' : ', ' + (f.province || '')), facility: f };
    }));
  }
  combos.province = new Combo('province', 'Province', 'Search provinces…', provinceOptions, function (o) { setFilters({ province: o.value }); });
  combos.district = new Combo('district', 'District', 'Search districts…', districtOptions, function (o) {
    setFilters(o.value ? { province: o.provinceId, district: o.value } : { district: '' });
  });
  combos.type = new Combo('type', 'Facility type', 'Search facility types…', typeOptions, function (o) { setFilters({ type: o.value }); });
  combos.facility = new Combo('facility', 'Facility', 'Search by facility, district or type…', facilityOptions, function (o) {
    var f = o.facility; setFilters(f ? { province: f.provinceId || '', district: f.districtId || '', facility: f.id } : { facility: '' });
  });

  function barHtml() {
    var opt = function (list, val) { return list.map(function (o) { return '<option value="' + esc(o[0]) + '"' + (o[0] === val ? ' selected' : '') + '>' + esc(o[1]) + '</option>'; }).join(''); };
    return '<div class="igf" role="group" aria-label="Filters for the whole portal">' +
      combos.province.html() + combos.district.html() + combos.type.html() + combos.facility.html() +
      '<div><label for="igf-period">Period</label><select id="igf-period">' + opt(PERIODS, filters.period) + '</select></div>' +
      '<div class="custom"><div><label for="igf-from">From</label><input type="date" id="igf-from"></div><div><label for="igf-to">To</label><input type="date" id="igf-to"></div><button class="btn" id="igf-apply" type="button">Apply</button></div>' +
      '<div style="margin-left:auto"><button class="btn ghost" id="igf-reset" type="button">Reset filters</button></div>' +
      '<div class="scope" id="igf-scope" aria-live="polite"><span class="tag">Filters apply across the portal</span><span>Loading facility registry…</span></div>' +
      '</div>';
  }
  function el(id) { return document.getElementById(id); }

  function syncControls() {
    if (!el('igf-province') || !geo) return;
    combos.province.setValue(filters.province); combos.district.setValue(filters.district);
    combos.type.setValue(filters.type); combos.facility.setValue(filters.facility);
    el('igf-period').value = filters.period;
    el('igf-from').value = filters.from || (resolved && resolved.from) || ''; el('igf-to').value = filters.to || (resolved && resolved.to) || '';
    el('igf-from').closest('.igf').classList.toggle('is-custom', filters.period === 'custom');
  }
  // Ask the data is a plain chat: no filter bar there.
  function showHideBar() {
    var host = document.getElementById('impilo-global-filters'); if (!host) return;
    var want = activeSection() === 'ask' ? 'none' : '';
    if (host.style.display !== want) host.style.display = want;
  }
  function renderScope() {
    showHideBar();
    var box = el('igf-scope'); if (!box || !resolved) return;
    var active = activeSection(), applies = !active || APPLIES_TO[active];
    var r = resolved;
    var html = '<span class="tag">Filters apply across the portal</span>' +
      '<span>Showing <b>' + esc(r.label) + '</b>' + (r.typeLabel ? ' · ' + esc(r.typeLabel) : '') + ' · <b>' + day(r.from) + ' – ' + day(r.to) + '</b></span>' +
      '<span>' + fmt(r.facilityCount) + ' ' + (r.facilityCount === 1 ? 'facility' : 'facilities') + '</span>' +
      (area() ? '<span class="pill area" title="Your account covers this area only">Your area: ' + esc(areaLabel()) + '</span>' : '') +
      (r.reportingIds.length ? '<span class="pill on">' + fmt(r.reportingIds.length) + ' reporting: ' + esc(r.reportingNames.join(', ')) + '</span>' : '<span class="pill off">No facility in this selection reports data yet</span>') +
      (lastUpdated ? '<span class="upd" title="Time of the newest record received from reporting facilities">' + esc(lastUpdatedText()) + '</span>' : '') +
      (active === 'workspace' ? '<span class="note">The workspace has its own period and filters.</span>' : active === 'users' ? '<span class="note">Filters don\'t affect user management.</span>' : active === 'reports' ? '<span class="note">Each report keeps its own place and period. Use \'Use portal filters\' in a report to copy these in.</span>' : active === 'geo' ? '<span class="note">Province and facility type apply to the map. Its period is set on the map\'s timeline.</span>' : applies ? '' : active === 'config' ? '<span class="note">Filters don\'t affect configuration.</span>' : '<span class="note">This section uses demonstration data, so these filters don\'t change it.</span>');
    // Only touch the DOM when the text changes: the page observer below would otherwise loop.
    if (box.innerHTML !== html && box.dataset.html !== html) { box.innerHTML = html; box.dataset.html = html; }
  }

  // ------------------------------------------------------------------ state
  function setFilters(patch) {
    var next = Object.assign({}, filters, patch);
    // Changing a level clears the levels below it.
    if ('province' in patch && patch.province !== filters.province) { if (!('district' in patch)) next.district = ''; if (!('facility' in patch)) next.facility = ''; }
    if ('district' in patch && patch.district !== filters.district && !('facility' in patch)) next.facility = '';
    if ('type' in patch && patch.type !== filters.type && !('facility' in patch)) next.facility = '';
    if (next.period !== 'custom') { next.from = ''; next.to = ''; }
    filters = clamp(next); save(); syncControls(); refresh();
  }

  async function refresh() {
    var my = ++seq;
    var q = new URLSearchParams();
    if (filters.province) q.set('province', filters.province);
    if (filters.district) q.set('district', filters.district);
    if (filters.type) q.set('type', filters.type);
    try {
      var body = await (await fetch(API + '/facilities?' + q, { cache: 'no-store' })).json();
      if (my !== seq) return;
      facilities = body.data;
      if (filters.facility && !facilities.some(function (f) { return f.id === filters.facility; })) { filters.facility = ''; save(); }
    } catch (e) { if (my === seq) showError(); return; }
    var inScope = filters.facility ? facilities.filter(function (f) { return f.id === filters.facility; }) : facilities;
    var reporting = inScope.filter(function (f) { return f.hasData; });
    var to = filters.period === 'custom' && filters.to ? filters.to : latestDate;
    var from = filters.period === 'custom' && filters.from ? filters.from : monthsBack(latestDate, +filters.period || 12);
    var path = ['Zimbabwe'];
    var prov = geo.provinces.find(function (p) { return p.id === filters.province; });
    if (prov) path.push(prov.name);
    if (prov && filters.district) { var d = prov.districts.find(function (x) { return x.id === filters.district; }); if (d) path.push(d.name); }
    if (filters.facility && inScope[0]) path.push(inScope[0].name);
    var typeLabel = (TYPES.find(function (t) { return t[0] === filters.type; }) || [])[1];
    resolved = {
      from: from, to: to, latestDataDate: latestDate, label: path.join(' › '), typeLabel: filters.type ? typeLabel : null,
      facilityCount: inScope.length, reportingIds: reporting.map(function (f) { return f.id; }), reportingNames: reporting.map(function (f) { return f.name; }),
      facility: filters.facility ? inScope[0] || null : null
    };
    syncControls(); renderScope(); broadcast();
  }

  function message() { return { type: 'impilo-filters', filters: Object.assign({}, filters), resolved: resolved }; }
  function broadcast() {
    if (!resolved) return;
    document.querySelectorAll('iframe').forEach(function (f) { try { f.contentWindow.postMessage(message(), '*'); } catch (e) { } });
  }
  window.addEventListener('message', function (e) {
    var d = e.data || {};
    if (d.type === 'impilo-filters-request' && resolved && e.source) e.source.postMessage(message(), '*');
    if (d.type === 'impilo-set-filters' && d.patch) setFilters(d.patch);
    // A page can open another portal section, e.g. Configuration -> Ask the data.
    if (d.type === 'impilo-open-section' && d.section) { var nb = Array.prototype.find.call(document.querySelectorAll('nav[aria-label="Portal sections"] button'), function (x) { return x.textContent.indexOf(d.section) >= 0; }); if (nb) { nb.click(); window.scrollTo({ top: 0 }); } }
    // The AI assistant page asks to be sized to its content, like the Overview.
    if (d.type === 'ai-assistant-height') { var f = document.getElementById('ai-assistant-frame'); if (f && e.source === f.contentWindow) f.style.height = Math.max(700, Math.ceil(d.height)) + 'px'; }
  });

  function showError() {
    var box = el('igf-scope');
    if (box) box.innerHTML = '<span class="tag">Filters</span><span style="color:#c62f2f">The facility registry is not available. Make sure the API is running (start.cmd), or run update.cmd, then reload.</span>';
  }

  // ------------------------------------------------------------------ last updated (portal header)
  // Time of the newest record in the warehouse, shown on the Overview and Registers headers.
  function lastUpdatedText() {
    if (!lastUpdated) return '';
    var d = new Date(String(lastUpdated).replace(' ', 'T'));
    return 'Last updated: ' + (isNaN(d) ? lastUpdated : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) +
      (String(lastUpdated).length > 10 ? ', ' + d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) : ''));
  }
  // The page header that held this is gone; the time now sits at the end of the filter bar.
  function showLastUpdated() { renderScope(); }

  // ------------------------------------------------------------------ section awareness
  function activeSection() {
    var b = document.querySelector('nav[aria-label="Portal sections"] button[aria-current="page"]');
    if (!b) return null;
    var t = b.textContent.trim().toLowerCase();
    return t.indexOf('configuration') === 0 ? 'config' : t.indexOf('data workspace') === 0 ? 'workspace' : t.indexOf('users') === 0 ? 'users' : t.indexOf('geospatial') === 0 ? 'geo' : t.indexOf('overview') === 0 ? 'overview' : t.indexOf('registers') === 0 ? 'registers' : t.indexOf('data intelligence') === 0 ? 'intel' : t.indexOf('reports') === 0 ? 'reports' : t.indexOf('facilities') === 0 ? 'facilities' : 'ask';
  }

  // ------------------------------------------------------------------ mount
  function bind() {
    Object.keys(combos).forEach(function (k) { combos[k].bind(); });
    el('igf-period').addEventListener('change', function () {
      if (this.value === 'custom') { filters.period = 'custom'; filters.from = resolved ? resolved.from : ''; filters.to = resolved ? resolved.to : ''; save(); syncControls(); }
      else setFilters({ period: this.value });
    });
    el('igf-apply').addEventListener('click', function () {
      var from = el('igf-from').value, to = el('igf-to').value;
      if (!from || !to || from > to) { el('igf-to').setCustomValidity('Choose a start date on or before the end date.'); el('igf-to').reportValidity(); return; }
      el('igf-to').setCustomValidity(''); setFilters({ period: 'custom', from: from, to: to });
    });
    el('igf-reset').addEventListener('click', function () { setFilters(Object.assign({}, DEFAULTS)); });
  }
  function mount() {
    var host = document.getElementById('impilo-global-filters');
    if (!host || host.querySelector('.igf')) return;
    host.innerHTML = barHtml(); bind();
    if (ready) { syncControls(); renderScope(); } else if (geo === null) { /* still loading */ }
  }
  // React may re-create the shell; re-mount the bar whenever its host is empty, and refresh the
  // "applies to this section" note when the active section changes.
  new MutationObserver(function () { mount(); renderScope(); showLastUpdated(); }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['aria-current'] });

  async function init() {
    mount();
    try {
      var res = await Promise.all([fetch(API + '/geography', { cache: 'no-store' }), fetch(API + '/facility', { cache: 'no-store' })]);
      if (!res[0].ok || !res[1].ok) throw Error();
      geo = await res[0].json(); var site = await res[1].json(); latestDate = site.latestDate; lastUpdated = site.lastUpdated || site.latestDate;
      if (area() && area().level === 'facility') {
        try { var all = await (await fetch(API + '/facilities', { cache: 'no-store' })).json(); allowedFacilities = all.data.filter(function (f) { return area().areas.indexOf(f.id) >= 0; }); } catch (e) { }
      }
      filters = clamp(Object.assign({}, filters)); save();
      ready = true; mount(); showLastUpdated(); await refresh();
    } catch (e) { showError(); }
  }
  window.ImpiloFilters = { get: function () { return Object.assign({}, filters); }, resolved: function () { return resolved; }, set: setFilters };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
