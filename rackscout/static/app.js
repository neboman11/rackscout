'use strict';

// ---------------------------------------------------------------------------
// Constants and state
// ---------------------------------------------------------------------------

const DEFAULTS = {
  vendor: '',
  model: '',
  generation: '',
  height: '1',
  min_ram: '16',
  min_bays: '4',
  drive_size: '',
  cpu_count: '',
  max_total: '',
  diskless: 'true',
  hide_damaged: 'true',
  modern_only: 'true',
  confirmed_only: 'false',
  include_leads: 'true',
  q: '',
  sort: 'price',
  page: '1',
};
const SELECT_KEYS = ['vendor', 'model', 'generation', 'height', 'min_ram', 'min_bays', 'drive_size', 'cpu_count'];
const CHECK_KEYS = ['diskless', 'hide_damaged', 'modern_only', 'confirmed_only', 'include_leads'];

// Used only when /api/facets is unavailable; the backend facets are authoritative.
const FALLBACK_VENDORS = ['Dell', 'HPE', 'Supermicro', 'Lenovo', 'Cisco'];
const FALLBACK_MODEL_VENDORS = {
  R240: 'Dell', R340: 'Dell', R440: 'Dell', R640: 'Dell', R6415: 'Dell',
  'HPE DL360 Gen10': 'HPE', 'HPE DL160 Gen10': 'HPE', 'HPE DL325 Gen10': 'HPE',
  'Supermicro SYS-1029U-TR4': 'Supermicro',
  'Lenovo SR630': 'Lenovo', 'Lenovo SR530': 'Lenovo',
  'Cisco C220 M5': 'Cisco',
};
const GENERATION_PREFIX = { HPE: 'Gen', Supermicro: 'X', Lenovo: 'Generation ', Cisco: 'M' };
let facetData = {
  vendors: FALLBACK_VENDORS,
  models: Object.keys(FALLBACK_MODEL_VENDORS),
  model_vendors: FALLBACK_MODEL_VENDORS,
  generations_by_vendor: { Dell: [14], HPE: [10], Supermicro: [11], Lenovo: [1], Cisco: [5] },
  generation_labels: {},
};
const PAGE_SIZE = 50;
const SEARCH_DELAY_MS = 350;
const DEST_KEY = 'rackscout.destinationPreference';
const MAX_TOTAL_PATTERN = /^\d{1,9}(\.\d{1,2})?$/;

let state = readStateFromUrl();
let listSeq = 0;
let listController = null;
let statusSeq = 0;
let detailSeq = 0;
let debounceTimer = 0;
let opener = null;
let openerId = null;
const rowStates = new Map();
let lastPages = 1;

const $ = id => document.getElementById(id);

// ---------------------------------------------------------------------------
// DOM helpers (all untrusted text goes through textContent)
// ---------------------------------------------------------------------------

function el(tag, options, children) {
  const node = document.createElement(tag);
  if (options) {
    if (options.className) node.className = options.className;
    if (options.text != null) node.textContent = options.text;
    if (options.attrs) {
      for (const [name, value] of Object.entries(options.attrs)) node.setAttribute(name, value);
    }
  }
  for (const child of children || []) {
    if (child != null) node.append(child);
  }
  return node;
}

