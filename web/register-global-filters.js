/* Register pages: follow the portal-wide filters when shown inside the portal.
 * Standalone (opened directly), a register keeps its own filter controls.
 * Relies on the register page's own globals: state, facilityMetadata and the #attendanceFilters form.
 */
(function () {
  'use strict';
  if (window.parent === window) return;

  var css = document.createElement('style');
  css.textContent = '.filter-card{display:none!important}' +
    '#impiloScope{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;font-size:12px;color:#466b4f;background:#f4faf5;border:1px solid #dceadd;border-radius:12px;padding:9px 12px;margin:0 0 14px}' +
    '#impiloScope b{color:#14532d}' +
    '#impiloEmpty{display:none;text-align:center;padding:36px 20px;border:1px solid #dceadd;border-radius:16px;background:#fff;color:#14532d}' +
    '#impiloEmpty h3{margin:0 0 6px;font-size:16px}#impiloEmpty p{margin:0 auto;max-width:560px;line-height:1.5;color:#466b4f}' +
    'body.impilo-empty #impiloEmpty{display:block}' +
    'body.impilo-empty .summary-section,body.impilo-empty #attendancePanel,body.impilo-empty .chart-grid,body.impilo-empty .facility-bootstrap{display:none!important}';
  document.head.appendChild(css);

  var latest = null, applied = '';
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function day(d) { return new Date(d + 'T00:00:00').toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }); }

  function ensureBanner() {
    if (document.getElementById('impiloScope')) return;
    var anchor = document.querySelector('.facility-bootstrap') || document.querySelector('.filter-card');
    if (!anchor) return;
    var scope = document.createElement('div'); scope.id = 'impiloScope'; scope.setAttribute('role', 'status');
    var empty = document.createElement('div'); empty.id = 'impiloEmpty';
    anchor.parentNode.insertBefore(scope, anchor); anchor.parentNode.insertBefore(empty, anchor);
  }

  function pageReady() {
    try { return typeof facilityMetadata !== 'undefined' && facilityMetadata && !document.getElementById('attendanceFrom').disabled; } catch (e) { return false; }
  }

  function apply() {
    if (!latest || !latest.resolved) return;
    ensureBanner();
    var r = latest.resolved, f = latest.filters;
    var scope = document.getElementById('impiloScope'), empty = document.getElementById('impiloEmpty');
    if (scope) scope.innerHTML = '<span>Showing <b>' + esc(r.label) + '</b>' + (r.typeLabel ? ' · ' + esc(r.typeLabel) : '') + ' · ' + (r.periodLabel ? esc(r.periodLabel) + ': ' : '') + '<b>' + (r.from === r.to ? day(r.from) : day(r.from) + ' – ' + day(r.to)) + '</b></span>' +
      '<span>Change the selection in the filter bar at the top of the portal.</span>';
    if (!r.reportingIds.length) {
      document.body.classList.add('impilo-empty');
      if (empty) empty.innerHTML = '<h3>No register data for this selection yet</h3><p>None of the ' + Number(r.facilityCount).toLocaleString('en-GB') +
        ' facilities in ' + esc(r.label.split(' › ').pop()) + (r.typeLabel ? ' (' + esc(r.typeLabel.toLowerCase()) + ')' : '') +
        ' send data to the national warehouse yet. Records will appear here as facilities are connected.</p>';
      applied = '';
      return;
    }
    document.body.classList.remove('impilo-empty');
    if (!pageReady()) { setTimeout(apply, 200); return; }
    // Single-tenant database: the register API takes the reporting facility's ID, or '' for all
    // reporting facilities. Multi-tenant: pass every reporting facility in scope instead.
    var facility = f.facility && r.reportingIds.indexOf(f.facility) >= 0 ? f.facility : '';
    var select = document.getElementById('facilityFilter');
    if (facility && !Array.prototype.some.call(select.options, function (o) { return o.value === facility; })) facility = '';
    var key = [r.from, r.to, facility].join('|');
    if (key === applied && state.applied.from === r.from && state.applied.to === r.to) return;
    applied = key;
    document.getElementById('periodPreset').value = 'custom';
    document.getElementById('attendanceFrom').value = r.from;
    document.getElementById('attendanceTo').value = r.to;
    select.value = facility;
    var form = document.getElementById('attendanceFilters');
    if (form.requestSubmit) form.requestSubmit(); else form.dispatchEvent(new Event('submit', { cancelable: true }));
  }

  window.addEventListener('message', function (e) {
    if (e.source !== window.parent) return;
    var d = e.data || {};
    if (d.type === 'impilo-filters') { latest = d; apply(); }
  });
  function ask() { window.parent.postMessage({ type: 'impilo-filters-request' }, '*'); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { ensureBanner(); ask(); }); else { ensureBanner(); ask(); }
})();
