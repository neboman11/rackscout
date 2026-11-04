'use strict';

// ---------------------------------------------------------------------------
// Constants and state
// ---------------------------------------------------------------------------

const DEFAULTS = {
  vendor: '',
  model: [],
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
const SELECT_KEYS = ['vendor', 'generation', 'height', 'min_ram', 'min_bays', 'drive_size', 'cpu_count'];
const MODEL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9 +\-]{0,59}$/;
const MAX_MODELS = 100;
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
let view = null;
let editor = null;
let editorLoading = 0;
let dialogGen = 0;
let fetchTimer = 0;

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
    if (key !== 'model' && params.has(key)) next[key] = params.get(key);
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
  next.model = normalizeModels(params.getAll('model'));
  if (!next.vendor) next.generation = '';
  if (next.max_total !== '' && !MAX_TOTAL_PATTERN.test(next.max_total)) next.max_total = '';
  return next;
}

function normalizeModels(values) {
  const seen = new Set();
  for (const value of values) {
    const name = String(value).trim();
    if (name === '' || !MODEL_PATTERN.test(name)) continue;
    seen.add(name);
    if (seen.size >= MAX_MODELS) break;
  }
  return [...seen];
}

function buildParams(includePage) {
  const params = new URLSearchParams();
  for (const key of Object.keys(DEFAULTS)) {
    if (key === 'page') continue;
    if (key === 'model') {
      for (const name of state.model) params.append('model', name);
      continue;
    }
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

function selectedModels() {
  return [...$('model-options').querySelectorAll('input[type="checkbox"]:checked')].map(box => box.value);
}

function updateModelSummary() {
  const picked = selectedModels();
  const mv = facetData.model_vendors || {};
  $('model-count').textContent = picked.length === 0 ? ($('vendor').value ? 'All ' + $('vendor').value + ' models' : 'All models') : plural(picked.length, 'model', 'models') + ' selected';
  $('model-names').textContent = picked.map(name => modelName(mv[name], name)).join(', ');
  $('model-clear').disabled = picked.length === 0;
}

function rebuildDependents(vendor, models, generation) {
  const mv = facetData.model_vendors || {};
  const available = (facetData.models || []).filter(name => !vendor || mv[name] === vendor);
  const extras = models.filter(name => !available.includes(name));
  const options = [...extras, ...available].map((name, index) => {
    const id = 'model-opt-' + index;
    const known = !extras.includes(name);
    const box = el('input', { attrs: { type: 'checkbox', id, value: name } });
    box.checked = models.includes(name);
    return el('label', { className: 'multi-option', attrs: { for: id } }, [
      box,
      el('span', { text: modelName(mv[name], name) + (known ? '' : ' (not in current list)') }),
    ]);
  });
  $('model-options').replaceChildren(el('legend', { className: 'sr-only', text: 'Models' }), ...options);
  updateModelSummary();

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
    if (key === 'vendor' || key === 'generation') continue;
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
  state.model = selectedModels();
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
  if (event.target.id === 'vendor') rebuildDependents($('vendor').value, [], '');
  if (event.target.closest && event.target.closest('#model-options')) updateModelSummary();
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
  state = { ...DEFAULTS, model: [] };
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

// Resolves true when rendered, false on error, null when superseded by a newer load.
async function loadListings() {
  const seq = ++listSeq;
  if (listController) listController.abort();
  const controller = new AbortController();
  listController = controller;
  setLoading(true);
  showError('');
  try {
    const data = await fetchJson('/api/listings?' + buildParams(true).toString(), controller.signal);
    if (seq !== listSeq) return null;
    renderListings(data);
    return true;
  } catch (error) {
    if (seq !== listSeq || error.name === 'AbortError') return null;
    showError('Could not load listings. ' + (error.message || 'Network error.') + ' Previously shown results may be out of date.');
    return false;
  } finally {
    if (seq === listSeq) setLoading(false);
  }
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
  const total = priceText(item, item.quoted_total);
  const nameButton = el('button', {
    className: 'name',
    text: modelTitle(item),
    attrs: {
      type: 'button',
      'data-id': String(item.id),
      'aria-label': 'View ' + (item.origin === 'manual' ? 'manual configuration ' : '') + modelTitle(item) + ', RAM ' + ramDisplay(item) + ', ' + storageLabel(item) + ', ' + priceAria(item),
    },
  });
  const seller = [item.seller || 'Unknown seller', item.rack_units == null ? 'height unknown' : item.rack_units + 'U', item.condition || 'condition unknown'].join(' · ');

  const cpuCell = el('td', null, [
    document.createTextNode(item.cpu_model ? (item.cpu_count == null ? '?' : item.cpu_count) + '× ' + item.cpu_model : 'CPU unknown'),
    small((item.total_cores == null ? 'cores unknown' : formatInt(item.total_cores) + ' cores total') + claimTag(item)),
  ]);

  const socketCell = el('td', null, [
    el('span', { className: 'socket-count', text: socketLabel(item) }),
    small(populatedLabel(item.cpu_count)),
  ]);

  const ramNote = ramPairingNote(item);
  const ramCell = el('td', { className: 'mono' }, [
    document.createTextNode(ramDisplay(item)),
    ramNote ? small(ramNote) : null,
  ]);

  const baysText = item.drive_bays == null ? 'Unknown' : item.drive_bays + ' × ' + driveSizeLabel(item.drive_size, false);
  const baysCell = el('td', null, [
    el('span', { className: item.drive_bays != null && item.drive_bays >= 8 ? 'preferred' : '', text: baysText }),
    small(storageLabel(item) + claimTag(item)),
  ]);

  const row = el('tr', null, [
    el('td', null, [nameButton, manualBadge(item), variantLine(item), small(seller), manualUpdatedNote(item)]),
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

function ramLabel(item) {
  if (item.ram_state === 'conflicting') return 'Conflicting claims';
  if (item.ram_gb != null && item.ram_gb > 0) return item.ram_gb + ' GB';
  if (item.ram_gb === 0 && (item.ram_state === 'confirmed' || item.ram_state === 'inferred' || manualFields(item).includes('ram_gb'))) return 'Not included';
  return 'Not stated';
}

// Offer semantics (item.offer): see API contract. Missing offer is treated as unverified.
function offerOf(item) {
  const offer = item.offer || {};
  return {
    kind: offer.kind || (item.price_kind === 'exact' && !item.page_verification_current ? 'historical_configuration' : 'unverified'),
    starting: offer.price_is_starting === true || (!item.offer && ['base', 'range'].includes(item.price_kind) && formatMoney(item.item_price) != null),
    paired: offer.specification_price_paired === true,
    evidence: Array.isArray(offer.evidence) ? offer.evidence : [],
  };
}

function priceText(item, value) {
  const money = formatMoney(value);
  if (money == null) return null;
  return offerOf(item).starting ? money + '+' : money;
}

function offerHeadline(item) {
  switch (offerOf(item).kind) {
    case 'historical_configuration': return 'Historical quote — not current';
    case 'multiple_options': return 'Multiple options available';
    case 'unverified': return 'Configuration options unverified';
    case 'manual_configuration': return 'Price entered by you — not independently verified';
    default: return '';
  }
}

function offerExplanation(item) {
  const offer = offerOf(item);
  const price = offer.starting ? 'The price shown is a starting price' : 'The price shown';
  switch (offer.kind) {
    case 'configuration': return 'Price checked against the current listing page for this option. Hardware confidence remains field-specific; an exact price does not confirm every component is included.';
    case 'manual_configuration': return 'You entered this price for this exact configuration. It has not been checked against the current listing page and is never browser-verified. Hardware values you did not change keep their original source confidence.';
    case 'historical_configuration': return 'This exact option was captured earlier and the page has not been reverified. Treat the price and selected option as historical, not a current quote.';
    case 'multiple_options': return 'The listing shows evidence of more than one hardware option. ' + price + '; which option it applies to is not verified, and this view does not list every option.';
    default: return 'The selected option is not verified. ' + price + ' and may not match the specifications listed below. This does not mean the listing offers multiple options.';
  }
}

// Aria text for a price: states starting / historical meaning, not only the number.
function priceAria(item) {
  const offer = offerOf(item);
  const total = formatMoney(item.quoted_total);
  const itemPrice = formatMoney(item.item_price);
  if (offer.kind === 'manual_configuration') return (total || 'total unknown') + ', price entered by you, not independently verified';
  if (offer.kind === 'historical_configuration') return total ? 'historical quote ' + total + ', not current' : 'historical quote, total unknown';
  if (offer.starting) {
    if (total) return 'starting price ' + total + ' or more, configuration not verified';
    return 'total unknown' + (itemPrice ? ', item price starting at ' + itemPrice + ', ' + shippingText(item) : '');
  }
  return total || 'total unknown';
}

function manualFields(item) {
  return item && item.manual && Array.isArray(item.manual.fields) ? item.manual.fields : [];
}

function manualBadge(item) {
  if (item.origin !== 'manual') return null;
  return el('div', null, [el('span', { className: 'badge manual', text: 'Manual configuration — entered by you' })]);
}

function variantLine(item) {
  if (!item.variant_label) return null;
  const prefix = item.origin === 'manual' ? 'Manual option: ' : offerOf(item).kind === 'historical_configuration' ? 'Historical option: ' : 'Option: ';
  return el('small', { className: 'variant-label', text: prefix + item.variant_label });
}

function manualUpdatedNote(item) {
  if (item.origin !== 'manual' || !item.manual) return null;
  const when = formatDate(item.manual.updated_at || item.manual.created_at);
  const parts = ['Edited ' + (when || 'date unknown'), manualFields(item).length ? plural(manualFields(item).length, 'field', 'fields') + ' changed' : 'no fields changed'];
  if (item.manual.source_changed) parts.push('source changed since');
  return small(parts.join(' · '));
}

function ramDisplay(item) {
  const label = ramLabel(item);
  if (!offerOf(item).paired && item.ram_gb != null && item.ram_gb > 0 && item.ram_state !== 'conflicting') {
    return (manualFields(item).includes('ram_gb') ? 'Entered: ' : 'Listed: ') + label;
  }
  return label;
}

function ramPairingNote(item) {
  const offer = offerOf(item);
  if (offer.paired || ramLabel(item) === 'Not stated') return '';
  if (manualFields(item).includes('ram_gb')) return 'Entered by you; no confirmed price for it';
  return offer.starting ? 'Not tied to starting price' : 'Listing claim, not price-verified';
}

function claimTag(item) {
  return offerOf(item).paired ? '' : ' · listing claim';
}

function storageStatus(item) {
  if (item.storage_conf === 'conflicting') return 'Conflicting claims';
  if (item.storage_state === 'diskless' && (item.storage_conf === 'confirmed' || item.storage_conf === 'inferred')) return 'Not included';
  if (item.storage_state === 'included') return 'Included';
  return 'Not stated';
}

function storageLabel(item) {
  const status = storageStatus(item);
  return status === 'Conflicting claims' ? 'Storage: conflicting claims' : 'Storage ' + status.toLowerCase();
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
  const offer = offerOf(item);
  if (offer.kind === 'historical_configuration') return 'Historical quote for the selected option; not reverified';
  if (item.price_kind === 'base') return 'Starting price; selected configuration unverified';
  if (item.price_kind === 'range') return 'Lowest price of a range; selected configuration unverified';
  if (item.price_kind === 'unknown') return 'Price unknown';
  return '';
}

function shippingText(item) {
  const shipping = formatMoney(item.shipping_price);
  if (shipping == null) return 'shipping unknown';
  return shipping === '$0.00' ? 'free shipping' : shipping + ' ship';
}

function itemPriceLine(item) {
  const itemPrice = priceText(item, item.item_price);
  if (!itemPrice) return '';
  return itemPrice + (offerOf(item).starting ? ' item · ' : ' + ') + shippingText(item);
}

function priceCell(item) {
  const total = priceText(item, item.quoted_total);
  const cell = el('td', { className: 'money' });
  cell.append(
    el('span', { className: 'sr-only', text: priceAria(item) }),
    el('span', { text: total || 'Total unknown', attrs: { 'aria-hidden': 'true' } }),
  );
  const line = itemPriceLine(item);
  cell.append(small(line || 'No price data'));
  const headline = offerHeadline(item);
  if (headline) cell.append(el('small', { className: 'offer-flag offer-' + offerOf(item).kind, text: headline }));
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
    rebuildDependents(vendor, selectedModels(), $('generation').value);
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
// Manual data refresh
// ---------------------------------------------------------------------------

const REFRESH_POLL_MS = 5000;
const REFRESH_COOLDOWN_POLL_MS = 60000;
const REFRESH_ERROR_POLL_MS = 15000;
const REFRESH_LABELS = {
  succeeded: 'Last manual refresh succeeded',
  partial: 'Last manual refresh completed partially',
  blocked: 'Last manual refresh was blocked by a source',
  failed: 'Last manual refresh failed',
};
let refreshSeq = 0;
let refreshTimer = 0;
let refreshInfo = null;
let refreshPosting = false;
let refreshNote = '';
let refreshNoteIsError = false;
let refreshWasActive = false;
let refreshResyncPending = false;
let refreshBaselineSet = false;
let refreshLastCompletion = '';

function refreshActive(info) {
  return !!info && (info.state === 'queued' || info.state === 'running' || info.collector_running === true);
}

function refreshCooldown(info) {
  return !!info && info.enabled && Number(info.retry_after_seconds) > 0;
}

function refreshWaitText(seconds) {
  const secs = Math.max(1, Math.ceil(Number(seconds) || 0));
  if (secs < 90) return 'about ' + secs + ' seconds';
  if (secs < 5400) return 'about ' + Math.ceil(secs / 60) + ' minutes';
  return 'about ' + Math.ceil(secs / 3600) + ' hours';
}

const SELECTED_LABELS = {
  succeeded: 'Last selected-listing fetch finished',
  partial: 'Last selected-listing fetch completed partially (cached, backed-off or incomplete data)',
  blocked: 'Last selected-listing fetch was blocked by a source',
  failed: 'Last selected-listing fetch failed',
};

function refreshMessageText(info) {
  if (!info) return refreshNote || 'Checking refresh availability…';
  const when = formatDate(info.finished_at);
  const server = info.message ? ' ' + info.message : '';
  const selected = info.scope === 'listing';
  const target = info.target_item_id ? 'listing ' + info.target_item_id : 'the selected listing';
  if (!info.enabled) return 'Manual refresh is disabled on this server. Data is still collected automatically once a day at 04:00 UTC.' + server;
  if (info.state === 'queued') {
    if (selected) return 'Fetch queued for ' + target + ' only, not the whole inventory. This page will update when it finishes.' + server;
    return 'Refresh queued. Waiting for the worker to start. This page will update when it finishes.' + server;
  }
  if (info.state === 'running') {
    if (selected) return 'Fetching ' + target + ' only; the rest of the inventory is not being re-collected. It may reuse cached data, honor a source back-off or finish partially. Filters, listings and manual edits stay usable.' + server;
    return 'Collecting data now. This can take several minutes; filters and listings stay usable.' + server;
  }
  if (info.collector_running) return 'A collection is already running (scheduled or manual). Refresh is unavailable until it finishes.' + server;
  const parts = [];
  const labels = selected ? SELECTED_LABELS : REFRESH_LABELS;
  if (labels[info.state]) parts.push(labels[info.state] + (selected ? ' for ' + target : '') + (when ? ' at ' + when : '') + '.' + server);
  if (refreshCooldown(info)) {
    const retry = formatDate(info.retry_at);
    parts.push('Next manual refresh ' + (retry ? 'available at ' + retry + ' (' : 'available in ') + refreshWaitText(info.retry_after_seconds) + (retry ? ')' : '') + '.');
  } else if (!parts.length) {
    parts.push('Ready. Manual refresh requests are rate limited and never bypass source back-offs.' + server);
  }
  return parts.join(' ');
}

function renderRefresh() {
  const info = refreshInfo;
  const button = $('refresh-data');
  const canClick = !!info && info.enabled === true && info.can_refresh === true && !refreshActive(info) && !refreshCooldown(info) && !refreshPosting;
  button.setAttribute('aria-disabled', canClick ? 'false' : 'true');
  let label = 'Refresh data';
  if (refreshPosting) label = 'Requesting…';
  else if (info && info.state === 'queued') label = 'Refresh queued';
  else if (refreshActive(info)) label = 'Collecting…';
  button.textContent = label;
  const message = $('refresh-message');
  const next = refreshNote || refreshMessageText(info);
  if (message.textContent !== next) message.textContent = next;
  message.classList.toggle('err', refreshNoteIsError || (!!info && (info.state === 'failed' || info.state === 'blocked') && !refreshActive(info)));
  renderFetchControl();
}

function setRefreshNote(text, isError) {
  refreshNote = text;
  refreshNoteIsError = !!isError;
}

function scheduleRefreshPoll(delay) {
  window.clearTimeout(refreshTimer);
  refreshTimer = 0;
  if (delay > 0) refreshTimer = window.setTimeout(() => loadRefresh(), delay);
}

function onRefreshInfo(info) {
  const wasActive = refreshWasActive;
  const terminal = ['succeeded', 'partial', 'blocked', 'failed'].includes(info.state) && !!info.request_id && !!info.finished_at;
  const completion = terminal ? info.request_id + '|' + info.finished_at : '';
  const newCompletion = refreshBaselineSet && completion !== '' && completion !== refreshLastCompletion;
  if (completion !== '') refreshLastCompletion = completion;
  refreshBaselineSet = true;
  refreshInfo = info;
  const active = refreshActive(info);
  refreshWasActive = active;
  renderRefresh();
  if (active) {
    scheduleRefreshPoll(REFRESH_POLL_MS);
  } else if (refreshCooldown(info)) {
    const wait = Number(info.retry_after_seconds) * 1000 + 1000;
    scheduleRefreshPoll(Math.min(REFRESH_COOLDOWN_POLL_MS, Math.max(1000, wait)));
  } else {
    scheduleRefreshPoll(0);
  }
  // A collection we watched has ended: reload data using the current URL state.
  // Also fires for a completion (new request_id/finished_at) first seen after the active phase was missed.
  if ((wasActive && !active) || (newCompletion && !active)) reloadAfterCollection();
}

function validRefreshInfo(data) {
  return !!data && typeof data === 'object' && typeof data.enabled === 'boolean' && typeof data.state === 'string';
}

async function readJson(response) {
  try {
    const data = await response.json();
    return data && typeof data === 'object' ? data : null;
  } catch (error) {
    return null;
  }
}

async function readRefreshBody(response) {
  const data = await readJson(response);
  return validRefreshInfo(data) ? data : null;
}

function reloadAfterCollection() {
  // Filters, search, sort and page live in `state`/URL and are untouched; the detail dialog stays open and an editor draft is never replaced.
  loadListings();
  loadFacets();
  loadStatus();
  refreshOpenDetail();
}

async function loadRefresh(keepNote) {
  if (refreshPosting) {
    // Never let a state read race a pending POST; resync once the POST settles.
    refreshResyncPending = true;
    return;
  }
  const seq = ++refreshSeq;
  window.clearTimeout(refreshTimer);
  refreshTimer = 0;
  try {
    const response = await fetch('/api/refresh', { headers: { Accept: 'application/json' }, credentials: 'same-origin' });
    const data = await readRefreshBody(response);
    if (seq !== refreshSeq) return;
    if (!data) throw new Error('Server responded ' + response.status);
    if (keepNote !== true) setRefreshNote('', false);
    onRefreshInfo(data);
  } catch (error) {
    if (seq !== refreshSeq) return;
    setRefreshNote('Could not read refresh state (' + (error.message || 'network error') + '). Use “Check status” to try again.', true);
    renderRefresh();
    // Keep following a collection we already know is active; otherwise wait for the user.
    scheduleRefreshPoll(refreshWasActive ? REFRESH_ERROR_POLL_MS : 0);
  }
}

async function requestRefresh() {
  await postRefresh('/api/refresh', false);
}

// Global refresh (selected=false) or selected-listing fetch (selected=true). Both use the same
// shared queue/cooldown state; only GETs poll, and a POST is never resent automatically.
async function postRefresh(url, selected) {
  if (refreshPosting) return;
  if (!selected && $('refresh-data').getAttribute('aria-disabled') === 'true') return;
  const target = selected ? { id: view ? view.id : null, gen: dialogGen } : null;
  // A selected-fetch result is only applied to the listing/dialog that issued it; the shared refresh status always updates.
  const sameTarget = () => !selected || (!!view && view.id === target.id && dialogGen === target.gen && $('detail').open);
  const report = selected ? (text, isError) => { if (sameTarget()) setDetailNotice(text, isError); } : setRefreshNote;
  refreshPosting = true;
  report('', false);
  renderRefresh();
  ++refreshSeq; // invalidate any GET already in flight; GETs are deferred while posting
  window.clearTimeout(refreshTimer);
  refreshTimer = 0;
  let response;
  try {
    response = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-RackScout-Refresh': '1' },
      body: '{}',
    });
  } catch (error) {
    refreshPosting = false;
    refreshResyncPending = false;
    report((selected ? 'Fetch' : 'Refresh') + ' request could not be sent (' + (error.message || 'network error') + '). It may or may not have been received; the current state is being re-read.', true);
    renderRefresh();
    loadRefresh(true);
    return;
  }
  const body = await readJson(response);
  const data = validRefreshInfo(body) ? body : null;
  refreshPosting = false;
  const resync = refreshResyncPending;
  refreshResyncPending = false;
  if (data) {
    if (!selected) setRefreshNote('', false);
    if (response.status === 429 && !(Number(data.retry_after_seconds) > 0)) {
      const header = Number(response.headers.get('Retry-After'));
      if (header > 0) data.retry_after_seconds = header;
    }
    onRefreshInfo(data);
    if (selected) report(selectedOutcome(response.status, data), response.status !== 202);
    if (resync) loadRefresh(true);
    return;
  }
  const retryAfter = Number(response.headers.get('Retry-After'));
  if (selected) {
    let text;
    if (response.status === 404) text = 'This listing is no longer active, so there is nothing to fetch.';
    else if (response.status === 429) {
      text = 'Not queued: this listing was checked recently or its source is backing off' + (retryAfter > 0 ? '; try again in ' + refreshWaitText(retryAfter) : '') + '.';
      if (retryAfter > 0 && sameTarget() && view.item) view.item.next_check_after = new Date(Date.now() + retryAfter * 1000).toISOString();
    } else if (response.status === 403) text = 'Fetching is not permitted from this page or is disabled. Manual edits are unaffected.';
    else if (response.status === 503) text = 'Fetch not started: the server could not process the request right now. You can try again.';
    else text = 'Fetch not started: server responded ' + response.status + '. You can try again.';
    report(text, true);
  } else {
    const detail = response.status === 403 ? 'Manual refresh is not permitted from this page or is disabled.'
      : response.status === 503 ? 'The server could not process the request right now.'
      : 'Server responded ' + response.status + '.';
    report('Refresh not started. ' + detail + ' You can try again.', true);
  }
  renderRefresh();
  loadRefresh(true);
}

function selectedOutcome(status, info) {
  if (status === 202) {
    return 'Fetch requested for this listing only. Your manual edits and drafts are untouched. Progress appears in the Data refresh panel; the result may come from cached data, honor a source back-off or be partial.';
  }
  if (status === 409) return 'Not queued: another collection is already queued or running.' + (info.message ? ' ' + info.message : '');
  if (status === 429) return 'Not queued: fetching is cooling down' + (Number(info.retry_after_seconds) > 0 ? '; try again in ' + refreshWaitText(info.retry_after_seconds) : '') + '.';
  return info.message || 'Fetch was not queued (server responded ' + status + ').';
}

// ---------------------------------------------------------------------------
// Detail dialog
// ---------------------------------------------------------------------------

function setDetailNotice(text, isError) {
  const node = $('detail-notice');
  node.textContent = text;
  node.hidden = !text;
  node.classList.toggle('err', !!isError);
}

function openDetail(id, button) {
  dialogGen += 1;
  opener = button;
  openerId = String(id);
  const dialog = $('detail');
  editor = null;
  editorLoading = 0;
  setDetailNotice('', false);
  if (!dialog.open) dialog.showModal();
  $('close').focus();
  loadDetail(id, { rowState: rowStates.get(String(id)) });
}

// Loads the listing and its manual-editor data. A stale response (another load/editor started
// meanwhile) is discarded so it can never overwrite a draft or a newer view. `silent` re-renders
// in place without a loading placeholder and is only used when no editor is open.
async function loadDetail(id, options) {
  const opts = options || {};
  const seq = ++detailSeq;
  const sid = String(id);
  if (!opts.silent) {
    editor = null;
    editorLoading = 0;
    $('close').disabled = false;
    if (!opts.notice) setDetailNotice('', false);
    $('detail-title').textContent = 'Listing details';
    $('detail-body').replaceChildren(el('p', { className: 'subtle', text: 'Loading listing details…' }));
  }
  const base = '/api/listings/' + encodeURIComponent(id);
  const [itemResult, manualResult] = await Promise.allSettled([fetchJson(base), fetchJson(base + '/manual')]);
  if (seq !== detailSeq) return;
  if (itemResult.status === 'rejected') {
    if (!opts.silent) {
      $('detail-body').replaceChildren(el('p', { className: 'err', text: 'Could not load listing details: ' + (itemResult.reason && itemResult.reason.message || 'network error') }));
    }
    return;
  }
  openerId = sid;
  // Detail has no filter context; keep the classification of the row the user opened.
  const rowState = rowStates.get(sid) || opts.rowState;
  const item = rowState ? { ...itemResult.value, match_state: rowState } : itemResult.value;
  view = {
    id: sid,
    item,
    manual: manualResult.status === 'fulfilled' && manualResult.value && typeof manualResult.value === 'object' ? manualResult.value : null,
    confirming: false,
  };
  renderDetail(item);
  if (opts.notice) setDetailNotice(opts.notice, false);
  if (opts.focus) $('detail-title').focus();
}

function refreshOpenDetail() {
  if (!$('detail').open || editor || editorLoading || !view || view.confirming) return;
  loadDetail(view.id, { silent: true });
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
  const total = priceText(item, item.quoted_total);
  const itemPrice = priceText(item, item.item_price);
  const shipping = formatMoney(item.shipping_price);
  const nodes = [];

  const states = el('div', null, [
    el('span', { className: 'badge ' + (item.match_state === 'match' ? 'good' : 'review'), text: item.match_state === 'match' ? 'Match' : 'Unverified lead — not a confirmed match' }),
    document.createTextNode(' '),
    el('span', { className: 'badge ' + badge.cls, text: badge.text }),
    item.origin === 'manual' ? document.createTextNode(' ') : null,
    item.origin === 'manual' ? el('span', { className: 'badge manual', text: 'Manual configuration — entered by you' }) : null,
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
  nodes.push(...detailActions());
  nodes.push(offerSection(item));
  if (item.origin === 'manual') nodes.push(manualSection(item));

  if (item.title) nodes.push(el('p', { className: 'subtle', text: 'Listing title: ' + item.title }));
  appendCallout(nodes, 'Warnings', item.warnings);
  appendCallout(nodes, 'Excluded or missing', item.exclusions);

  const list = el('dl');
  dlRow(list, 'Processors', item.cpu_model ? (item.cpu_count == null ? '?' : item.cpu_count) + '× ' + item.cpu_model + ' · ' + (item.total_cores == null ? 'cores unknown' : item.total_cores + ' installed cores') : 'Unknown');
  dlRow(list, 'Socket population', socketLabel(item) + ' — ' + populatedLabel(item.cpu_count));
  if (item.variant_label) dlRow(list, item.origin === 'manual' ? 'Manual option' : offerOf(item).kind === 'historical_configuration' ? 'Historical option' : 'Option', item.variant_label);
  dlRow(list, 'Memory', ramDisplay(item) + (ramPairingNote(item) ? ' — ' + ramPairingNote(item) : ''));
  dlRow(list, 'Drive bays', item.drive_bays == null ? 'Unknown' : item.drive_bays + ' × ' + driveSizeLabel(item.drive_size, true));
  dlRow(list, 'Storage', storageStatus(item));
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
  if (item.origin === 'manual') {
    dlRow(list, 'Price verification', offerOf(item).kind === 'manual_configuration' ? 'Entered by you; not independently verified or browser-verified' : 'No price confirmed by you');
  } else {
    dlRow(list, 'Page verification', item.page_verification_current ? 'Current' : item.page_verified_at ? 'Not current — historical evidence only' : 'Never verified');
    dlRow(list, 'Page last verified', formatDate(item.page_verified_at) || 'Never');
  }
  nodes.push(list);
  nodes.push(el('p', { className: 'benchmark-note', text: 'Not stated means the listing does not say; it does not mean not included. Only Not included reflects confirmed or inferred absence.' }));

  const siblings = siblingsSection();
  if (siblings) nodes.push(siblings);
  nodes.push(benchmarkSection(item));
  nodes.push(evidenceSection(item));
  if (item.origin === 'manual' && view && view.manual && view.manual.original) nodes.push(originalSection(view.manual.original));

  const href = safeEbayUrl(item.source_url);
  if (href) {
    const link = externalLink(href, 'Open source listing ↗');
    link.className = 'source-link';
    nodes.push(link);
  }
  $('detail-body').replaceChildren(...nodes);
  renderFetchControl();
}

function detailStatusLabel(status) {
  const labels = { never: 'Not yet checked', verified: 'Seller details verified', partial: 'Partly checked', blocked: 'Blocked by source', failed: 'Check failed' };
  return labels[status] || 'Unknown';
}

function offerSection(item) {
  const offer = offerOf(item);
  const headline = offerHeadline(item) || 'Current exact price quote';
  const children = [el('strong', { className: 'offer-flag offer-' + offer.kind, text: headline }), el('p', { text: offerExplanation(item) })];
  if (!offer.paired) {
    children.push(el('p', { text: 'Processors, memory and storage below are listing claims, shown as stated. They are not verified as included at this price, and other options may exist.' }));
  }
  if (offer.evidence.length) {
    children.push(el('p', { className: 'subtle', text: 'Evidence of options:' }));
    children.push(el('ul', null, offer.evidence.map(text => el('li', { text: String(text) }))));
  }
  return el('div', { className: 'callout offer-callout' }, children);
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
// Manual configurations: detail actions, siblings, selected fetch control
// ---------------------------------------------------------------------------

function actionButton(text, onClick, id, extraClass) {
  const button = el('button', { className: 'action' + (extraClass ? ' ' + extraClass : ''), text, attrs: { type: 'button', id } });
  button.addEventListener('click', onClick);
  return button;
}

function detailActions() {
  const manual = view && view.manual;
  const item = view.item;
  const bar = el('div', { className: 'detail-actions', attrs: { role: 'group', 'aria-label': 'Listing actions' } });
  if (manual && manual.enabled === true) {
    bar.append(actionButton('Edit details', () => openEditor(view.id, 'edit'), 'edit-details'));
    bar.append(actionButton('Add configuration', () => openEditor(view.id, 'add'), 'add-config'));
  }
  const fetchButton = actionButton('Fetch seller details', onFetchClick, 'fetch-seller');
  fetchButton.setAttribute('aria-disabled', 'true');
  fetchButton.setAttribute('aria-describedby', 'fetch-reason');
  bar.append(fetchButton);
  if (manual && manual.enabled === true && item.origin === 'manual') {
    bar.append(actionButton('Remove manual configuration', showRemoveConfirm, 'remove-manual', 'danger'));
  }
  const nodes = [bar, el('div', { attrs: { id: 'remove-slot' } })];
  const note = el('p', { className: 'subtle action-note' });
  if (!manual) {
    note.append(document.createTextNode('Manual editing availability could not be loaded. '));
    note.append(actionButton('Retry', () => loadDetail(view.id, { focus: true }), 'manual-retry', 'inline'));
  } else if (manual.enabled !== true) {
    note.textContent = 'Manual editing is disabled on this server.';
  } else if (item.origin === 'manual') {
    note.textContent = 'Edit details changes this manual configuration only. Add configuration creates another independent option from the same source listing.';
  } else {
    note.textContent = 'Edit details saves a manual copy of this row and hides the source row while a copy exists. The original source data and evidence are kept. Add configuration creates a separate manual option without copying any price.';
  }
  nodes.push(note);
  nodes.push(el('p', { className: 'subtle action-note', attrs: { id: 'fetch-reason' } }));
  return nodes;
}

function fetchState() {
  const item = view && view.item;
  const info = refreshInfo;
  if (!item) return { ok: false, reason: 'Listing not loaded.' };
  if (view.manual && view.manual.fetch_enabled === false) return { ok: false, reason: 'Fetching seller details is disabled on this server. Manual editing still works.' };
  if (!info) return { ok: false, reason: 'Checking whether fetching is available…' };
  if (!info.enabled) return { ok: false, reason: 'Fetching is disabled on this server. Manual editing still works.' };
  if (refreshPosting) return { ok: false, reason: 'A request is being sent…' };
  if (refreshActive(info)) return { ok: false, reason: 'A collection is already queued or running; wait for it to finish.' };
  const next = item.next_check_after ? Date.parse(item.next_check_after) : NaN;
  if (!Number.isNaN(next) && next > Date.now()) {
    return { ok: false, reason: 'This listing was checked recently or its source is backing off. Next check allowed after ' + (formatDate(item.next_check_after) || 'a later time') + '.' };
  }
  if (refreshCooldown(info)) return { ok: false, reason: 'Fetching is rate limited; available again in ' + refreshWaitText(info.retry_after_seconds) + '.' };
  if (info.can_refresh !== true) return { ok: false, reason: 'Fetching is not available right now.' };
  return { ok: true, reason: 'Fetches seller details for this listing only (not the whole inventory). It uses cached data and honors source back-offs, so it may be partial. It never changes your manual values.' };
}

function scheduleFetchExpiry() {
  const next = view && view.item && view.item.next_check_after ? Date.parse(view.item.next_check_after) : NaN;
  if (Number.isNaN(next) || next <= Date.now()) return;
  const id = view.id;
  const gen = dialogGen;
  // Read-only re-render when the per-listing cooldown ends; it never sends a request.
  fetchTimer = window.setTimeout(() => {
    fetchTimer = 0;
    if (view && view.id === id && dialogGen === gen) renderFetchControl();
  }, Math.min(next - Date.now() + 250, 2147483000));
}

function renderFetchControl() {
  const button = $('fetch-seller');
  const reason = $('fetch-reason');
  window.clearTimeout(fetchTimer);
  fetchTimer = 0;
  if (!button || !reason || !view) return;
  scheduleFetchExpiry();
  const status = fetchState();
  button.setAttribute('aria-disabled', status.ok ? 'false' : 'true');
  button.textContent = refreshPosting ? 'Requesting…' : 'Fetch seller details';
  if (reason.textContent !== status.reason) reason.textContent = status.reason;
}

function onFetchClick() {
  const button = $('fetch-seller');
  if (!button || button.getAttribute('aria-disabled') === 'true' || !view) return;
  postRefresh('/api/listings/' + encodeURIComponent(view.id) + '/refresh', true);
}

function showRemoveConfirm() {
  const slot = $('remove-slot');
  if (!slot || !view || !view.manual) return;
  view.confirming = true;
  const panel = buildRemovePanel(view.id, () => view.manual && view.manual.revision, () => {
    if (view) view.confirming = false;
    slot.replaceChildren();
    const trigger = $('remove-manual');
    if (trigger) trigger.focus();
  });
  slot.replaceChildren(panel.node);
  panel.cancel.focus();
}

// Explicit two-step removal. Deletes only the manual copy; source data is untouched.
function buildRemovePanel(id, getRevision, onCancel) {
  const seq = detailSeq;
  let busy = false;
  const status = el('p', { className: 'err', attrs: { role: 'alert' } });
  const confirmButton = el('button', { className: 'action danger', text: 'Yes, remove this manual configuration', attrs: { type: 'button' } });
  const cancel = el('button', { className: 'action', text: 'Keep it', attrs: { type: 'button' } });
  const node = el('div', { className: 'callout remove-panel', attrs: { role: 'group', 'aria-label': 'Confirm removal' } }, [
    el('strong', { text: 'Remove this manual configuration?' }),
    el('p', { text: 'Its edited values, saved price and notes are deleted. Source data is not changed; the source listing reappears in the inventory once no manual configuration remains for it. This cannot be undone.' }),
    status,
    el('div', { className: 'editor-actions' }, [confirmButton, cancel]),
  ]);
  cancel.addEventListener('click', () => {
    if (!busy) onCancel();
  });
  confirmButton.addEventListener('click', async () => {
    if (busy) return;
    const revision = Number(getRevision());
    if (!Number.isSafeInteger(revision) || revision < 1) {
      status.textContent = 'The current revision is unknown. Reload this listing and try again.';
      return;
    }
    busy = true;
    confirmButton.setAttribute('aria-disabled', 'true');
    confirmButton.textContent = 'Removing…';
    status.textContent = '';
    let response;
    try {
      response = await manualRequest('DELETE', id, { revision });
    } catch (error) {
      busy = false;
      confirmButton.setAttribute('aria-disabled', 'false');
      confirmButton.textContent = 'Yes, remove this manual configuration';
      status.textContent = 'Result unknown (' + (error.message || 'network error') + '). It may have been removed. Reload the listing to check before trying again; nothing was resent.';
      return;
    }
    const data = await readJson(response);
    busy = false;
    if (response.ok && data && data.deleted_id != null) {
      loadListings();
      if (seq !== detailSeq) return;
      editor = null;
      const target = data.source_id != null ? data.source_id : data.deleted_id;
      loadDetail(target, { notice: 'Manual configuration removed. Source data was not changed.', focus: true });
      return;
    }
    confirmButton.setAttribute('aria-disabled', 'false');
    confirmButton.textContent = 'Yes, remove this manual configuration';
    if (response.status === 409) {
      status.textContent = 'Not removed: this configuration changed since you opened it (revision conflict). Reload the listing to see the latest version, then decide again.';
    } else if (response.status === 404) {
      status.textContent = 'This manual configuration no longer exists. Reload the listing.';
      loadListings();
    } else if (response.status === 403) {
      status.textContent = 'Manual editing is not permitted from this page or is disabled.';
    } else {
      const messages = detailMessages(data);
      status.textContent = 'Not removed: ' + (messages.length ? messages.join(' ') : 'server responded ' + response.status + '.') + (response.status >= 500 ? ' The result may be unknown; reload to check.' : '');
    }
  });
  return { node, cancel };
}

function shippingPhrase(value) {
  const money = formatMoney(value);
  if (money == null) return 'shipping unknown';
  return money === '$0.00' ? 'free shipping' : money + ' shipping';
}

function pairStatusText(config) {
  if (typeof config.price_pair_status === 'string') {
    return ['confirmed', 'paired', 'manual_configuration'].includes(config.price_pair_status) ? 'Price entered by you' : 'Price not confirmed';
  }
  return config.price_confirmed === true ? 'Price entered by you' : 'Price not confirmed';
}

function siblingsSection() {
  const manual = view && view.manual;
  const configs = manual && Array.isArray(manual.configurations) ? manual.configurations : [];
  if (!configs.length) return null;
  const canEdit = manual.enabled === true;
  const section = el('section', { className: 'siblings', attrs: { 'aria-label': 'Manual configurations of this listing' } });
  section.append(
    el('h3', { text: 'Manual configurations for this listing' }),
    el('p', { className: 'subtle', text: 'Each option is its own row with its own RAM and price. Options are never combined or expanded.' }),
  );
  const list = el('ul', { className: 'sibling-list' });
  for (const config of configs) {
    const name = config.label || 'Manual configuration';
    const current = String(config.id) === view.id;
    const ram = config.ram_gb == null ? 'RAM unknown' : config.ram_gb === 0 ? 'No RAM included' : config.ram_gb + ' GB RAM';
    const price = formatMoney(config.item_price);
    const line = [ram, price ? price + ' item, ' + shippingPhrase(config.shipping_price) : 'no item price entered', pairStatusText(config)].join(' · ');
    const buttons = el('div', { className: 'sibling-actions' });
    const viewButton = el('button', { className: 'action', text: current ? 'Viewing' : 'View', attrs: { type: 'button', 'aria-label': 'View ' + name } });
    if (current) viewButton.disabled = true;
    viewButton.addEventListener('click', () => loadDetail(config.id, { focus: true }));
    buttons.append(viewButton);
    if (canEdit) {
      const editButton = el('button', { className: 'action', text: 'Edit', attrs: { type: 'button', 'aria-label': 'Edit ' + name } });
      editButton.addEventListener('click', () => openEditor(config.id, 'edit'));
      const addButton = el('button', { className: 'action', text: 'Add similar', attrs: { type: 'button', 'aria-label': 'Add a configuration based on ' + name } });
      addButton.addEventListener('click', () => openEditor(config.id, 'add'));
      buttons.append(editButton, addButton);
    }
    list.append(el('li', null, [
      el('div', { className: 'sibling-text' }, [el('strong', { text: name + (current ? ' (this view)' : '') }), small(line)]),
      buttons,
    ]));
  }
  section.append(list);
  return section;
}

function manualSection(item) {
  const meta = item.manual || {};
  const box = el('div', { className: 'callout manual-callout' }, [el('strong', { text: 'Manual configuration — entered by you' })]);
  const facts = [];
  if (item.variant_label) facts.push('Label: ' + item.variant_label);
  const created = formatDate(meta.created_at);
  const updated = formatDate(meta.updated_at);
  if (created) facts.push('Created ' + created);
  if (updated) facts.push('Last edited ' + updated);
  const names = manualFields(item).map(key => (specOf(key) ? specOf(key).label : key));
  facts.push(names.length ? 'Fields you set: ' + names.join(', ') + '. Every other value keeps its original source confidence.' : 'You have not changed any field; all values are the original source values.');
  facts.push(meta.price_confirmed === true
    ? 'You confirmed the item price for this configuration. It is a price entered by you, not independently verified or browser-verified.'
    : 'You have not confirmed a price for this configuration; any price shown is an unpaired source starting quote or unknown.');
  if (meta.notes) facts.push('Notes: ' + meta.notes);
  box.append(el('ul', null, facts.map(text => el('li', { text }))));
  if (meta.source_changed) {
    box.append(el('p', { className: 'err', text: 'The source listing changed after this copy was made. Your saved values were not overwritten; review them against the source.' }));
  }
  return box;
}

// Read-only original/baseline source information. The server shape is not rigid, so scalars,
// string lists and evidence-like objects are all rendered as plain text.
function humanize(key) {
  const text = String(key).replace(/_/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function plainValue(key, value) {
  if (value == null) return 'Unknown';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'string' && /price/.test(key) && formatMoney(value)) return formatMoney(value);
  return String(value);
}

function originalRows(list, prefix, value, depth, counter) {
  for (const [key, entry] of Object.entries(value)) {
    if (counter.n >= 200) return;
    const label = humanize(prefix ? prefix + ' · ' + key : key);
    if (Array.isArray(entry)) {
      const lines = entry.slice(0, 50).map(part => {
        if (part == null || typeof part !== 'object') return plainValue(key, part);
        const head = [part.field, part.state].filter(Boolean).join(' · ');
        const text = part.text || part.message || '';
        if (head || text) return (head ? head + (text ? ': ' : '') : '') + text;
        return Object.entries(part).filter(([, v]) => v == null || typeof v !== 'object').map(([k, v]) => k + ': ' + plainValue(k, v)).join(', ');
      });
      counter.n += 1;
      list.append(el('dt', { text: label }), el('dd', { className: 'lines', text: lines.length ? lines.join('\n') : 'None' }));
    } else if (entry && typeof entry === 'object') {
      if (depth < 3) originalRows(list, prefix ? prefix + ' · ' + key : key, entry, depth + 1, counter);
    } else {
      counter.n += 1;
      list.append(el('dt', { text: label }), el('dd', { text: plainValue(key, entry) }));
    }
  }
}

function originalSection(original) {
  const details = el('details', { className: 'original-detail' }, [el('summary', { text: 'Original source specifications and evidence (read-only)' })]);
  if (!original || typeof original !== 'object') {
    details.append(el('p', { className: 'subtle', text: 'No original data available.' }));
    return details;
  }
  const list = el('dl');
  originalRows(list, '', original, 0, { n: 0 });
  details.append(list);
  return details;
}

// ---------------------------------------------------------------------------
// Manual configuration editor
// ---------------------------------------------------------------------------

const COMPONENT_OPTIONS = [['unknown', 'Unknown'], ['present', 'Included'], ['missing', 'Missing']];
const ESSENTIAL_KEYS = { cpu_included: 'cpu', psu: 'psu', cooling: 'cooling', nic: 'nic', drive_path: 'drive_path' };
const COMPONENT_HELP = 'Unknown = not stated. Missing excludes this configuration from confirmed hardware.';
const EDIT_SPECS = [
  { group: 'Server', key: 'model', label: 'Server model', kind: 'model', config: true, help: 'Choose a catalog model from this vendor, or leave unknown.' },
  { group: 'Processor', key: 'cpu_model', label: 'CPU model', kind: 'text', max: 100, config: true, help: 'Blank means unknown.' },
  { group: 'Processor', key: 'cpu_count', label: 'Populated CPUs', kind: 'int', min: 1, max: 8, config: true, help: 'Whole number 1 to 8. Blank means unknown.' },
  { group: 'Processor', key: 'total_cores', label: 'Total installed cores', kind: 'int', min: 1, max: 8192, config: true, help: 'All populated CPUs combined. If you change the CPU, enter the new total or leave blank (unknown).' },
  { group: 'Memory', key: 'ram_gb', label: 'Installed RAM (GB)', kind: 'int', min: 0, max: 1048576, config: true, help: 'Enter 0 if this configuration includes no RAM. Leave blank if unknown.' },
  { group: 'Chassis and storage', key: 'drive_bays', label: 'Drive bays', kind: 'int', min: 1, max: 128, config: true, help: 'Whole number 1 to 128. Blank means unknown.' },
  { group: 'Chassis and storage', key: 'drive_size', label: 'Drive form factor', kind: 'select', config: true, options: [['', 'Unknown'], ['SFF', '2.5″ SFF'], ['LFF', '3.5″ LFF']], help: 'Bay size.' },
  { group: 'Chassis and storage', key: 'storage_state', label: 'Storage', kind: 'select', config: true, options: [['unknown', 'Unknown'], ['diskless', 'No storage included'], ['included', 'Storage included']], help: 'Whether drives are included.' },
  { group: 'Chassis and storage', key: 'damage', label: 'Chassis damage', kind: 'select', options: [['', 'Unknown'], ['no', 'No damage'], ['yes', 'Damaged']], help: 'Damage you observed or the seller stated.' },
  { group: 'Components', key: 'cpu_included', label: 'CPUs installed', kind: 'select', component: true, config: true, options: COMPONENT_OPTIONS, help: COMPONENT_HELP },
  { group: 'Components', key: 'psu', label: 'Power supplies', kind: 'select', component: true, config: true, options: COMPONENT_OPTIONS, help: COMPONENT_HELP },
  { group: 'Components', key: 'cooling', label: 'Cooling (fans and heatsinks)', kind: 'select', component: true, config: true, options: COMPONENT_OPTIONS, help: COMPONENT_HELP },
  { group: 'Components', key: 'nic', label: 'Network card / LOM', kind: 'select', component: true, config: true, options: COMPONENT_OPTIONS, help: COMPONENT_HELP },
  { group: 'Components', key: 'drive_path', label: 'Drive backplane / caddies', kind: 'select', component: true, config: true, options: COMPONENT_OPTIONS, help: COMPONENT_HELP },
  { group: 'Price', key: 'item_price', label: 'Item price (USD)', kind: 'money', help: 'Up to two decimals, 0 to 99,999,999.99. Blank means unknown.' },
  { group: 'Price', key: 'shipping_price', label: 'Shipping (USD)', kind: 'money', help: 'Blank means shipping unknown. Enter 0 only if shipping is explicitly free.' },
];

function specOf(key) {
  return EDIT_SPECS.find(spec => spec.key === key);
}

function normalizeComponent(value) {
  if (value === 'present' || value === 'confirmed' || value === true) return 'present';
  if (value === 'missing') return 'missing';
  return 'unknown';
}

function pickValue(values, spec) {
  if (values[spec.key] !== undefined) return values[spec.key];
  const essential = ESSENTIAL_KEYS[spec.key];
  const states = values.essential_states;
  if (essential && states && typeof states === 'object' && states[essential] !== undefined) return states[essential];
  return null;
}

function toRaw(spec, value) {
  if (spec.key === 'damage') return value === true ? 'yes' : value === false ? 'no' : '';
  if (spec.component) return normalizeComponent(value);
  if (spec.key === 'storage_state') return value === 'diskless' || value === 'included' ? value : 'unknown';
  return value == null ? '' : String(value);
}

function fromRaw(spec, raw) {
  const text = String(raw == null ? '' : raw);
  if (spec.kind === 'int') {
    const trimmed = text.trim();
    if (trimmed === '') return { ok: true, value: null };
    const number = Number(trimmed);
    if (!/^\d+$/.test(trimmed) || !Number.isSafeInteger(number) || number < spec.min || number > spec.max) {
      return { ok: false, error: spec.label + ' must be a whole number from ' + formatInt(spec.min) + ' to ' + formatInt(spec.max) + ', or blank if unknown.' };
    }
    return { ok: true, value: number };
  }
  if (spec.kind === 'text') {
    const trimmed = text.trim();
    if (trimmed === '') return { ok: true, value: null };
    if (trimmed.length > spec.max || /[\u0000-\u001f\u007f]/.test(trimmed)) return { ok: false, error: spec.label + ' must be plain text of at most ' + spec.max + ' characters.' };
    return { ok: true, value: trimmed };
  }
  if (spec.kind === 'money') {
    const trimmed = text.trim();
    if (trimmed === '') return { ok: true, value: null };
    if (!/^\d{1,8}(\.\d{1,2})?$/.test(trimmed)) return { ok: false, error: spec.label + ' must be a number such as 450 or 450.00 (at most two decimals, up to 99,999,999.99), or blank if unknown.' };
    return { ok: true, value: trimmed };
  }
  if (spec.key === 'damage') return { ok: true, value: text === 'yes' ? true : text === 'no' ? false : null };
  if (spec.kind === 'model' || spec.key === 'drive_size') return { ok: true, value: text === '' ? null : text };
  return { ok: true, value: text };
}

function displayRaw(spec, raw) {
  if (spec.options) {
    const found = spec.options.find(option => option[0] === raw);
    return found ? found[1] : raw || 'Unknown';
  }
  if (raw === '') return 'Unknown';
  if (spec.kind === 'money') return formatMoney(raw) || raw;
  if (spec.kind === 'model') return modelName(editor && editor.item.vendor, raw) || raw;
  return raw;
}

function editorSig(ed) {
  return JSON.stringify([ed.label, ed.notes, Object.keys(ed.touched).sort().map(key => [key, ed.raw[key]]), ed.price_confirmed]);
}

function editorDirty() {
  return !!editor && editorSig(editor) !== editor.initialSig;
}

function origValue(key) {
  const original = editor && editor.data.original;
  if (!original || typeof original !== 'object') return undefined;
  for (const bag of [original.values, original.fields, original]) {
    if (bag && typeof bag === 'object' && !Array.isArray(bag) && Object.prototype.hasOwnProperty.call(bag, key)) return bag[key];
  }
  return undefined;
}

function manualRequest(method, id, body) {
  return fetch('/api/listings/' + encodeURIComponent(id) + '/manual', {
    method,
    credentials: 'same-origin',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-RackScout-Edit': '1' },
    body: JSON.stringify(body),
  });
}

function detailMessages(data) {
  const detail = data && data.detail !== undefined ? data.detail : data && data.error;
  if (typeof detail === 'string') return [detail];
  if (Array.isArray(detail)) {
    return detail.map(part => {
      if (typeof part === 'string') return part;
      if (part && (part.msg || part.message)) return (Array.isArray(part.loc) ? part.loc.join('.') + ': ' : '') + (part.msg || part.message);
      return null;
    }).filter(Boolean);
  }
  if (detail && typeof detail === 'object') return Object.entries(detail).map(([key, value]) => key + ': ' + String(value));
  return [];
}

async function openEditor(id, mode) {
  const seq = ++detailSeq;
  editorLoading = seq;
  editor = null;
  const sid = String(id);
  setDetailNotice('', false);
  $('detail-title').textContent = mode === 'add' ? 'Add configuration' : 'Edit details';
  $('detail-body').replaceChildren(el('p', { className: 'subtle', text: 'Loading editor…' }));
  const base = '/api/listings/' + encodeURIComponent(sid);
  let item;
  let data;
  try {
    [item, data] = await Promise.all([fetchJson(base), fetchJson(base + '/manual')]);
  } catch (error) {
    if (seq !== detailSeq) return;
    editorLoading = 0;
    const back = actionButton('Back to details', () => loadDetail(sid, { focus: true }), 'editor-back');
    $('detail-body').replaceChildren(el('p', { className: 'err', text: 'Could not load the editor: ' + (error.message || 'network error') }), back);
    return;
  }
  if (seq !== detailSeq) return;
  editorLoading = 0;
  if (!data || data.enabled !== true) {
    const back = actionButton('Back to details', () => loadDetail(sid, { focus: true }), 'editor-back');
    $('detail-body').replaceChildren(el('p', { className: 'err', text: 'Manual editing is disabled on this server.' }), back);
    return;
  }
  editor = buildEditorState(item, data, mode, sid);
  renderEditor();
  $('detail-title').focus();
}

function buildEditorState(item, data, mode, id) {
  const values = data.values && typeof data.values === 'object' ? data.values : {};
  const explicit = data.fields && typeof data.fields === 'object' && !Array.isArray(data.fields) ? data.fields : {};
  const base = {};
  const raw = {};
  const touched = {};
  const initialTouched = {};
  for (const spec of EDIT_SPECS) {
    // A new option never inherits another option's price or shipping.
    base[spec.key] = mode === 'add' && spec.kind === 'money' ? '' : toRaw(spec, pickValue(values, spec));
    raw[spec.key] = base[spec.key];
  }
  for (const spec of EDIT_SPECS) {
    if (!Object.prototype.hasOwnProperty.call(explicit, spec.key)) continue;
    if (mode === 'add' && spec.kind === 'money') continue;
    touched[spec.key] = true;
    initialTouched[spec.key] = true;
    raw[spec.key] = toRaw(spec, explicit[spec.key]);
  }
  const ed = {
    id,
    mode,
    isManual: item.origin === 'manual',
    item,
    data,
    base,
    raw,
    touched,
    initialTouched,
    pendingSource: {},
    label: mode === 'add' ? '' : String(data.label || item.variant_label || 'Manual configuration').slice(0, 120),
    notes: mode === 'add' ? '' : String(data.notes || ''),
    price_confirmed: mode === 'add' ? false : data.price_confirmed === true,
    submitting: false,
    controls: {},
    alertEl: null,
  };
  ed.initialSig = editorSig(ed);
  return ed;
}

function setErr(name, message) {
  const node = $('ed-' + name + '-err');
  const input = $('ed-' + name);
  if (node) {
    node.textContent = message || '';
    node.hidden = !message;
  }
  if (input) {
    if (message) input.setAttribute('aria-invalid', 'true');
    else input.removeAttribute('aria-invalid');
  }
}

function clearAllErrors() {
  for (const name of ['label', 'notes', 'price_confirmed', ...EDIT_SPECS.map(spec => spec.key)]) setErr(name, '');
}

function hideEditorAlert() {
  if (editor && editor.alertEl) {
    editor.alertEl.hidden = true;
    editor.alertEl.replaceChildren();
  }
}

function showEditorAlert(title, messages, buttons) {
  const box = editor && editor.alertEl;
  if (!box) return;
  const nodes = [el('strong', { text: title })];
  if (messages && messages.length) nodes.push(el('ul', null, messages.map(message => el('li', { text: message }))));
  if (buttons && buttons.length) nodes.push(el('div', { className: 'editor-actions' }, buttons));
  box.replaceChildren(...nodes);
  box.hidden = false;
  box.focus();
}

// Discard guard rendered inside the dialog (no window.confirm), used by Cancel, Close and Escape.
function guardDiscard(proceed) {
  if (editor && editor.submitting) return;
  if (!editor || !editorDirty()) {
    proceed();
    return;
  }
  const discard = actionButton('Discard changes', proceed, 'editor-discard', 'danger');
  const keep = actionButton('Keep editing', () => {
    hideEditorAlert();
    const first = $('ed-label');
    if (first) first.focus();
  }, 'editor-keep');
  showEditorAlert('Discard unsaved changes?', ['Your draft has not been saved.'], [discard, keep]);
}

function updateSaveButton() {
  const button = $('editor-save');
  if (!button || !editor) return;
  button.setAttribute('aria-disabled', editor.submitting ? 'true' : 'false');
  button.textContent = editor.submitting ? 'Saving…' : editor.mode === 'add' ? 'Save new configuration' : 'Save';
}

function updateSourceNote(spec) {
  const control = editor.controls[spec.key];
  if (!control) return;
  let text = '';
  if (editor.pendingSource[spec.key]) text = 'The source value will be applied when you save.';
  else {
    const original = origValue(spec.key);
    if (original !== undefined) text = 'Source value: ' + displayRaw(spec, toRaw(spec, original));
  }
  control.src.textContent = text;
  control.src.hidden = text === '';
}

function clearPriceConfirmation() {
  const ed = editor;
  if (!ed || !ed.price_confirmed) return;
  ed.price_confirmed = false;
  const box = $('ed-price_confirmed');
  if (box) box.checked = false;
  setDetailNotice('Price confirmation cleared because hardware fields changed. Re-check it if the price still applies to this exact configuration.', false);
}

// Freezes every control in the dialog body (and Close) while a write is in flight so no input can be lost.
function setEditorFrozen(frozen) {
  for (const control of $('detail-body').querySelectorAll('input,select,textarea,button')) {
    if (frozen) {
      control.dataset.wasDisabled = control.disabled ? '1' : '';
      control.disabled = true;
    } else {
      control.disabled = control.dataset.wasDisabled === '1';
      delete control.dataset.wasDisabled;
    }
  }
  $('close').disabled = frozen;
}

function onFieldEdited(spec) {
  const ed = editor;
  if (!ed || ed.submitting) return;
  const control = ed.controls[spec.key];
  ed.raw[spec.key] = control.input.value;
  // Editing or explicitly affirming a value is a manual assertion, even if it matches the source.
  // Reset to source is the deliberate way to remove that assertion.
  ed.touched[spec.key] = true;
  ed.pendingSource[spec.key] = false;
  control.reset.hidden = !ed.touched[spec.key];
  control.mark.disabled = true;
  setErr(spec.key, '');
  updateSourceNote(spec);
  if (spec.config) clearPriceConfirmation();
}

function resetField(spec) {
  const ed = editor;
  if (!ed || ed.submitting) return;
  const control = ed.controls[spec.key];
  delete ed.touched[spec.key];
  const original = origValue(spec.key);
  let raw;
  ed.pendingSource[spec.key] = false;
  if (original !== undefined) raw = toRaw(spec, original);
  else if (!ed.initialTouched[spec.key]) raw = ed.base[spec.key];
  else {
    raw = spec.kind === 'model' ? '' : spec.options ? (spec.options.find(option => option[0] === '') ? '' : spec.options[0][0]) : '';
    ed.pendingSource[spec.key] = true;
  }
  control.input.value = raw;
  ed.raw[spec.key] = raw;
  control.reset.hidden = true;
  control.mark.disabled = false;
  setErr(spec.key, '');
  updateSourceNote(spec);
  if (spec.config) clearPriceConfirmation();
  control.input.focus();
}

function makeInput(spec, id) {
  const ed = editor;
  if (spec.kind === 'model' || spec.kind === 'select') {
    const select = el('select', { attrs: { id } });
    let options;
    if (spec.kind === 'model') {
      const vendor = ed.item.vendor;
      const catalog = (facetData.models || []).filter(model => !vendor || (facetData.model_vendors || {})[model] === vendor);
      options = [['', 'Unknown / not selected'], ...catalog.map(model => [model, modelName(vendor, model) || model])];
      for (const value of [ed.raw[spec.key], ed.base[spec.key]]) {
        if (value && !options.some(option => option[0] === value)) options.push([value, modelName(vendor, value) || value]);
      }
    } else {
      options = spec.options;
    }
    for (const [value, text] of options) select.append(el('option', { text, attrs: { value } }));
    select.value = ed.raw[spec.key];
    return select;
  }
  const attrs = { id, type: 'text', autocomplete: 'off', spellcheck: 'false' };
  if (spec.kind === 'int') attrs.inputmode = 'numeric';
  if (spec.kind === 'money') attrs.inputmode = 'decimal';
  if (spec.kind === 'text') attrs.maxlength = String(spec.max);
  if (spec.kind === 'money') attrs.maxlength = '11';
  const input = el('input', { attrs });
  input.value = ed.raw[spec.key];
  return input;
}

function buildField(spec) {
  const ed = editor;
  const id = 'ed-' + spec.key;
  const input = makeInput(spec, id);
  const reset = el('button', { className: 'linkbutton', text: 'Reset to source', attrs: { type: 'button', 'aria-label': 'Reset ' + spec.label + ' to the source value' } });
  reset.hidden = !ed.touched[spec.key];
  const mark = el('button', { className: 'linkbutton', text: 'Use as manual', attrs: {
    id: id + '-assert', type: 'button', 'aria-label': 'Use displayed ' + spec.label + ' as a manual value',
  } });
  mark.disabled = !!ed.touched[spec.key];
  const help = el('small', { className: 'field-help', text: spec.help, attrs: { id: id + '-help' } });
  const src = el('small', { className: 'field-help source-note', attrs: { id: id + '-src' } });
  const err = el('div', { className: 'field-error', attrs: { id: id + '-err' } });
  err.hidden = true;
  input.setAttribute('aria-describedby', id + '-help ' + id + '-src ' + id + '-err');
  ed.controls[spec.key] = { input, reset, mark, err, src };
  input.addEventListener(spec.kind === 'model' || spec.kind === 'select' ? 'change' : 'input', () => onFieldEdited(spec));
  reset.addEventListener('click', () => resetField(spec));
  mark.addEventListener('click', () => onFieldEdited(spec));
  const node = el('div', { className: 'field editor-field' }, [
    el('div', { className: 'field-head' }, [el('label', { text: spec.label, attrs: { for: id } }), mark, reset]),
    input,
    help,
    src,
    err,
  ]);
  updateSourceNote(spec);
  return node;
}

function renderEditor() {
  const ed = editor;
  ed.controls = {};
  const verb = ed.mode === 'add' ? 'Add configuration' : 'Edit details';
  $('detail-title').textContent = verb + ' — ' + modelTitle(ed.item);
  const form = el('form', { className: 'manual-form', attrs: { id: 'manual-form', novalidate: '', autocomplete: 'off' } });
  ed.alertEl = el('div', { className: 'callout editor-alert', attrs: { id: 'editor-alert', role: 'alert', tabindex: '-1' } });
  ed.alertEl.hidden = true;
  form.append(ed.alertEl);

  let intro;
  if (ed.mode === 'add') {
    intro = 'Add configuration creates another independent manual row (its own RAM and price) from the same source listing. No price or shipping is copied; fields you leave alone keep their source values and confidence.';
  } else if (ed.isManual) {
    intro = 'You are editing a manual configuration. Only fields you change become manual values; everything else keeps its original source confidence.';
  } else {
    intro = 'Saving creates a separate manual configuration and hides this source row while it exists. The original source data and evidence are kept, and only fields you change become manual values.';
  }
  form.append(el('p', { className: 'subtle', text: intro }));

  const labelField = el('div', { className: 'field editor-field' }, [
    el('div', { className: 'field-head' }, [el('label', { text: 'Configuration label (required)', attrs: { for: 'ed-label' } })]),
    el('input', { attrs: { id: 'ed-label', type: 'text', maxlength: '120', required: '', autocomplete: 'off', placeholder: 'e.g. 128 GB RAM option', 'aria-describedby': 'ed-label-err' } }),
    el('div', { className: 'field-error', attrs: { id: 'ed-label-err' } }),
  ]);
  labelField.querySelector('input').value = ed.label;
  labelField.querySelector('input').addEventListener('input', event => {
    ed.label = event.target.value;
    setErr('label', '');
  });
  labelField.lastChild.hidden = true;
  form.append(labelField);

  const notesField = el('div', { className: 'field editor-field' }, [
    el('div', { className: 'field-head' }, [el('label', { text: 'Notes', attrs: { for: 'ed-notes' } })]),
    el('textarea', { attrs: { id: 'ed-notes', maxlength: '2000', rows: '3', 'aria-describedby': 'ed-notes-err' } }),
    el('div', { className: 'field-error', attrs: { id: 'ed-notes-err' } }),
  ]);
  notesField.querySelector('textarea').value = ed.notes;
  notesField.querySelector('textarea').addEventListener('input', event => {
    ed.notes = event.target.value;
    setErr('notes', '');
  });
  notesField.lastChild.hidden = true;
  form.append(notesField);

  const groups = [];
  for (const spec of EDIT_SPECS) {
    let group = groups.find(entry => entry.name === spec.group);
    if (!group) {
      group = { name: spec.group, specs: [] };
      groups.push(group);
    }
    group.specs.push(spec);
  }
  for (const group of groups) {
    const fieldset = el('fieldset', { className: 'editor-group' }, [el('legend', { text: group.name })]);
    for (const spec of group.specs) fieldset.append(buildField(spec));
    if (group.name === 'Price') {
      const box = el('input', { attrs: { type: 'checkbox', id: 'ed-price_confirmed', 'aria-describedby': 'ed-price_confirmed-help ed-price_confirmed-err' } });
      box.checked = ed.price_confirmed;
      box.addEventListener('change', () => {
        ed.price_confirmed = box.checked;
        setErr('price_confirmed', '');
      });
      fieldset.append(
        el('label', { className: 'check editor-confirm', attrs: { for: 'ed-price_confirmed' } }, [
          box,
          el('span', { text: 'This item price applies to exactly the configuration entered above' }, [
            el('small', { text: 'Saved as “Price entered by you”. It is not independently verified or browser-verified. Type the price yourself to confirm it; the box clears whenever RAM, CPU or other hardware fields change.', attrs: { id: 'ed-price_confirmed-help' } }),
          ]),
        ]),
        el('div', { className: 'field-error', attrs: { id: 'ed-price_confirmed-err' } }),
      );
      fieldset.lastChild.hidden = true;
    }
    form.append(fieldset);
  }

  if (ed.data.original) form.append(originalSection(ed.data.original));

  const save = el('button', { className: 'primary', text: 'Save', attrs: { type: 'submit', id: 'editor-save' } });
  const cancel = actionButton('Cancel', () => guardDiscard(() => loadDetail(ed.id, { focus: true })), 'editor-cancel');
  const actions = el('div', { className: 'editor-actions sticky' }, [save, cancel]);
  form.append(actions);
  form.addEventListener('submit', event => {
    event.preventDefault();
    saveEditor();
  });

  const nodes = [form, el('div', { attrs: { id: 'remove-slot' } })];
  if (ed.isManual && ed.mode === 'edit') {
    const trigger = actionButton('Remove this manual configuration', () => {
      const slot = $('remove-slot');
      const panel = buildRemovePanel(ed.id, () => ed.data.revision, () => {
        slot.replaceChildren();
        trigger.focus();
      });
      slot.replaceChildren(panel.node);
      panel.cancel.focus();
    }, 'remove-manual', 'danger');
    nodes.splice(1, 0, trigger);
  }
  $('detail-body').replaceChildren(...nodes);
  updateSaveButton();
}

function collectEditor() {
  const ed = editor;
  const errors = {};
  const fields = {};
  const label = ed.label.trim();
  if (!label) errors.label = 'Enter a label for this configuration (up to 120 characters).';
  else if (label.length > 120 || /[\u0000-\u001f\u007f]/.test(label)) errors.label = 'The label must be plain text of at most 120 characters.';
  if (ed.notes.length > 2000) errors.notes = 'Notes can be at most 2,000 characters.';
  for (const key of Object.keys(ed.touched)) {
    const spec = specOf(key);
    const result = fromRaw(spec, ed.raw[key]);
    if (result.ok) fields[key] = result.value;
    else errors[key] = result.error;
  }
  if (ed.price_confirmed && !errors.item_price) {
    if (!ed.touched.item_price) errors.price_confirmed = 'Type the item price yourself to confirm it. A prefilled source price cannot be confirmed automatically.';
    else if (fields.item_price == null) errors.price_confirmed = 'Enter an item price before confirming it.';
  }
  const revision = Number(ed.data.revision);
  if (!Number.isSafeInteger(revision) || revision < 1) errors.form = 'The editor data has no valid revision. Cancel and reopen the editor.';
  return { errors, body: { revision, label, notes: ed.notes, fields, price_confirmed: ed.price_confirmed } };
}

function applyServerMessages(messages) {
  const keys = ['label', 'notes', 'price_confirmed', ...EDIT_SPECS.map(spec => spec.key)];
  for (const message of messages) {
    const key = keys.find(name => new RegExp('(^|[^a-z_])' + name + '([^a-z_]|$)').test(message));
    if (key) setErr(key, message);
  }
}

async function saveEditor() {
  const ed = editor;
  if (!ed || ed.submitting) return;
  clearAllErrors();
  hideEditorAlert();
  const { errors, body } = collectEditor();
  const names = Object.keys(errors);
  if (names.length) {
    for (const name of names) setErr(name, errors[name]);
    showEditorAlert('Fix ' + plural(names.length, 'problem', 'problems') + ' before saving', names.map(name => errors[name]));
    return;
  }
  ed.submitting = true;
  setEditorFrozen(true);
  updateSaveButton();
  const patch = ed.mode === 'edit' && ed.isManual;
  let response;
  try {
    response = await manualRequest(patch ? 'PATCH' : 'POST', ed.id, body);
  } catch (error) {
    ed.submitting = false;
    if (editor === ed) setEditorFrozen(false);
    if (editor === ed) {
      updateSaveButton();
      showEditorAlert('Result unknown — nothing was resent', ['The request could not be completed (' + (error.message || 'network error') + '). It may or may not have been saved. Your draft is kept and is not resent automatically. Check the list of manual configurations before saving again to avoid a duplicate.']);
    }
    return;
  }
  const data = await readJson(response);
  if (response.ok && data && data.item && typeof data.item === 'object' && data.item.id != null) {
    const savedId = data.item.id;
    const listed = await loadListings();
    if (editor !== ed) return;
    editor = null;
    let notice = patch
      ? 'Saved. The changes apply to this manual configuration only.'
      : 'Saved as a new manual configuration. The source row is hidden while manual copies exist; the original source data is kept.';
    if (listed === true && !rowStates.has(String(savedId))) {
      notice += ' It is not shown in the current list because of your filters or page; your filters were not changed.';
    }
    loadDetail(savedId, { notice, focus: true });
    return;
  }
  ed.submitting = false;
  if (editor === ed) setEditorFrozen(false);
  if (editor !== ed) return;
  updateSaveButton();
  if (response.status === 409) {
    const reload = actionButton('Discard my draft and load the latest version', () => openEditor(ed.id, ed.mode), 'editor-reload');
    showEditorAlert('Not saved — this configuration changed', ['It was updated since you opened it (revision conflict), for example by another edit or a data collection. Your draft is kept here and nothing was retried. Copy what you need, then load the latest version.'], [reload]);
    return;
  }
  const messages = detailMessages(data);
  if (response.status === 422) {
    applyServerMessages(messages);
    showEditorAlert('Not saved — the server rejected some values', messages.length ? messages : ['Check the values and try again.']);
    return;
  }
  if (response.status === 403) {
    showEditorAlert('Not saved — editing is not permitted', ['Manual editing is disabled or not allowed from this page. Your draft is kept.']);
    return;
  }
  if (response.status === 404) {
    showEditorAlert('Not saved — configuration not found', ['This configuration no longer exists. Your draft is kept; close the editor and reload the list.']);
    return;
  }
  const unknown = response.status >= 500;
  showEditorAlert(unknown ? 'Result unknown — nothing was resent' : 'Not saved', [
    (messages.length ? messages.join(' ') : 'The server responded ' + response.status + '.') + ' Your draft is kept and is not resent automatically.' + (unknown ? ' It may have been saved; check the list of manual configurations before saving again.' : ''),
  ]);
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
  $('model-clear').addEventListener('click', event => {
    for (const box of $('model-options').querySelectorAll('input:checked')) box.checked = false;
    updateModelSummary();
    $('model-picker').querySelector('summary').focus();
    onFilterChanged(event);
  });
  document.addEventListener('click', event => {
    const picker = $('model-picker');
    if (picker.open && !picker.contains(event.target)) picker.open = false;
  });
  $('model-picker').addEventListener('keydown', event => {
    if (event.key === 'Escape' && $('model-picker').open) {
      $('model-picker').open = false;
      $('model-picker').querySelector('summary').focus();
    }
  });
  $('prev').addEventListener('click', () => goToPage(-1));
  $('next').addEventListener('click', () => goToPage(1));
  $('refresh-data').addEventListener('click', requestRefresh);
  $('status-refresh').addEventListener('click', () => {
    loadStatus();
    loadRefresh();
  });
  $('destination-form').addEventListener('submit', saveDestination);
  $('dest-clear').addEventListener('click', clearDestination);

  $('table').addEventListener('click', event => {
    const button = event.target.closest('button[data-id]');
    if (button) openDetail(button.dataset.id, button);
  });
  const dialog = $('detail');
  $('close').addEventListener('click', () => guardDiscard(() => dialog.close()));
  dialog.addEventListener('click', event => {
    if (event.target === dialog) guardDiscard(() => dialog.close());
  });
  dialog.addEventListener('cancel', event => {
    if (editor && editor.submitting) {
      event.preventDefault();
      return;
    }
    if (editorDirty()) {
      event.preventDefault();
      guardDiscard(() => dialog.close());
    }
  });
  dialog.addEventListener('close', () => {
    detailSeq += 1;
    editor = null;
    editorLoading = 0;
    window.clearTimeout(fetchTimer);
    fetchTimer = 0;
    $('close').disabled = false;
    view = null;
    setDetailNotice('', false);
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
  loadRefresh();
  loadListings();
}

init();