function small(text) {
  return el('small', { text });
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

const intFormat = new Intl.NumberFormat('en-US');

function formatInt(value) {
  return intFormat.format(value);
}

// Decimal strings are formatted by string manipulation only; never converted to float.
function formatMoney(value) {
  if (typeof value !== 'string' || !/^-?\d+(\.\d+)?$/.test(value)) return null;
  const negative = value.startsWith('-');
  const [whole, fraction = ''] = value.replace('-', '').split('.');
  const grouped = whole.replace(/^0+(?=\d)/, '').replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return (negative ? '-$' : '$') + grouped + '.' + fraction.padEnd(2, '0');
}

function formatDate(value) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

function populatedLabel(count) {
  if (count == null) return 'Unknown';
  if (count === 1) return 'Single populated';
  if (count === 2) return 'Dual populated';
  return count + ' populated';
}

function socketLabel(item) {
  const populated = item.cpu_count == null ? '?' : item.cpu_count;
  const sockets = item.socket_capacity == null ? '?' : item.socket_capacity;
  return populated + ' of ' + sockets;
}

function driveSizeLabel(size, long) {
  if (size === 'SFF') return long ? '2.5-inch SFF' : '2.5″';
  if (size === 'LFF') return long ? '3.5-inch LFF' : '3.5″';
  return long ? 'Unknown size' : '?';
}

function modelName(vendor, model) {
  if (!model) return null;
  if (!vendor) return model;
  if (model.toLowerCase().startsWith(vendor.toLowerCase() + ' ')) return model;
  if (vendor === 'Dell') return 'Dell PowerEdge ' + model;
  return vendor + ' ' + model;
}

function modelTitle(item) {
  return modelName(item.vendor, item.model) || ((item.vendor || 'Server') + ' (model unknown)');
}

function generationLabel(vendor, number) {
  const byVendor = (facetData.generation_labels || {})[vendor];
  const found = byVendor && typeof byVendor === 'object' ? byVendor[String(number)] : null;
  if (typeof found === 'string' && found) return found;
  if (number === '' || number == null) return 'Any';
  if (vendor === 'Dell') return number + 'G';
  if (vendor in GENERATION_PREFIX) return GENERATION_PREFIX[vendor] + number;
  return vendor + ' generation ' + number;
}

// ---------------------------------------------------------------------------
// Safe links
// ---------------------------------------------------------------------------

function safeUrl(value, hosts, pathPattern) {
  if (typeof value !== 'string') return null;
  let url;
  try {
    url = new URL(value);
  } catch (error) {
    return null;
  }
  if (url.protocol !== 'https:' || url.username || url.password || url.port) return null;
  if (!hosts.includes(url.hostname)) return null;
  if (pathPattern && !pathPattern.test(url.pathname)) return null;
  return url.href;
}

function safeEbayUrl(value) {
  return safeUrl(value, ['www.ebay.com', 'ebay.com'], /^\/itm\/(?:[^/]+\/)?\d+\/?$/)
    || safeUrl(value, ['itm.ebaydesc.com'], /^\/itmdesc\/\d+\/?$/);
}

function safeBenchmarkUrl(value) {
  return safeUrl(value, ['www.cpubenchmark.net', 'cpubenchmark.net'], /^\/cpu\.php$/);
}

function externalLink(href, text, label) {
  const attrs = { href, target: '_blank', rel: 'noopener noreferrer' };
  if (label) attrs['aria-label'] = label;
  return el('a', { text, attrs });
}

// ---------------------------------------------------------------------------
// URL <-> state
// ---------------------------------------------------------------------------

function readStateFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const next = { ...DEFAULTS };
  for (const key of Object.keys(DEFAULTS)) {
    if (params.has(key)) next[key] = params.get(key);
  }
  for (const key of CHECK_KEYS) {
    next[key] = next[key] === 'false' ? 'false' : next[key] === 'true' ? 'true' : DEFAULTS[key];
  }
  if (!['price', 'ram', 'bays', 'benchmark'].includes(next.sort)) next.sort = DEFAULTS.sort;
  const bounds = { generation: [1, 100], height: [1, 10], min_ram: [0, 65536], min_bays: [0, 64], cpu_count: [1, 2], page: [1, 100000] };
  for (const [key, [low, high]] of Object.entries(bounds)) {
    const raw = next[key].trim();
    if (raw === '' && key !== 'page') continue;
    const value = Number(raw);
    next[key] = /^\d+$/.test(raw) && Number.isSafeInteger(value) && value >= low && value <= high ? String(value) : DEFAULTS[key];
  }
  next.q = next.q.slice(0, 200);
  if (!next.vendor) next.generation = '';
  if (next.max_total !== '' && !MAX_TOTAL_PATTERN.test(next.max_total)) next.max_total = '';
  return next;
}

function buildParams(includePage) {
  const params = new URLSearchParams();
  for (const key of Object.keys(DEFAULTS)) {
    if (key === 'page') continue;
    params.set(key, state[key]);
  }
  params.set('page', state.page);
  if (includePage) params.set('page_size', String(PAGE_SIZE));
  return params;
}

function pushUrl() {
  const params = buildParams(false);
  const next = window.location.pathname + '?' + params.toString();
  if (next !== window.location.pathname + window.location.search) {
    window.history.pushState(null, '', next);
  }
}

// ---------------------------------------------------------------------------
// Form <-> state
// ---------------------------------------------------------------------------

function ensureOption(select, value) {
  if (value === '') return;
  if (![...select.options].some(option => option.value === value)) {
    select.append(el('option', { text: value, attrs: { value } }));
  }
}

function rebuildDependents(vendor, model, generation) {
  const modelSelect = $('model');
  const mv = facetData.model_vendors || {};
  const models = (facetData.models || []).filter(name => !vendor || mv[name] === vendor);
  const modelOptions = [el('option', { text: vendor ? 'All ' + vendor + ' models' : 'All models', attrs: { value: '' } })];
  for (const name of models) {
    modelOptions.push(el('option', { text: modelName(mv[name], name), attrs: { value: name } }));
  }
  modelSelect.replaceChildren(...modelOptions);
  ensureOption(modelSelect, model);
  modelSelect.value = model;

  const genSelect = $('generation');
  const numbers = vendor ? ((facetData.generations_by_vendor || {})[vendor] || []) : [];
  const genOptions = [el('option', { text: 'Any', attrs: { value: '' } })];
  for (const number of numbers) {
    genOptions.push(el('option', { text: generationLabel(vendor, number), attrs: { value: String(number) } }));
  }
  genSelect.replaceChildren(...genOptions);
  if (vendor && generation !== '' && !numbers.map(String).includes(generation)) {
    genSelect.append(el('option', { text: generationLabel(vendor, generation), attrs: { value: generation } }));
  }
  genSelect.value = vendor ? generation : '';
  genSelect.disabled = !vendor;
}

