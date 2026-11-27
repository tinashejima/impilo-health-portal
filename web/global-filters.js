/* Impilo portal: one set of filters for the whole system.
 *
 * The bar lives in the portal shell (#impilo-global-filters) above every section. Its state is
 * remembered across sections and visits (localStorage). Pages embedded in the portal (the Overview
 * dashboard and every register) receive it by postMessage and hide their own filter controls:
 *   portal -> page   {type:'impilo-filters', filters, resolved}
 *   page   -> portal {type:'impilo-filters-request'}           (on load)
 *   page   -> portal {type:'impilo-set-filters', patch}        (drill-down clicks in the Overview)
 * On a page opened on its own (no portal), the same bar can be mounted in #impilo-global-filters; the
 * page then listens for the 'impilo-filters' window event and calls ImpiloFilters.set(patch).
 *
 * Filters: provinces, districts, facility types and facilities each hold several IDs ([] = all).
 * Pages that predate multiple selection also get province/district/type/facility: the single ID when
 * exactly one is chosen, otherwise '' (all); type is comma-separated.
 * `resolved` carries the concrete dates and which facilities in scope report data, so every page
 * applies the selection the same way.
 */
(function () {
  'use strict';
  var API = (window.IMPILO_API_ORIGIN || "http://127.0.0.1:8765") + '/api';
  var KEY = 'impilo.filters.v2', OLD_KEY = 'impilo.filters.v1';
  var DEFAULTS = { provinces: [], districts: [], types: [], facilities: [], period: 'data-12', month: '', quarter: '', half: '', year: '', week: '', from: '', to: '' };
  var TYPES = [['CLINIC', 'Clinics', null], ['RURAL_HOSPITAL', 'Rural hospitals', 'Hospitals'], ['DISTRICT_HOSPITAL', 'District hospitals', 'Hospitals'],
    ['PROVINCIAL_HOSPITAL', 'Provincial hospitals', 'Hospitals'], ['CENTRAL_HOSPITAL', 'Central hospitals', 'Hospitals'], ['OTHER_HOSPITAL', 'Other hospitals', 'Hospitals']];
  var HOSPITALS = TYPES.filter(function (t) { return t[2]; }).map(function (t) { return t[0]; });
  // Reporting periods. Calendar periods count from today; "latest data" periods end on the newest record.
  var PERIODS = [
    ['Quick periods', [['today', 'Today'], ['yesterday', 'Yesterday'], ['this-week', 'This week'], ['last-week', 'Last week'], ['biweekly', 'Bi-weekly (two weeks)'], ['this-month', 'This month'], ['last-month', 'Last month']]],
    ['Choose a period', [['month', 'Monthly'], ['quarter', 'Quarterly'], ['half', 'Bi-yearly (half-year)'], ['year', 'Yearly'], ['custom', 'Custom period']]],
    ['Up to the latest data', [['data-1', 'Last month of data'], ['data-3', 'Last 3 months of data'], ['data-6', 'Last 6 months of data'], ['data-12', 'Last 12 months of data']]]];
  var PERIOD_IDS = [].concat.apply([], PERIODS.map(function (g) { return g[1].map(function (p) { return p[0]; }); }));
  var QUARTERS = [['1', 'Q1 (January – March)'], ['2', 'Q2 (April – June)'], ['3', 'Q3 (July – September)'], ['4', 'Q4 (October – December)']];
  var HALVES = [['1', 'H1 (January – June)'], ['2', 'H2 (July – December)']];
  var FIRST_YEAR = 2005;
  var APPLIES_TO = { overview: true, registers: true, ask: true, geo: true };

  var filters = load();
  var geo = null, latestDate = null, lastUpdated = null, facilities = [], resolved = null, seq = 0, ready = false;

  // ------------------------------------------------------------------ saved state
  function arr(v) { return Array.isArray(v) ? v.filter(Boolean).map(String) : v ? String(v).split(',').filter(Boolean) : []; }
  function normalise(f) {
    var out = Object.assign({}, DEFAULTS);
    Object.keys(DEFAULTS).forEach(function (k) { if (f && f[k] != null) out[k] = Array.isArray(DEFAULTS[k]) ? arr(f[k]) : String(f[k]); });
    if (PERIOD_IDS.indexOf(out.period) < 0) out.period = 'data-12';
    return out;
  }
  function load() {
    try {
      var saved = localStorage.getItem(KEY);
      if (saved) return normalise(JSON.parse(saved));
      // Filters saved before multiple selection: one province/district/type/facility and 'Last N months'.
      var old = JSON.parse(localStorage.getItem(OLD_KEY) || 'null');
      if (old) {
        return normalise({
          provinces: old.province, districts: old.district, facilities: old.facility,
          types: old.type === 'HOSPITAL' ? HOSPITALS : old.type,
          period: old.period === 'custom' ? 'custom' : 'data-' + (old.period || '12'), from: old.from, to: old.to
        });
      }
    } catch (e) { /* unreadable: start from the defaults */ }
    return normalise({});
  }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(filters)); } catch (e) { /* storage unavailable: filters still work for this visit */ } }

  // ------------------------------------------------------------------ helpers
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function fmt(n) { return Number(n).toLocaleString('en-GB'); }
  function el(id) { return document.getElementById(id); }
  function day(d) { return new Date(d + 'T00:00:00').toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }); }
  function pad(n) { return String(n).padStart(2, '0'); }
  function iso(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function parse(s) { return new Date(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)); }
  function addDays(d, n) { var x = new Date(d); x.setDate(x.getDate() + n); return x; }
  function monday(d) { return addDays(d, -((d.getDay() + 6) % 7)); }
  function monthsBack(d, n) { var y = +d.slice(0, 4), m = +d.slice(5, 7) - (n - 1); while (m < 1) { m += 12; y--; } return y + '-' + pad(m) + '-01'; }
  function monthEnd(y, m) { return iso(new Date(y, m, 0)); } // m is 1-12
  function monthName(y, m) { return new Date(y, m - 1, 1).toLocaleDateString('en-GB', { month: 'long', year: 'numeric' }); }
  // ISO week ("2026-W47") <-> its Monday.
  function weekMonday(w) {
    var m = /^(\d{4})-W(\d{2})$/.exec(w || ''); if (!m) return null;
    var jan4 = new Date(+m[1], 0, 4); return addDays(monday(jan4), (+m[2] - 1) * 7);
  }
  function isoWeek(d) {
    var t = new Date(d.getFullYear(), d.getMonth(), d.getDate()); t.setDate(t.getDate() + 3 - (t.getDay() + 6) % 7);
    var jan4 = new Date(t.getFullYear(), 0, 4);
    var w = 1 + Math.round(((t - jan4) / 864e5 - 3 + (jan4.getDay() + 6) % 7) / 7);
    return t.getFullYear() + '-W' + pad(w);
  }
  function names(list) { return list.length <= 3 ? list.join(', ') : list.slice(0, 2).join(', ') + ' and ' + (list.length - 2) + ' more'; }

  // Defaults for the "choose a period" controls: the period holding the newest data, so they open on figures.
  function anchor() { return latestDate || iso(new Date()); }
  function defaults() {
    var a = anchor(), y = a.slice(0, 4), m = +a.slice(5, 7);
    return { month: a.slice(0, 7), quarter: y + '-Q' + Math.ceil(m / 3), half: y + '-H' + (m <= 6 ? 1 : 2), year: y, week: isoWeek(addDays(monday(new Date()), -7)) };
  }

  // The selected reporting period as dates: {from, to, label}.
  function resolvePeriod(f) {
    var today = new Date(), t = iso(today), d = defaults(), y, n;
    switch (f.period) {
      case 'today': return { from: t, to: t, label: 'Today' };
      case 'yesterday': var yd = iso(addDays(today, -1)); return { from: yd, to: yd, label: 'Yesterday' };
      case 'this-week': var mo = monday(today); return { from: iso(mo), to: iso(addDays(mo, 6)), label: 'This week' };
      case 'last-week': var lm = addDays(monday(today), -7); return { from: iso(lm), to: iso(addDays(lm, 6)), label: 'Last week' };
      case 'biweekly':
        var start = weekMonday(f.week) || weekMonday(d.week);
        return { from: iso(start), to: iso(addDays(start, 13)), label: 'Two weeks from ' + day(iso(start)) };
      case 'this-month': return { from: iso(new Date(today.getFullYear(), today.getMonth(), 1)), to: monthEnd(today.getFullYear(), today.getMonth() + 1), label: 'This month' };
      case 'last-month':
        var pm = new Date(today.getFullYear(), today.getMonth() - 1, 1);
        return { from: iso(pm), to: monthEnd(pm.getFullYear(), pm.getMonth() + 1), label: 'Last month' };
      case 'month':
        var mm = /^\d{4}-\d{2}$/.test(f.month) ? f.month : d.month; y = +mm.slice(0, 4); n = +mm.slice(5, 7);
        return { from: mm + '-01', to: monthEnd(y, n), label: monthName(y, n) };
      case 'quarter':
        var q = /^\d{4}-Q[1-4]$/.test(f.quarter) ? f.quarter : d.quarter; y = +q.slice(0, 4); n = +q.slice(-1);
        return { from: y + '-' + pad(n * 3 - 2) + '-01', to: monthEnd(y, n * 3), label: 'Q' + n + ' ' + y };
      case 'half':
        var h = /^\d{4}-H[12]$/.test(f.half) ? f.half : d.half; y = +h.slice(0, 4); n = +h.slice(-1);
        return { from: y + (n === 1 ? '-01-01' : '-07-01'), to: y + (n === 1 ? '-06-30' : '-12-31'), label: (n === 1 ? 'January – June ' : 'July – December ') + y };
      case 'year':
        y = /^\d{4}$/.test(f.year) ? f.year : d.year; return { from: y + '-01-01', to: y + '-12-31', label: 'Year ' + y };
      case 'custom':
        if (f.from && f.to) return { from: f.from, to: f.to, label: 'Custom period' };
        break;
    }
    n = +(String(f.period).split('-')[1]) || 12;
    var to = latestDate || t;
    return { from: monthsBack(to, n), to: to, label: n === 1 ? 'Last month of data' : 'Last ' + n + ' months of data' };
  }

  // ------------------------------------------------------------------ styles
  var css = document.createElement('style');
  css.textContent = '#impilo-global-filters{position:sticky;top:78px;z-index:30;margin-bottom:16px}' +
    '.igf{display:flex;flex-wrap:wrap;align-items:end;gap:10px;background:rgba(255,255,255,.96);backdrop-filter:blur(6px);border:1px solid #e3e9f3;border-radius:16px;padding:12px 14px;box-shadow:0 10px 25px -18px rgba(11,31,58,.35);font-family:Inter,-apple-system,"Segoe UI",sans-serif}' +
    '.igf label,.igf .lbl{display:block;font-size:11px;font-weight:700;color:#465f88;margin-bottom:4px}' +
    '.igf select,.igf input{font:inherit;font-size:13.5px;height:38px;padding:0 12px;border:1px solid #e2e5ea;border-radius:10px;background:#fff;color:#111827;max-width:240px;box-shadow:0 1px 2px rgba(16,24,40,.06);transition:border-color .15s,box-shadow .15s}' +
    '.igf select{-webkit-appearance:none;appearance:none;padding-right:34px;background:#fff url(\"data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%2716%27 height=%2716%27 viewBox=%270 0 24 24%27 fill=%27none%27 stroke=%27%236b7280%27 stroke-width=%272%27 stroke-linecap=%27round%27 stroke-linejoin=%27round%27%3E%3Cpath d=%27M8 9l4-4 4 4M16 15l-4 4-4-4%27/%3E%3C/svg%3E\") no-repeat right 10px center;cursor:pointer}' +
    '.igf select:hover,.igf .combo:hover{border-color:#cfd5dd}' +
    '.igf select:focus,.igf input:focus{outline:0;border-color:#218a39;box-shadow:0 0 0 3px rgba(33,138,57,.14)}' +
    '.igf select:disabled{opacity:.55}' +
    '.igf .btn{font:inherit;font-size:13.5px;font-weight:600;border-radius:10px;height:38px;padding:0 14px;border:1px solid #218a39;background:#218a39;color:#fff;cursor:pointer}' +
    '.igf .btn.ghost{background:#fff;color:#218a39}.igf .btn.ghost:hover{background:#ecf7ee}' +
    '.igf .btn.sm{height:32px;padding:0 10px;font-size:12.5px;white-space:nowrap}' +
    '.igf .pp{display:flex;gap:8px;align-items:end}.igf .pp:empty{display:none}' +
    '.igf .scope{flex-basis:100%;display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;font-size:12px;color:#637797;border-top:1px solid #edf1f7;padding-top:8px;margin-top:2px}' +
    '.igf .scope b{color:#0b1f3a;font-weight:700}' +
    '.igf .pill{font-size:11px;font-weight:600;border-radius:999px;padding:2px 8px}' +
    '.igf .upd{margin-left:auto;font-weight:600;color:#465f88;white-space:nowrap}.igf .upd+.note{margin-left:12px}.igf .pill.area{background:#eef2ff;color:#3730a3}.igf .pill.on{background:#ecf7ee;color:#176c2b}.igf .pill.off{background:#fdf1d6;color:#8a5a00}' +
    '.igf .note{margin-left:auto;font-style:italic}' +
    '.igf .tag{font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#218a39}' +
    '.igf .field{position:relative}' +
    '.igf .combo{display:flex;align-items:center;justify-content:space-between;gap:8px;font:inherit;font-size:13.5px;height:38px;min-width:150px;max-width:240px;padding:0 10px 0 12px;border:1px solid #e2e5ea;border-radius:10px;background:#fff;color:#111827;cursor:pointer;text-align:left;box-shadow:0 1px 2px rgba(16,24,40,.06);transition:border-color .15s,box-shadow .15s}' +
    '.igf .combo span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '.igf .combo .n{flex:none;font-size:11px;font-weight:700;color:#176c2b;background:#ecf7ee;border-radius:999px;padding:1px 7px}' +
    '.igf .combo:after{content:"";flex:none;width:16px;height:16px;background:url(\"data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%2716%27 height=%2716%27 viewBox=%270 0 24 24%27 fill=%27none%27 stroke=%27%236b7280%27 stroke-width=%272%27 stroke-linecap=%27round%27 stroke-linejoin=%27round%27%3E%3Cpath d=%27M8 9l4-4 4 4M16 15l-4 4-4-4%27/%3E%3C/svg%3E\") no-repeat center}' +
    '.igf .combo:focus-visible,.igf .combo[aria-expanded=true]{outline:0;border-color:#218a39;box-shadow:0 0 0 3px rgba(33,138,57,.14)}' +
    '.igf .combo.set{border-color:#9fd2ab}' +
    '.igf .pop{position:absolute;left:0;top:calc(100% + 4px);z-index:40;width:max(100%,340px);background:#fff;border:1px solid #d7dfec;border-radius:12px;box-shadow:0 18px 40px -16px rgba(11,31,58,.35);padding:8px}' +
    '.igf .pop input[type=search]{width:100%;max-width:none;margin-bottom:6px}' +
    '.igf .pop .count{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:#465f88;padding:2px 4px 6px}' +
    '.igf .pop .count b{color:#0b1f3a}' +
    '.igf .pop ul{list-style:none;margin:0;padding:0;max-height:300px;overflow:auto;border-top:1px solid #edf1f7;border-bottom:1px solid #edf1f7}' +
    '.igf .pop li{padding:6px 8px;border-radius:7px;cursor:pointer;font-size:13px;color:#0b1f3a;display:flex;align-items:center;gap:9px}' +
    '.igf .pop li .t{flex:1;min-width:0}.igf .pop li small{color:#637797;font-size:11px;white-space:nowrap;margin-left:auto}' +
    '.igf .pop li input{flex:none;width:16px;height:16px;margin:0;padding:0;box-shadow:none;accent-color:#218a39;cursor:pointer}' +
    '.igf .pop li.active{background:#ecf7ee}' +
    '.igf .pop li.all{font-weight:700;border-bottom:1px solid #edf1f7;border-radius:7px 7px 0 0}' +
    '.igf .pop li.group{cursor:default;font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#637797;padding:9px 8px 3px}' +
    '.igf .pop .more{font-size:11.5px;color:#637797;padding:6px 8px 0}.igf .pop .more:empty{display:none}' +
    '.igf .pop .acts{display:flex;gap:6px;align-items:center;padding-top:8px}.igf .pop .acts .sp{flex:1}' +
    '.igf .pop .hint{font-size:11.5px;color:#8a5a00;padding:6px 4px 0}.igf .pop .hint:empty{display:none}';
  document.head.appendChild(css);

  // ------------------------------------------------------------------ multiple-selection dropdown
  // A button that opens a checkbox list with a search box, an "All …" row, Select all / Clear all and
  // the number selected. Ticks are applied when the list closes (Apply, Enter on the search box,
  // clicking outside); Esc closes without applying. Nothing ticked means all.
  // Options: {value,label,sub,group}. An empty selection ([]) means "all"; every box then shows ticked.
  var MAX_SHOWN = 300;
  function Multi(key, label, noun, getOptions, onApply) {
    this.key = key; this.label = label; this.noun = noun; this.getOptions = getOptions; this.onApply = onApply;
    this.value = []; this.draft = null; this.shown = []; this.matches = []; this.active = -1;
  }
  Multi.prototype.id = function (s) { return 'igf-' + this.key + (s ? '-' + s : ''); };
  Multi.prototype.html = function () {
    var k = this;
    return '<div class="field"><span class="lbl" id="' + k.id('label') + '">' + esc(k.label) + '</span>' +
      '<button type="button" class="combo" id="' + k.id() + '" aria-haspopup="dialog" aria-expanded="false" aria-labelledby="' + k.id('label') + ' ' + k.id() + '"><span></span><b class="n" hidden></b></button>' +
      '<div class="pop" id="' + k.id('pop') + '" role="dialog" aria-labelledby="' + k.id('label') + '" hidden>' +
      '<input type="search" id="' + k.id('q') + '" placeholder="Search ' + esc(k.noun.plural) + '…" autocomplete="off" aria-controls="' + k.id('list') + '">' +
      '<div class="count" id="' + k.id('count') + '" aria-live="polite"></div>' +
      '<ul id="' + k.id('list') + '" role="listbox" aria-multiselectable="true" aria-labelledby="' + k.id('label') + '"></ul>' +
      '<div class="more" id="' + k.id('more') + '"></div><div class="hint" id="' + k.id('hint') + '"></div>' +
      '<div class="acts"><button type="button" class="btn ghost sm" id="' + k.id('clear') + '">Clear All</button><button type="button" class="btn ghost sm" id="' + k.id('all') + '">Select All</button><span class="sp"></span>' +
      '<button type="button" class="btn sm" id="' + k.id('apply') + '">Apply</button></div></div></div>';
  };
  Multi.prototype.values = function () { return this.getOptions().map(function (o) { return o.value; }); };
  // The applied selection as the list of ticked values (all of them when the filter is "all").
  Multi.prototype.ticked = function () {
    var all = this.values(), v = this.value.filter(function (x) { return all.indexOf(x) >= 0; });
    return v.length ? v : all;
  };
  Multi.prototype.setValue = function (value) {
    this.value = value.slice();
    var b = el(this.id()); if (!b) return;
    var opts = this.getOptions(), sel = opts.filter(function (o) { return value.indexOf(o.value) >= 0; });
    var all = !sel.length || sel.length === opts.length, text;
    if (all) text = 'All ' + this.noun.plural; else if (sel.length === 1) text = sel[0].label; else text = sel.length + ' ' + this.noun.plural;
    b.querySelector('span').textContent = text;
    var n = b.querySelector('.n'); n.hidden = !opts.length; n.textContent = all ? opts.length + '/' + opts.length : sel.length + '/' + opts.length;
    n.title = (all ? opts.length : sel.length) + ' of ' + opts.length + ' selected';
    b.classList.toggle('set', !all);
    b.title = all ? 'All ' + this.noun.plural + ' (' + opts.length + ')' : sel.map(function (o) { return o.label; }).join(', ');
  };
  Multi.prototype.setDisabled = function (d) { var b = el(this.id()); if (b) b.disabled = d; };
  Multi.prototype.isOpen = function () { var p = el(this.id('pop')); return p && !p.hidden; };
  Multi.prototype.open = function () {
    closeAll(this);
    this.draft = new Set(this.ticked());
    el(this.id('pop')).hidden = false; el(this.id()).setAttribute('aria-expanded', 'true');
    var q = el(this.id('q')); q.value = ''; this.active = -1; this.render(); q.focus();
  };
  // apply: true = use the ticks, false = discard them.
  Multi.prototype.close = function (apply, focusButton) {
    if (!this.isOpen()) return;
    el(this.id('pop')).hidden = true; el(this.id()).setAttribute('aria-expanded', 'false');
    if (focusButton) el(this.id()).focus();
    var draft = this.draft; this.draft = null;
    if (!apply || !draft) return;
    var all = this.values(), picked = all.filter(function (v) { return draft.has(v); });
    var next = !picked.length || picked.length === all.length ? [] : picked; // none or every box ticked = all
    if (next.join(',') !== this.value.join(',')) this.onApply(next);
  };
  Multi.prototype.render = function () {
    var self = this, words = el(this.id('q')).value.toLowerCase().split(/\s+/).filter(Boolean), opts = this.getOptions();
    this.matches = opts.filter(function (o) {
      var hay = (o.label + ' ' + (o.sub || '') + ' ' + (o.group || '')).toLowerCase();
      return words.every(function (w) { return hay.indexOf(w) >= 0; });
    });
    this.shown = this.matches.slice(0, MAX_SHOWN);
    var draft = this.draft, n = opts.filter(function (o) { return draft.has(o.value); }).length;
    var allOn = n === opts.length && n > 0, k = this.key, html = '', lastGroup = null;
    if (!words.length && opts.length) {
      html += '<li class="all" role="option" data-i="-1" id="' + this.id('o-1') + '" aria-selected="' + allOn + '"><input type="checkbox" tabindex="-1"' + (allOn ? ' checked' : '') + (n && !allOn ? ' data-mixed="1"' : '') + '><span class="t">All ' + esc(this.noun.plural) + '</span><small>' + fmt(opts.length) + '</small></li>';
    }
    this.shown.forEach(function (o, i) {
      if (o.group && o.group !== lastGroup) { html += '<li class="group" role="presentation">' + esc(o.group) + '</li>'; lastGroup = o.group; }
      var on = draft.has(o.value);
      html += '<li role="option" id="igf-' + k + '-o' + i + '" data-i="' + i + '" aria-selected="' + on + '"><input type="checkbox" tabindex="-1"' + (on ? ' checked' : '') + '><span class="t">' + esc(o.label) + '</span>' + (o.sub ? '<small>' + esc(o.sub) + '</small>' : '') + '</li>';
    });
    var list = el(this.id('list'));
    list.innerHTML = html || '<li class="group" role="presentation">No matches</li>';
    list.querySelectorAll('input[data-mixed]').forEach(function (c) { c.indeterminate = true; });
    el(this.id('count')).innerHTML = '<span><b>' + fmt(n) + ' of ' + fmt(opts.length) + '</b> selected</span>' + (words.length ? '<span>' + fmt(this.matches.length) + ' match' + (this.matches.length === 1 ? '' : 'es') + '</span>' : '');
    el(this.id('more')).textContent = this.matches.length > MAX_SHOWN ? 'Showing the first ' + MAX_SHOWN + ' of ' + fmt(this.matches.length) + '. Type to narrow the list; Select All ticks all ' + fmt(this.matches.length) + '.' : '';
    el(this.id('hint')).textContent = n ? '' : 'Nothing ticked: all ' + this.noun.plural + ' will be included.';
    el(this.id('all')).textContent = words.length ? 'Select All matches' : 'Select All';
    el(this.id('clear')).textContent = words.length ? 'Clear matches' : 'Clear All';
    this.highlight();
  };
  Multi.prototype.toggle = function (i) {
    if (i === -1) { var on = this.draft.size === this.values().length; this.setAll(!on, false); return; }
    var o = this.shown[i]; if (!o) return;
    if (this.draft.has(o.value)) this.draft.delete(o.value); else this.draft.add(o.value);
    this.render();
  };
  // Select All / Clear All act on the search matches, or on everything when there is no search.
  Multi.prototype.setAll = function (on, matchesOnly) {
    var d = this.draft, list = matchesOnly ? this.matches : this.getOptions();
    list.forEach(function (o) { if (on) d.add(o.value); else d.delete(o.value); });
    this.render();
  };
  Multi.prototype.highlight = function () {
    var self = this, list = el(this.id('list'));
    list.querySelectorAll('li[role=option]').forEach(function (li) { li.classList.toggle('active', +li.dataset.i === self.active); });
    var a = list.querySelector('li[data-i="' + this.active + '"]');
    el(this.id('q')).setAttribute('aria-activedescendant', a ? a.id : '');
    if (a) a.scrollIntoView({ block: 'nearest' });
  };
  Multi.prototype.bind = function () {
    var self = this;
    el(this.id()).addEventListener('click', function () { if (self.isOpen()) self.close(true); else self.open(); });
    el(this.id('q')).addEventListener('input', function () { self.active = this.value ? 0 : -1; self.render(); });
    el(this.id('q')).addEventListener('keydown', function (e) {
      var first = this.value ? 0 : -1;
      if (e.key === 'ArrowDown') { e.preventDefault(); self.active = Math.min(self.shown.length - 1, self.active + 1); self.highlight(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); self.active = Math.max(first, self.active - 1); self.highlight(); }
      else if (e.key === ' ' && !this.value) { e.preventDefault(); self.toggle(self.active); } // with a search typed, Space is a space
      else if (e.key === 'Enter') { e.preventDefault(); self.close(true, true); }
      else if (e.key === 'Escape') { e.preventDefault(); self.close(false, true); }
    });
    el(this.id('list')).addEventListener('mousedown', function (e) {
      var li = e.target.closest('li[role=option]'); if (!li) return;
      // The list is redrawn on toggle, so the clicked row leaves the page: stop here, or the page-wide
      // handler would take it for a click outside and close the list.
      e.preventDefault(); e.stopPropagation(); self.active = +li.dataset.i; self.toggle(+li.dataset.i); el(self.id('q')).focus();
    });
    el(this.id('all')).addEventListener('click', function () { self.setAll(true, !!el(self.id('q')).value); });
    el(this.id('clear')).addEventListener('click', function () { self.setAll(false, !!el(self.id('q')).value); });
    el(this.id('apply')).addEventListener('click', function () { self.close(true, true); });
    el(this.id('pop')).addEventListener('keydown', function (e) { if (e.key === 'Escape') { e.preventDefault(); self.close(false, true); } });
  };
  var combos = {};
  function closeAll(except) { Object.keys(combos).forEach(function (k) { if (combos[k] !== except) combos[k].close(true); }); }
  document.addEventListener('mousedown', function (e) {
    var f = e.target.closest && e.target.closest('.igf .field');
    Object.keys(combos).forEach(function (k) { if (!f || !f.contains(el(combos[k].id()))) combos[k].close(true); });
  });
  // Clicks inside the Overview or register frames never reach this document; the portal window
  // loses focus instead, so apply and close any open list then.
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
  function only(list, allowed) { return allowed ? list.filter(function (x) { return allowed.indexOf(x) >= 0; }) : list; }
  // Keep the selection inside the account's area and the geographical hierarchy.
  function clamp(next) {
    if (!geo) return next;
    var a = area();
    next.provinces = only(next.provinces, allowedProvinceIds());
    if (a && a.level === 'district') next.districts = only(next.districts, a.areas);
    if (a && a.level === 'facility') next.facilities = only(next.facilities, a.areas);
    // Districts must sit inside the chosen provinces.
    if (next.provinces.length) next.districts = next.districts.filter(function (d) { return next.provinces.indexOf(provinceOfDistrict(d)) >= 0; });
    next.types = next.types.filter(function (t) { return TYPES.some(function (x) { return x[0] === t; }); });
    if (next.types.length === TYPES.length) next.types = [];
    return next;
  }
  function areaLabel() {
    var a = area(); if (!a) return '';
    var list = a.level === 'province' ? a.areas.map(function (id) { var p = geo.provinces.find(function (x) { return x.id === id; }); return p ? p.name : id; })
      : a.level === 'district' ? a.areas.map(function (id) { var d; geo.provinces.some(function (p) { d = p.districts.find(function (x) { return x.id === id; }); return d; }); return d ? d.name : id; })
      : a.areas.map(function (id) { var f = allowedFacilities.find(function (x) { return x.id === id; }); return f ? f.name : id; });
    return list.join(', ');
  }

  // ------------------------------------------------------------------ options (cascading)
  function provinceOptions() {
    var allowed = allowedProvinceIds();
    return (geo ? geo.provinces : []).filter(function (p) { return !allowed || allowed.indexOf(p.id) >= 0; })
      .map(function (p) { return { value: p.id, label: p.name, sub: p.districts.length + ' districts' }; });
  }
  // Districts of the chosen provinces (all provinces when none is chosen), grouped by province.
  function districtOptions() {
    var provs = geo ? geo.provinces.filter(function (p) { return !filters.provinces.length || filters.provinces.indexOf(p.id) >= 0; }) : [];
    var a = area(), allowed = a && a.level === 'district' ? a.areas : a && a.level === 'facility' ? allowedFacilities.map(function (f) { return f.districtId; }) : null;
    var allowedP = allowedProvinceIds(), opts = [];
    provs.forEach(function (p) {
      if (allowedP && allowedP.indexOf(p.id) < 0) return;
      p.districts.forEach(function (d) { if (allowed && allowed.indexOf(d.id) < 0) return; opts.push({ value: d.id, label: d.name, group: provs.length > 1 ? p.name : null, provinceId: p.id }); });
    });
    return opts;
  }
  function typeOptions() { return TYPES.map(function (t) { return { value: t[0], label: t[1], group: t[2] }; }); }
  // Facilities in the chosen provinces, districts and types (fetched by refresh()).
  function facilityOptions() {
    var ar = area(), allowed = ar && ar.level === 'facility' ? ar.areas : null, many = filters.provinces.length !== 1;
    return facilities.filter(function (f) { return !allowed || allowed.indexOf(f.id) >= 0; })
      .sort(function (a, b) { return (b.hasData - a.hasData) || a.name.localeCompare(b.name); })
      .map(function (f) { return { value: f.id, label: f.name, sub: (f.hasData ? 'Reporting · ' : '') + (f.district || '') + (many ? ', ' + (f.province || '') : ''), facility: f }; });
  }
  var NOUN = function (one, many) { return { one: one, plural: many }; };
  combos.province = new Multi('province', 'Province', NOUN('province', 'provinces'), provinceOptions, function (v) { setFilters({ provinces: v }); });
  combos.district = new Multi('district', 'District', NOUN('district', 'districts'), districtOptions, function (v) { setFilters({ districts: v }); });
  combos.type = new Multi('type', 'Facility type', NOUN('facility type', 'facility types'), typeOptions, function (v) { setFilters({ types: v }); });
  combos.facility = new Multi('facility', 'Facility', NOUN('facility', 'facilities'), facilityOptions, function (v) { setFilters({ facilities: v }); });

  // ------------------------------------------------------------------ bar
  function periodSelect() {
    return '<div><label for="igf-period">Reporting period</label><select id="igf-period">' + PERIODS.map(function (g) {
      return '<optgroup label="' + esc(g[0]) + '">' + g[1].map(function (p) { return '<option value="' + p[0] + '"' + (p[0] === filters.period ? ' selected' : '') + '>' + esc(p[1]) + '</option>'; }).join('') + '</optgroup>';
    }).join('') + '</select></div>';
  }
  function yearOptions(sel) {
    var out = '', last = Math.max(new Date().getFullYear(), +(anchor().slice(0, 4)));
    for (var y = last; y >= FIRST_YEAR; y--) out += '<option value="' + y + '"' + (String(y) === String(sel) ? ' selected' : '') + '>' + y + '</option>';
    return out;
  }
  function opts(list, sel) { return list.map(function (o) { return '<option value="' + o[0] + '"' + (o[0] === String(sel) ? ' selected' : '') + '>' + esc(o[1]) + '</option>'; }).join(''); }
  // The extra controls a period needs: a month, a quarter and year, a half-year and year, a year, a start week or dates.
  function periodControls() {
    var f = filters, d = defaults(), r = resolvePeriod(f), q, h;
    switch (f.period) {
      case 'month': return '<div><label for="igf-month">Month</label><input type="month" id="igf-month" value="' + esc(f.month || d.month) + '" required></div>';
      case 'quarter': q = f.quarter || d.quarter;
        return '<div><label for="igf-pq">Quarter</label><select id="igf-pq">' + opts(QUARTERS, q.slice(-1)) + '</select></div><div><label for="igf-pyear">Year</label><select id="igf-pyear">' + yearOptions(q.slice(0, 4)) + '</select></div>';
      case 'half': h = f.half || d.half;
        return '<div><label for="igf-ph">Half-year</label><select id="igf-ph">' + opts(HALVES, h.slice(-1)) + '</select></div><div><label for="igf-pyear">Year</label><select id="igf-pyear">' + yearOptions(h.slice(0, 4)) + '</select></div>';
      case 'year': return '<div><label for="igf-pyear">Year</label><select id="igf-pyear">' + yearOptions(f.year || d.year) + '</select></div>';
      case 'biweekly': return '<div><label for="igf-week">Starting week</label><input type="week" id="igf-week" value="' + esc(f.week || d.week) + '" required title="The two weeks start on the Monday of this week"></div>';
      case 'custom': return '<div><label for="igf-from">From</label><input type="date" id="igf-from" value="' + esc(f.from || r.from) + '"></div><div><label for="igf-to">To</label><input type="date" id="igf-to" value="' + esc(f.to || r.to) + '"></div><button class="btn" id="igf-apply" type="button">Apply</button>';
    }
    return '';
  }
  function bindPeriodControls() {
    var on = function (id, fn) { var x = el(id); if (x) x.addEventListener('change', fn); };
    on('igf-month', function () { if (this.value) setFilters({ month: this.value }); });
    on('igf-pq', function () { setFilters({ quarter: el('igf-pyear').value + '-Q' + this.value }); });
    on('igf-ph', function () { setFilters({ half: el('igf-pyear').value + '-H' + this.value }); });
    on('igf-pyear', function () {
      if (filters.period === 'quarter') setFilters({ quarter: this.value + '-Q' + el('igf-pq').value });
      else if (filters.period === 'half') setFilters({ half: this.value + '-H' + el('igf-ph').value });
      else setFilters({ year: this.value });
    });
    on('igf-week', function () { if (this.value) setFilters({ week: this.value }); });
    var apply = el('igf-apply');
    if (apply) apply.addEventListener('click', function () {
      var from = el('igf-from').value, to = el('igf-to').value;
      if (!from || !to || from > to) { el('igf-to').setCustomValidity('Choose a start date on or before the end date.'); el('igf-to').reportValidity(); return; }
      el('igf-to').setCustomValidity(''); setFilters({ period: 'custom', from: from, to: to });
    });
  }
  function renderPeriodControls() {
    var box = el('igf-pp'); if (!box) return;
    var html = periodControls();
    if (box.dataset.html !== html) { box.innerHTML = html; box.dataset.html = html; bindPeriodControls(); }
  }
  function barHtml() {
    return '<div class="igf" role="group" aria-label="Filters for the whole portal">' +
      combos.province.html() + combos.district.html() + combos.type.html() + combos.facility.html() +
      periodSelect() + '<div class="pp" id="igf-pp"></div>' +
      '<div style="margin-left:auto"><button class="btn ghost" id="igf-reset" type="button">Reset filters</button></div>' +
      '<div class="scope" id="igf-scope" aria-live="polite"><span class="tag">Filters apply across the portal</span><span>Loading facility registry…</span></div>' +
      '</div>';
  }

  function syncControls() {
    if (!el('igf-province') || !geo) return;
    combos.province.setValue(filters.provinces); combos.district.setValue(filters.districts);
    combos.type.setValue(filters.types); combos.facility.setValue(filters.facilities);
    var a = area();
    combos.province.setDisabled(provinceOptions().length < 2 && !!a);
    combos.district.setDisabled(!districtOptions().length);
    el('igf-period').value = filters.period;
    renderPeriodControls();
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
      '<span>Showing <b>' + esc(r.label) + '</b>' + (r.typeLabel ? ' · ' + esc(r.typeLabel) : '') + ' · ' + esc(r.periodLabel) + ': <b>' + (r.from === r.to ? day(r.from) : day(r.from) + ' – ' + day(r.to)) + '</b></span>' +
      '<span>' + fmt(r.facilityCount) + ' ' + (r.facilityCount === 1 ? 'facility' : 'facilities') + '</span>' +
      (area() ? '<span class="pill area" title="Your account covers this area only">Your area: ' + esc(areaLabel()) + '</span>' : '') +
      (r.reportingIds.length ? '<span class="pill on">' + fmt(r.reportingIds.length) + ' reporting: ' + esc(names(r.reportingNames)) + '</span>' : '<span class="pill off">No facility in this selection reports data yet</span>') +
      (latestDate && r.from > latestDate ? '<span class="pill off" title="The newest record in the warehouse is from ' + esc(day(latestDate)) + '">No data received for this period yet (data ends ' + esc(day(latestDate)) + ')</span>' : '') +
      (lastUpdated ? '<span class="upd" title="Time of the newest record received from reporting facilities">' + esc(lastUpdatedText()) + '</span>' : '') +
      (active === 'workspace' ? '<span class="note">The workspace has its own period and filters.</span>' : active === 'users' ? '<span class="note">Filters don\'t affect user management.</span>' : active === 'reports' ? '<span class="note">Each report keeps its own place and period. Use \'Use portal filters\' in a report to copy these in.</span>' : active === 'geo' ? '<span class="note">The map follows one province and the facility types. Its period is set on the map\'s timeline.</span>' : applies ? '' : active === 'config' ? '<span class="note">Filters don\'t affect configuration.</span>' : '<span class="note">This section uses demonstration data, so these filters don\'t change it.</span>');
    // Only touch the DOM when the text changes: the page observer below would otherwise loop.
    if (box.innerHTML !== html && box.dataset.html !== html) { box.innerHTML = html; box.dataset.html = html; }
  }

  // ------------------------------------------------------------------ state
  // patch may use the lists (provinces, districts, types, facilities) or, from pages that predate
  // multiple selection, single IDs (province, district, type, facility; '' = all).
  function setFilters(patch) {
    patch = Object.assign({}, patch);
    var legacy = { province: 'provinces', district: 'districts', type: 'types', facility: 'facilities' };
    Object.keys(legacy).forEach(function (k) {
      if (!(k in patch)) return;
      var v = arr(patch[k]); if (k === 'type' && v.indexOf('HOSPITAL') >= 0) v = HOSPITALS;
      patch[legacy[k]] = v; delete patch[k];
      // A drill-down to one place replaces the levels below it, as before.
      if (k === 'province' && !('district' in patch) && !('districts' in patch)) patch.districts = [];
      if ((k === 'province' || k === 'district' || k === 'type') && !('facility' in patch) && !('facilities' in patch)) patch.facilities = [];
    });
    var next = Object.assign({}, filters, patch);
    ['provinces', 'districts', 'types', 'facilities'].forEach(function (k) { next[k] = arr(next[k]); });
    if (next.period !== 'custom') { next.from = ''; next.to = ''; }
    if (PERIOD_IDS.indexOf(next.period) < 0) next.period = 'data-12';
    filters = clamp(next); save(); syncControls(); refresh();
  }

  // The facility list only depends on provinces, districts and types: a new period reuses it.
  var listKey = null;
  async function refresh() {
    var my = ++seq;
    var q = new URLSearchParams();
    if (filters.provinces.length) q.set('province', filters.provinces.join(','));
    if (filters.districts.length) q.set('district', filters.districts.join(','));
    if (filters.types.length) q.set('type', filters.types.join(','));
    try {
      if (listKey !== String(q)) {
        var body = await (await fetch(API + '/facilities?' + q, { cache: 'no-store' })).json();
        if (my !== seq) return;
        facilities = body.data; listKey = String(q);
      }
      // Facilities outside the chosen provinces, districts or types drop out of the selection.
      var keep = filters.facilities.filter(function (id) { return facilities.some(function (f) { return f.id === id; }); });
      if (keep.length !== filters.facilities.length) { filters.facilities = keep; save(); }
    } catch (e) { if (my === seq) showError(); return; }
    var chosen = filters.facilities;
    var inScope = chosen.length ? facilities.filter(function (f) { return chosen.indexOf(f.id) >= 0; }) : facilities;
    var reporting = inScope.filter(function (f) { return f.hasData; });
    var period = resolvePeriod(filters);
    var path = ['Zimbabwe'], provName = function (id) { var p = geo.provinces.find(function (x) { return x.id === id; }); return p ? p.name : id; };
    var distName = function (id) { var d = null; geo.provinces.some(function (p) { d = p.districts.find(function (x) { return x.id === id; }); return d; }); return d ? d.name : id; };
    if (filters.provinces.length) path.push(names(filters.provinces.map(provName)));
    if (filters.districts.length) path.push(names(filters.districts.map(distName)));
    if (chosen.length) path.push(chosen.length <= 3 ? inScope.map(function (f) { return f.name; }).join(', ') : fmt(chosen.length) + ' facilities');
    var typeLabel = filters.types.length ? names(filters.types.map(function (t) { return (TYPES.find(function (x) { return x[0] === t; }) || [t, t])[1]; })) : null;
    resolved = {
      from: period.from, to: period.to, periodLabel: period.label, period: filters.period, latestDataDate: latestDate,
      label: path.join(' › '), typeLabel: typeLabel,
      facilityCount: inScope.length, reportingIds: reporting.map(function (f) { return f.id; }), reportingNames: reporting.map(function (f) { return f.name; }),
      facility: chosen.length === 1 ? inScope[0] || null : null,
      provinces: filters.provinces.slice(), districts: filters.districts.slice(), types: filters.types.slice(), facilities: chosen.slice()
    };
    syncControls(); renderScope(); broadcast();
  }

  function message() {
    var f = filters, one = function (list) { return list.length === 1 ? list[0] : ''; };
    var legacy = { province: one(f.provinces), district: one(f.districts), type: f.types.join(','), facility: one(f.facilities) };
    // One district without a province: older pages expect its province too.
    if (!legacy.province && legacy.district) legacy.province = provinceOfDistrict(legacy.district);
    if (!legacy.province && resolved && resolved.facility) { legacy.province = resolved.facility.provinceId || ''; legacy.district = resolved.facility.districtId || ''; }
    return { type: 'impilo-filters', filters: Object.assign({}, f, legacy), resolved: resolved };
  }
  function broadcast() {
    if (!resolved) return;
    var m = message();
    document.querySelectorAll('iframe').forEach(function (f) { try { f.contentWindow.postMessage(m, '*'); } catch (e) { } });
    try { window.dispatchEvent(new CustomEvent('impilo-filters', { detail: m })); } catch (e) { }
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
      if (this.value === 'custom') { var r = resolvePeriod(filters); filters.period = 'custom'; filters.from = r.from; filters.to = r.to; save(); syncControls(); }
      else setFilters({ period: this.value });
    });
    el('igf-reset').addEventListener('click', function () { setFilters(JSON.parse(JSON.stringify(DEFAULTS))); });
  }
  function mount() {
    var host = document.getElementById('impilo-global-filters');
    if (!host || host.querySelector('.igf')) return;
    // Outside the portal (no portal header) the bar sticks to the top of the page.
    if (!document.querySelector('nav[aria-label="Portal sections"]') && host.dataset.standalone) host.style.top = '8px';
    host.innerHTML = barHtml(); bind();
    if (ready) { syncControls(); renderScope(); }
  }
  // React may re-create the shell; re-mount the bar whenever its host is empty, and refresh the
  // "applies to this section" note when the active section changes.
  new MutationObserver(function () { mount(); renderScope(); showLastUpdated(); }).observe(document.body || document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['aria-current'] });

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
  window.ImpiloFilters = {
    get: function () { return message().filters; }, resolved: function () { return resolved; }, set: setFilters,
    // For pages and tests: the dates a period gives, e.g. ImpiloFilters.period({period:'quarter', quarter:'2025-Q3'}).
    period: function (f) { return resolvePeriod(normalise(Object.assign({}, filters, f))); }
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