function fillVendors(value) {
  const select = $('vendor');
  select.replaceChildren(el('option', { text: 'All vendors', attrs: { value: '' } }),
    ...(facetData.vendors || []).map(name => el('option', { text: name, attrs: { value: name } })));
  ensureOption(select, value);
  select.value = value;
}

function applyStateToForm() {
  fillVendors(state.vendor);
  rebuildDependents(state.vendor, state.model, state.generation);
  for (const key of SELECT_KEYS) {
    if (key === 'vendor' || key === 'model' || key === 'generation') continue;
    const select = $(key);
    ensureOption(select, state[key]);
    select.value = state[key];
  }
  for (const key of CHECK_KEYS) $(key).checked = state[key] === 'true';
  $('max_total').value = state.max_total;
  $('q').value = state.q;
  $('sort').value = state.sort;
  showMaxTotalError('');
}

function showMaxTotalError(message) {
  $('max_total_error').textContent = message;
  $('max_total_error').hidden = message === '';
  $('max_total').setAttribute('aria-invalid', message ? 'true' : 'false');
}

function readFormIntoState() {
  for (const key of SELECT_KEYS) state[key] = $(key).value;
  for (const key of CHECK_KEYS) state[key] = $(key).checked ? 'true' : 'false';
  state.q = $('q').value.trim().slice(0, 200);
  state.sort = $('sort').value;
  const budget = $('max_total').value.trim().replace(/^\$/, '').replace(/,/g, '');
  if (budget === '' || MAX_TOTAL_PATTERN.test(budget)) {
    state.max_total = budget;
    showMaxTotalError('');
    return true;
  }
  showMaxTotalError('Enter a dollar amount such as 500 or 499.99.');
  return false;
}

function onFilterChanged(event) {
  window.clearTimeout(debounceTimer);
  if (event.target.id === 'vendor') rebuildDependents($('vendor').value, '', '');
  const typing = event.target.matches('input[type="text"], input[type="search"]');
  debounceTimer = window.setTimeout(() => {
    const previousPage = state.page;
    if (!readFormIntoState()) return;
    state.page = '1';
    if (previousPage !== '1') window.scrollTo({ top: 0 });
    pushUrl();
    loadListings();
  }, typing ? SEARCH_DELAY_MS : 0);
}

function resetFilters() {
  window.clearTimeout(debounceTimer);
  state = { ...DEFAULTS };
  applyStateToForm();
  pushUrl();
  loadListings();
}

// ---------------------------------------------------------------------------
// Listings
// ---------------------------------------------------------------------------

function setLoading(loading) {
  $('loading').hidden = !loading;
  $('inventory').setAttribute('aria-busy', loading ? 'true' : 'false');
  $('results').classList.toggle('stale', loading);
}

function showError(message) {
  $('error-text').textContent = message;
  $('error').hidden = message === '';
}

async function fetchJson(url, signal) {
  const response = await fetch(url, { signal, headers: { Accept: 'application/json' } });
  if (!response.ok) {
    throw new Error('Server responded ' + response.status + (response.statusText ? ' ' + response.statusText : ''));
  }
  return response.json();
}

async function loadListings() {
  const seq = ++listSeq;
  if (listController) listController.abort();
  const controller = new AbortController();
  listController = controller;
  setLoading(true);
  showError('');
  try {
    const data = await fetchJson('/api/listings?' + buildParams(true).toString(), controller.signal);
    if (seq !== listSeq) return;
    renderListings(data);
  } catch (error) {
    if (seq !== listSeq || error.name === 'AbortError') return;
    showError('Could not load listings. ' + (error.message || 'Network error.') + ' Previously shown results may be out of date.');
  } finally {
    if (seq === listSeq) setLoading(false);
  }
}

function renderChips() {
  const chips = [];
  if (state.vendor) chips.push(state.vendor);
  chips.push(state.vendor && state.generation ? generationLabel(state.vendor, state.generation) : 'Any generation');
  chips.push(state.height ? state.height + 'U' : 'Any height');
  if (state.model) chips.push(modelName((facetData.model_vendors || {})[state.model], state.model));
  chips.push(state.modern_only === 'true' ? 'Dell 14G-era or newer' : 'All eras');
  if (state.min_ram) chips.push('≥ ' + state.min_ram + ' GB RAM');
  if (state.min_bays) chips.push('≥ ' + state.min_bays + ' bays');
  if (state.drive_size) chips.push(state.drive_size);
  if (state.cpu_count) chips.push(populatedLabel(Number(state.cpu_count)));
  if (state.max_total) chips.push('≤ ' + (formatMoney(state.max_total) || state.max_total) + ' known total');
  if (state.diskless === 'true') chips.push('No storage included');
  if (state.hide_damaged === 'true') chips.push('Damaged hidden');
  if (state.confirmed_only === 'true') chips.push('Confirmed only');
  if (state.include_leads === 'true' && state.confirmed_only !== 'true') chips.push('Leads included');
  if (state.q) chips.push('Search: ' + state.q);
  $('chips').replaceChildren(...chips.map(text => el('span', { className: 'chip', text })));
}

function updateHeaders() {
  $('th-price').textContent = state.sort === 'price' ? 'Total USD ↑' : 'Total USD';
  $('th-benchmark').textContent = state.sort === 'benchmark' ? 'CPU Mark / CPU ↓' : 'CPU Mark / CPU';
  $('th-ram').textContent = state.sort === 'ram' ? 'RAM ↓' : 'RAM';
  $('th-bays').textContent = state.sort === 'bays' ? 'Bays ↓' : 'Bays';
}

function plural(count, singular, pluralForm) {
  return count + ' ' + (count === 1 ? singular : pluralForm);
}

function renderListings(data) {
  const items = Array.isArray(data.items) ? data.items : [];
  rowStates.clear();
  for (const item of items) rowStates.set(String(item.id), item.match_state);
  const matches = Number(data.total_matches) || 0;
  const leads = Number(data.total_leads) || 0;
  lastPages = Math.max(1, Number(data.pages) || 1);

  $('count').textContent = plural(matches, 'matching server', 'matching servers');
  const notes = [];
  if (leads > 0) notes.push(plural(leads, 'unverified lead', 'unverified leads') + ' shown separately');
  if (items.some(item => item.match_state === 'match' && item.hardware_state !== 'confirmed')) {
    notes.push('some matches need hardware review');
  }
  $('summary-note').textContent = notes.join(' · ');

  renderChips();
  updateHeaders();

  const table = $('table');
  for (const body of table.querySelectorAll('tbody')) body.remove();
  const matchItems = items.filter(item => item.match_state !== 'lead');
  const leadItems = items.filter(item => item.match_state === 'lead');
  if (matchItems.length) table.append(buildGroup('Matches', matches, 'Meet every selected filter on known data.', matchItems, false));
  if (leadItems.length) {
    table.append(buildGroup('Unverified leads', leads, 'Not matches: some filtered fields are unknown or uncertain. Verify with the seller.', leadItems, true));
  }

  const empty = items.length === 0;
  $('table').closest('.table-wrap').hidden = empty;
  $('empty').hidden = !empty;
  if (empty) {
    $('empty-text').textContent = leads === 0 && matches === 0
      ? 'No listings match these filters. Try a higher budget, fewer required bays, or include unverified leads.'
      : 'This page has no listings. Return to the first page.';
  }

  const page = Number(data.page) || 1;
  $('pager').hidden = lastPages <= 1;
  $('page-label').textContent = 'Page ' + page + ' of ' + lastPages;
  $('prev').disabled = page <= 1;
  $('next').disabled = page >= lastPages;
}

function buildGroup(title, total, description, items, isLeads) {
  const body = el('tbody');
  const heading = el('th', { attrs: { colspan: '8', scope: 'colgroup' } }, [
    document.createTextNode(title + ' (' + formatInt(total) + ')'),
    el('span', { text: description }),
  ]);
  body.append(el('tr', { className: 'group-head' + (isLeads ? ' leads' : '') }, [heading]));
  for (const item of items) body.append(buildRow(item));
  return body;
}

function buildRow(item) {
  const total = formatMoney(item.quoted_total);
  const nameButton = el('button', {
    className: 'name',
    text: modelTitle(item),
    attrs: {
      type: 'button',
      'data-id': String(item.id),
      'aria-label': 'View ' + modelTitle(item) + ', ' + (item.ram_gb == null ? 'RAM unknown' : item.ram_gb + ' GB') + ', ' + (total || 'total unknown'),
    },
  });
  const seller = [item.seller || 'Unknown seller', item.rack_units == null ? 'height unknown' : item.rack_units + 'U', item.condition || 'condition unknown'].join(' · ');

  const cpuCell = el('td', null, [
    document.createTextNode(item.cpu_model ? (item.cpu_count == null ? '?' : item.cpu_count) + '× ' + item.cpu_model : 'CPU unknown'),
    small(item.total_cores == null ? 'cores unknown' : formatInt(item.total_cores) + ' cores total'),
  ]);

  const socketCell = el('td', null, [
    el('span', { className: 'socket-count', text: socketLabel(item) }),
    small(populatedLabel(item.cpu_count)),
  ]);

  const ramCell = el('td', { className: 'mono' }, [
    document.createTextNode(item.ram_gb == null ? 'Unknown' : item.ram_gb + ' GB'),
  ]);

  const baysText = item.drive_bays == null ? 'Unknown' : item.drive_bays + ' × ' + driveSizeLabel(item.drive_size, false);
  const baysCell = el('td', null, [
    el('span', { className: item.drive_bays != null && item.drive_bays >= 8 ? 'preferred' : '', text: baysText }),
    small(storageLabel(item.storage_state)),
  ]);

  const row = el('tr', null, [
    el('td', null, [nameButton, small(seller)]),
    cpuCell,
    socketCell,
    benchmarkCell(item),
    ramCell,
    baysCell,
    hardwareCell(item),
    priceCell(item),
  ]);
  return row;
}

function storageLabel(state) {
  if (state === 'diskless') return 'No storage';
  if (state === 'included') return 'Storage included';
  return 'Storage unknown';
}

function benchmarkCell(item) {
  const cell = el('td', { className: 'benchmark' });
  const bench = item.benchmark;
  if (!bench) {
    cell.append(el('span', { text: 'Not available' }), small('No matched benchmark'));
    return cell;
  }
  const href = safeBenchmarkUrl(bench.source_url);
  const text = formatInt(bench.cpu_mark);
  const label = 'PassMark CPU Mark ' + text + ' per CPU' + (item.cpu_model ? ' for ' + item.cpu_model : '') + '; view source';
  cell.append(href ? externalLink(href, text, label) : el('span', { text }));
  cell.append(small('ST ' + formatInt(bench.single_thread) + ' · per CPU'));
  return cell;
}

function hardwareBadge(item) {
  switch (item.hardware_state) {
    case 'confirmed': return { cls: 'good', text: 'Confirmed' };
    case 'damaged': return { cls: 'bad', text: 'Chassis damage' };
    case 'missing': return { cls: 'review', text: 'Parts missing' };
    default: return { cls: 'review', text: 'Needs review' };
  }
}

function hardwareCell(item) {
  const badge = hardwareBadge(item);
  const notes = [];
  if (item.warnings && item.warnings.length) notes.push(plural(item.warnings.length, 'warning', 'warnings'));
  if (item.exclusions && item.exclusions.length) notes.push(plural(item.exclusions.length, 'exclusion', 'exclusions'));
  return el('td', null, [
    el('span', { className: 'badge ' + badge.cls, text: badge.text }),
    small(notes.length ? notes.join(' · ') : 'See details'),
  ]);
}

function priceKindNote(item) {
  if (item.price_kind === 'base') return 'Base price, selected variant unverified';
  if (item.price_kind === 'range') return 'Price range, variant unverified';
  if (item.price_kind === 'unknown') return 'Price unknown';
  if (item.price_kind === 'exact' && item.page_verified_at && !item.page_verification_current) return 'Historical selected-variant quote; not reverified';
  return '';
}

function shippingText(item) {
  const shipping = formatMoney(item.shipping_price);
  if (shipping == null) return 'shipping unknown';
  return shipping === '$0.00' ? 'free shipping' : shipping + ' ship';
}

function priceCell(item) {
  const total = formatMoney(item.quoted_total);
  const itemPrice = formatMoney(item.item_price);
  const cell = el('td', { className: 'money' });
  cell.append(document.createTextNode(total || 'Total unknown'));
  const parts = [];
  if (itemPrice) parts.push(itemPrice + ' + ' + shippingText(item));
  const kind = priceKindNote(item);
  if (kind) parts.push(kind);
  cell.append(small(parts.join(' · ') || 'No price data'));
  return cell;
}

// ---------------------------------------------------------------------------
// Facets
// ---------------------------------------------------------------------------

function applyFacets(facets) {
  const next = { ...facetData };
  if (Array.isArray(facets.vendors) && facets.vendors.length) next.vendors = facets.vendors;
  if (Array.isArray(facets.models)) next.models = facets.models;
  if (facets.model_vendors && typeof facets.model_vendors === 'object') next.model_vendors = facets.model_vendors;
  if (facets.generations_by_vendor && typeof facets.generations_by_vendor === 'object') next.generations_by_vendor = facets.generations_by_vendor;
  next.generation_labels = facets.generation_labels && typeof facets.generation_labels === 'object' ? facets.generation_labels : {};
  facetData = next;
}

async function loadFacets() {
  $('facet-error').hidden = true;
  try {
    const facets = await fetchJson('/api/facets');
    applyFacets(facets);
    const vendor = $('vendor').value;
    fillVendors(vendor);
    rebuildDependents(vendor, $('model').value, $('generation').value);
  } catch (error) {
    $('facet-message').textContent = 'Filter choices could not refresh. Existing choices remain available.';
    $('facet-error').hidden = false;
  }
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

const STATE_LABELS = {
  never: 'never run',
  ok: 'succeeded',
  success: 'succeeded',
  partial: 'partially succeeded',
  blocked: 'blocked by source',
  failed: 'failed',
};

async function loadStatus() {
  const seq = ++statusSeq;
  try {
    const status = await fetchJson('/api/status');
    if (seq !== statusSeq) return;
    renderStatus(status);
  } catch (error) {
    if (seq !== statusSeq) return;
    renderStatusFailure(error);
  }
}

function runLine(name, run) {
  if (!run || typeof run !== 'object') return el('li', { text: name + ': status not reported' });
  const label = STATE_LABELS[run.state] || 'unknown';
  const parts = [name + ': ' + label];
  const attempt = formatDate(run.last_attempt_at);
  if (attempt) parts.push('last attempt ' + attempt);
  const line = el('li', { className: run.state === 'failed' || run.state === 'blocked' ? 'err' : '', text: parts.join(' · ') });
  if (run.message) line.append(document.createTextNode(' — ' + run.message));
  return line;
}

function renderStatus(status) {
  const lastSuccess = formatDate(status.last_success_at);
  const sourceUpdated = formatDate(status.source_updated_at);
  const body = $('status-body');
  const children = [];

  const headline = [];
  headline.push(lastSuccess ? 'Last successful collection: ' + lastSuccess : 'No successful collection recorded yet');
  if (sourceUpdated) headline.push('Upstream index updated: ' + sourceUpdated);
  if (typeof status.inventory_count === 'number') headline.push(plural(status.inventory_count, 'listing stored', 'listings stored'));
  children.push(el('p', { text: headline.join(' · ') }));
  if (status.stale) {
    children.push(el('p', { className: 'err', text: 'Data is stale: the upstream index or last successful run is older than expected. Listings may no longer be available.' }));
  }
  children.push(el('ul', null, [runLine('Discovery', status.discovery), runLine('Enrichment', status.enrichment)]));
  const vendors = Array.isArray(status.sources) ? status.sources.filter(v => v && typeof v === 'object' && v.vendor) : [];
  if (vendors.length) {
    children.push(el('p', { className: 'subtle', text: 'Per-vendor source pages' }));
    children.push(el('ul', null, vendors.map(v => {
      const parts = [v.vendor + ': ' + (STATE_LABELS[v.state] || 'unknown')];
      const ok = formatDate(v.last_success_at);
      parts.push(ok ? 'last success ' + ok : 'no successful collection');
      const upd = formatDate(v.source_updated_at);
      if (upd) parts.push('index updated ' + upd);
      if (typeof v.inventory_count === 'number') parts.push(plural(v.inventory_count, 'listing', 'listings'));
      if (v.stale) parts.push('stale');
      const line = el('li', { className: v.state === 'failed' || v.state === 'blocked' || v.stale ? 'err' : '', text: parts.join(' · ') });
      if (v.message) line.append(document.createTextNode(' — ' + v.message));
      return line;
    })));
  }
  const provider = [status.provider, status.coverage].filter(Boolean).join(' · ');
  if (provider) children.push(el('p', { text: provider }));
  body.replaceChildren(...children);

  if (status.coverage) $('coverage-note').textContent = status.coverage + '. Shipping quotes come from the index and their destination is unconfirmed.';

  const problems = [];
  if (status.stale) problems.push('listing data is stale');
  for (const [name, run] of [['discovery', status.discovery], ['enrichment', status.enrichment]]) {
    if (run && ['failed', 'blocked', 'partial'].includes(run.state)) problems.push(name + ' ' + STATE_LABELS[run.state]);
  }
  const badVendors = vendors.filter(v => ['failed', 'blocked', 'partial', 'never'].includes(v.state) || v.stale).map(v => v.vendor + ' ' + (v.stale && v.state === 'success' ? 'stale' : STATE_LABELS[v.state] || 'unknown'));
  if (badVendors.length) problems.push('vendor data: ' + badVendors.join(', '));
  setStatusWarning(problems.length ? 'Collection warning: ' + problems.join('; ') + '. Listings shown may be incomplete or outdated.' : '');
  const bad = problems.length > 0;
  const dot = $('status-dot');
  dot.className = 'statusdot' + (bad ? ' warn' : lastSuccess ? ' ok' : '');
}

function renderStatusFailure(error) {
  const message = 'Collection status unavailable (' + (error.message || 'network error') + '). Listings shown may be out of date.';
  setStatusWarning(message);
  $('status-dot').className = 'statusdot bad';
  const previous = $('status-body');
  if (previous.textContent.startsWith('Loading')) {
    previous.replaceChildren(el('p', { className: 'err', text: 'Status could not be loaded.' }));
  }
}

function setStatusWarning(message) {
  const banner = $('status-warning');
  banner.textContent = message;
  banner.hidden = message === '';
}

// ---------------------------------------------------------------------------
// Detail dialog
// ---------------------------------------------------------------------------

function openDetail(id, button) {
  opener = button;
  openerId = String(id);
  const rowState = rowStates.get(String(id));
  const seq = ++detailSeq;
  const dialog = $('detail');
  $('detail-title').textContent = 'Listing details';
  $('detail-body').replaceChildren(el('p', { className: 'subtle', text: 'Loading listing details…' }));
  if (!dialog.open) dialog.showModal();
  $('close').focus();
  fetchJson('/api/listings/' + encodeURIComponent(id))
    .then(item => {
      // Detail has no filter context; keep the classification of the row the user opened.
      if (seq === detailSeq) renderDetail(rowState ? { ...item, match_state: rowState } : item);
    })
    .catch(error => {
      if (seq !== detailSeq) return;
      $('detail-body').replaceChildren(el('p', { className: 'err', text: 'Could not load listing details: ' + (error.message || 'network error') }));
    });
}

function restoreFocus() {
  if (opener && opener.isConnected && opener.getClientRects().length) {
    opener.focus();
    return;
  }
  const replacement = [...document.querySelectorAll('button[data-id]')].find(button => button.dataset.id === openerId);
  if (replacement && replacement.getClientRects().length) replacement.focus();
  else (!$('empty').hidden ? $('empty-reset') : $('q')).focus();
}

function dlRow(list, term, value) {
  list.append(el('dt', { text: term }), el('dd', { text: value }));
}

function renderDetail(item) {
  $('detail-title').textContent = modelTitle(item);
  const badge = hardwareBadge(item);
  const total = formatMoney(item.quoted_total);
  const itemPrice = formatMoney(item.item_price);
  const shipping = formatMoney(item.shipping_price);
  const nodes = [];

  const states = el('div', null, [
    el('span', { className: 'badge ' + (item.match_state === 'match' ? 'good' : 'review'), text: item.match_state === 'match' ? 'Match' : 'Unverified lead — not a confirmed match' }),
    document.createTextNode(' '),
    el('span', { className: 'badge ' + badge.cls, text: badge.text }),
  ]);
  nodes.push(states);
  nodes.push(el('div', { className: 'detail-price', text: total || 'Total unknown' }));
  const priceNotes = [];
  priceNotes.push(itemPrice ? itemPrice + ' item + ' + (shipping ? shipping + ' quoted shipping' : 'shipping unknown') : 'Item price unknown');
  priceNotes.push('tax excluded');
  const kind = priceKindNote(item);
  if (kind) priceNotes.push(kind);
  nodes.push(el('p', { className: 'subtle', text: priceNotes.join(' · ') }));
  if (item.shipping_context) nodes.push(el('p', { className: 'subtle', text: item.shipping_context + ' Destination of this quote is unconfirmed.' }));

  if (item.title) nodes.push(el('p', { className: 'subtle', text: 'Listing title: ' + item.title }));
  appendCallout(nodes, 'Warnings', item.warnings);
  appendCallout(nodes, 'Excluded or missing', item.exclusions);

  const list = el('dl');
  dlRow(list, 'Processors', item.cpu_model ? (item.cpu_count == null ? '?' : item.cpu_count) + '× ' + item.cpu_model + ' · ' + (item.total_cores == null ? 'cores unknown' : item.total_cores + ' installed cores') : 'Unknown');
  dlRow(list, 'Socket population', socketLabel(item) + ' — ' + populatedLabel(item.cpu_count));
  dlRow(list, 'Memory', item.ram_gb == null ? 'Unknown' : item.ram_gb + ' GB');
  dlRow(list, 'Drive bays', item.drive_bays == null ? 'Unknown' : item.drive_bays + ' × ' + driveSizeLabel(item.drive_size, true));
  dlRow(list, 'Storage included', item.storage_state === 'diskless' ? 'None' : item.storage_state === 'included' ? 'Yes' : 'Unknown');
  dlRow(list, 'Rack height', item.rack_units == null ? 'Unknown' : item.rack_units + 'U');
  dlRow(list, 'Vendor', item.vendor || 'Unknown');
  dlRow(list, 'Generation', item.generation_label || (item.vendor && item.generation != null ? generationLabel(item.vendor, item.generation) : 'Unknown'));
  dlRow(list, 'Platform era', item.target_era === true ? 'Dell 14G-era or newer comparable' : item.target_era === false ? 'Older than the target era' : 'Unknown');
  dlRow(list, 'Condition', item.condition || 'Unknown');
  dlRow(list, 'Seller', item.seller || 'Unknown');
  dlRow(list, 'Detail check', detailStatusLabel(item.detail_status));
  dlRow(list, 'Source updated', formatDate(item.source_updated_at) || 'Unknown');
  dlRow(list, 'Last seen', formatDate(item.last_seen_at) || 'Unknown');
  dlRow(list, 'Last detail attempt', formatDate(item.last_checked_at) || 'Never');
  dlRow(list, 'Page verification', item.page_verification_current ? 'Current' : item.page_verified_at ? 'Not current — historical evidence only' : 'Never verified');
  dlRow(list, 'Page last verified', formatDate(item.page_verified_at) || 'Never');
  nodes.push(list);

  nodes.push(benchmarkSection(item));
  nodes.push(evidenceSection(item));

  const href = safeEbayUrl(item.source_url);
  if (href) {
    const link = externalLink(href, 'Open source listing ↗');
    link.className = 'source-link';
    nodes.push(link);
  }
  $('detail-body').replaceChildren(...nodes);
}

function detailStatusLabel(status) {
  const labels = { never: 'Not yet checked', verified: 'Seller details verified', partial: 'Partly checked', blocked: 'Blocked by source', failed: 'Check failed' };
  return labels[status] || 'Unknown';
}

function appendCallout(nodes, heading, messages) {
  if (!Array.isArray(messages) || messages.length === 0) return;
  const list = el('ul', null, messages.map(message => el('li', { text: message })));
  nodes.push(el('div', { className: 'callout' }, [el('strong', { text: heading }), list]));
}

function benchmarkSection(item) {
  const section = el('section', { className: 'benchmark-detail', attrs: { 'aria-label': 'CPU benchmark reference' } });
  section.append(el('h3', { text: 'CPU performance reference' }));
  const bench = item.benchmark;
  if (!bench) {
    section.append(el('p', { text: 'Not available. No matched CPU benchmark; no estimate substituted.' }));
    return section;
  }
  const href = safeBenchmarkUrl(bench.source_url);
  const mark = el('strong', { text: formatInt(bench.cpu_mark) });
  const markLine = el('p');
  if (href) {
    const link = externalLink(href, 'PassMark CPU Mark: ');
    link.append(mark);
    markLine.append(link);
  } else {
    markLine.append(document.createTextNode('PassMark CPU Mark: '), mark);
  }
  markLine.append(document.createTextNode(' per CPU'), el('br'), document.createTextNode('Single-thread rating: '), el('strong', { text: formatInt(bench.single_thread) }));
  section.append(markLine);
  const retrieved = formatDate(bench.retrieved_at) || 'date unknown';
  section.append(small('PerformanceTest ' + bench.version + ' · ' + formatInt(bench.samples) + ' samples · retrieved ' + retrieved));
  const scope = bench.scope === 'single_cpu' ? 'one CPU' : String(bench.scope);
  let text = 'Reference average for ' + scope + (item.cpu_model ? ' (' + item.cpu_model + ')' : '') + ', not a benchmark of this listing. ';
  if (item.cpu_count != null && item.cpu_count > 1) text += 'Multiple populated sockets do not imply a multiple of this score. ';
  text += 'Whole-server score: not measured.';
  section.append(el('p', { text }));
  return section;
}

function evidenceSection(item) {
  const box = el('div', { className: 'evidence' }, [el('strong', { text: 'Configuration evidence' })]);
  const evidence = Array.isArray(item.evidence) ? item.evidence : [];
  if (evidence.length === 0) {
    box.append(el('p', { text: 'No evidence recorded. Treat unspecified fields as unknown.' }));
    return box;
  }
  const list = el('ul');
  for (const entry of evidence) {
    const state = ['confirmed', 'inferred', 'unknown', 'conflicting'].includes(entry.state) ? entry.state : 'unknown';
    const li = el('li', { className: 'ev-' + state }, [
      el('span', { className: 'ev-state', text: entry.field + ' · ' + state + ':' }),
      document.createTextNode(entry.text),
    ]);
    const href = safeEbayUrl(entry.source_url);
    if (href) li.append(document.createTextNode(' '), externalLink(href, '(source)'));
    list.append(li);
  }
  box.append(list);
  return box;
}

// ---------------------------------------------------------------------------
// Destination preference (browser-only; does not alter any quote)
// ---------------------------------------------------------------------------

function loadDestination() {
  try {
    const saved = JSON.parse(window.localStorage.getItem(DEST_KEY) || 'null');
    if (saved && typeof saved === 'object') {
      $('dest-country').value = String(saved.country || '').slice(0, 60);
      $('dest-postcode').value = String(saved.postcode || '').slice(0, 12);
      $('dest-saved').textContent = $('dest-country').value || $('dest-postcode').value
        ? 'Saved in this browser. Listed shipping remains the index quote for an unconfirmed destination.'
        : '';
    }
  } catch (error) {
    // Storage unavailable or corrupt; preference is optional.
  }
}

function saveDestination(event) {
  event.preventDefault();
  const value = { country: $('dest-country').value.trim(), postcode: $('dest-postcode').value.trim() };
  try {
    window.localStorage.setItem(DEST_KEY, JSON.stringify(value));
    $('dest-saved').textContent = 'Saved in this browser only. This does not fetch a new quote; listed shipping remains the index quote for an unconfirmed destination.';
  } catch (error) {
    $('dest-saved').textContent = 'Could not save: browser storage is unavailable.';
  }
}

function clearDestination() {
  $('dest-country').value = '';
  $('dest-postcode').value = '';
  try {
    window.localStorage.removeItem(DEST_KEY);
  } catch (error) {
    // Nothing to clear.
  }
  $('dest-saved').textContent = 'Preference cleared.';
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function goToPage(delta) {
  const next = Math.min(lastPages, Math.max(1, Number(state.page) + delta));
  if (String(next) === state.page) return;
  state.page = String(next);
  pushUrl();
  loadListings();
  $('inventory').scrollIntoView({ block: 'start' });
}

function init() {
  applyStateToForm();
  loadDestination();

  const form = $('filters');
  form.addEventListener('submit', event => event.preventDefault());
  form.addEventListener('input', onFilterChanged);
  form.addEventListener('change', onFilterChanged);
  $('q').addEventListener('input', onFilterChanged);
  $('sort').addEventListener('change', onFilterChanged);
  $('reset').addEventListener('click', resetFilters);
  $('empty-reset').addEventListener('click', resetFilters);
  $('retry').addEventListener('click', loadListings);
  $('facet-retry').addEventListener('click', loadFacets);
  $('prev').addEventListener('click', () => goToPage(-1));
  $('next').addEventListener('click', () => goToPage(1));
  $('status-refresh').addEventListener('click', loadStatus);
  $('destination-form').addEventListener('submit', saveDestination);
  $('dest-clear').addEventListener('click', clearDestination);

  $('table').addEventListener('click', event => {
    const button = event.target.closest('button[data-id]');
    if (button) openDetail(button.dataset.id, button);
  });
  const dialog = $('detail');
  $('close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener('close', () => {
    detailSeq += 1;
    restoreFocus();
  });

  window.addEventListener('popstate', () => {
    window.clearTimeout(debounceTimer);
    state = readStateFromUrl();
    applyStateToForm();
    loadListings();
  });

  loadFacets();
  loadStatus();
  loadListings();
}

init();
