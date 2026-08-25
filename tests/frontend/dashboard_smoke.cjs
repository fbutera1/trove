#!/usr/bin/env node
// DOM-stub smoke test for trove/dashboard/static/index.html inline script.
// Verifies: (1) script executes without error against a minimal DOM,
// (2) initial load fetches /api/nuggets, (3) switching to the Tasks view
// fetches /api/nuggets/tasks with horizon/assignee params, (4) task cards
// render due/assignee chips and author label, (5) drawer renders task fields.
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const html = fs.readFileSync(
  path.join(__dirname, '..', '..', 'trove', 'dashboard', 'static', 'index.html'),
  'utf8'
);

// ── Extract the inline script ─────────────────────────────────────────
const m = html.match(/<script>([\s\S]*?)<\/script>/);
assert(m, 'inline <script> not found');
const scriptSrc = m[1];

// ── Minimal DOM stub ──────────────────────────────────────────────────
const fetched = [];
const elements = {};

function makeElement(id) {
  const el = {
    id,
    value: '',
    textContent: '',
    dataset: {},
    className: '',
    style: {},
    _listeners: {},
    _innerHTML: '',
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) {
      this._innerHTML = String(v);
      // Setting innerHTML replaces content — empty string clears children.
      this.children = this._innerHTML ? this.children : [];
    },
    children: [],
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c, force) {
        const on = force === undefined ? !this._set.has(c) : !!force;
        if (on) this._set.add(c); else this._set.delete(c);
        return on;
      },
      contains(c) { return this._set.has(c); },
    },
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    appendChild(child) { (this.children ||= []).push(child); return child; },
    remove() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener(event, fn) {
      (this._listeners[event] ||= []).push(fn);
    },
  };
  return el;
}

const KNOWN_IDS = [
  'searchInput', 'searchBanner', 'searchQueryDisplay', 'clearSearchLink',
  'filterClassification', 'filterStatus', 'filterDate', 'filterSource',
  'sortControl', 'errorBanner', 'listArea', 'drawerOverlay', 'drawer',
  'drawerBody', 'drawerClose', 'viewNuggets', 'viewTasks',
  'filterHorizon', 'filterAssignee', 'searchBox',
];
KNOWN_IDS.forEach(id => { elements[id] = makeElement(id); });

const documentStub = {
  body: { classList: makeElement('body').classList },
  getElementById(id) {
    if (!elements[id]) elements[id] = makeElement(id);
    return elements[id];
  },
  createElement(tag) {
    return makeElement('<' + tag + '>');
  },
  addEventListener() {},
};

// ── Canned API responses ──────────────────────────────────────────────
const TASK_ITEMS = [
  {
    message_id: 't1', classification: 'task', status: 'enriched',
    created_at: Date.now() / 1000 - 3600, source: 'signal',
    raw_content: 'vacuum the car', summary: 'Vacuum the car',
    author: 'uuid-x', author_label: 'Jami',
    assignee: null, assignee_display: 'Jami',
    due_at: Date.now() / 1000 - 86400, // overdue
    entities: null, links: null, metadata: null, confidence: null,
  },
  {
    message_id: 't2', classification: 'task', status: 'enriched',
    created_at: Date.now() / 1000 - 1800, source: 'signal',
    raw_content: 'book HVAC appointment', summary: 'Book HVAC appointment',
    author: 'uuid-y', author_label: 'Frank',
    assignee: 'Sam', assignee_display: 'Sam',
    due_at: Date.now() / 1000 + 86400,
    entities: null, links: null, metadata: null, confidence: 0.9,
  },
];
const NUGGET_ITEMS = [
  {
    message_id: 'n1', classification: 'fact', status: 'enriched',
    created_at: Date.now() / 1000 - 100, source: 'signal',
    raw_content: 'the milk is almost gone', summary: 'Milk running low',
    author: 'uuid-x', author_label: 'Jami',
    due_at: null, assignee: null,
    entities: null, links: null, metadata: null, confidence: 0.8,
  },
  {
    // Regression: a RESOLVED task with a past due date must show a plain
    // date chip — never the red 'Overdue' chip (regression, 2026-08-25).
    message_id: 'n2', classification: 'task', status: 'resolved',
    created_at: Date.now() / 1000 - 3 * 86400, source: 'signal',
    raw_content: 'give Havok a bath', summary: 'Give Havok a bath',
    author: 'uuid-y', author_label: 'Frank',
    assignee: 'Frank', assignee_display: 'Frank',
    due_at: Date.now() / 1000 - 2 * 86400,
    entities: null, links: null, metadata: null, confidence: null,
  },
];

const fetchStub = async (url) => {
  fetched.push(String(url));
  if (String(url).startsWith('/api/nuggets/tasks')) {
    return { ok: true, status: 200, json: async () => ({ horizon: 'all', assignee: null, count: TASK_ITEMS.length, items: TASK_ITEMS }) };
  }
  if (String(url).startsWith('/api/nuggets/search')) {
    return { ok: true, status: 200, json: async () => ({ query: '', count: 0, results: [] }) };
  }
  if (String(url).match(/^\/api\/nuggets\/[^/]+$/)) {
    // detail — return the first task item shape
    return { ok: true, status: 200, json: async () => ({ ...TASK_ITEMS[0], related: [] }) };
  }
  return { ok: true, status: 200, json: async () => ({ count: NUGGET_ITEMS.length, limit: 20, offset: 0, items: NUGGET_ITEMS }) };
};

// ── Run the script ────────────────────────────────────────────────────
const fn = new Function('document', 'fetch', 'window', scriptSrc);
fn(documentStub, fetchStub, {});

// ── Helpers ───────────────────────────────────────────────────────────
const sleep = ms => new Promise(r => setTimeout(r, ms));
function fire(id, event) {
  const el = elements[id];
  (el._listeners?.[event] || []).forEach(f => f({ preventDefault() {} }));
}
function listCards() {
  return (elements.listArea.children || []).filter(c => c.className === 'nugget-card');
}

(async () => {
  await sleep(50); // let loadInitial() settle

  // 1) initial nugget load
  assert(fetched[0].startsWith('/api/nuggets?'), 'initial load should hit /api/nuggets, got: ' + fetched[0]);
  assert(!fetched[0].includes('/tasks'), 'initial load must not hit /api/nuggets/tasks');
  assert(listCards().length === 2, 'expected 2 nugget cards, got ' + listCards().length);
  const nuggetCard = listCards()[0];
  assert(nuggetCard.innerHTML.includes('Jami'), 'nugget card should show author label');
  assert(!nuggetCard.innerHTML.includes('chip-due'), 'non-task card must not have due chip');
  const resolvedTaskCard = listCards()[1];
  assert(resolvedTaskCard.innerHTML.includes('chip-due'), 'resolved task card should show a due chip (date)');
  assert(!resolvedTaskCard.innerHTML.includes('overdue'), 'resolved task with past due date must NOT show the overdue chip');
  assert(resolvedTaskCard.innerHTML.includes('Frank'), 'resolved task card should show assignee');

  // 2) switch to Tasks view
  fire('viewTasks', 'click');
  await sleep(50);
  const tasksUrl = fetched[fetched.length - 1];
  assert(tasksUrl.startsWith('/api/nuggets/tasks'), 'Tasks view should hit /api/nuggets/tasks, got: ' + tasksUrl);
  assert(documentStub.body.classList.contains('view-tasks'), 'body should carry view-tasks class');
  assert(viewTasksActive(), 'viewTasks button should be active');
  assert.strictEqual(listCards().length, 2, 'expected 2 task cards');

  const card1 = listCards()[0]; // overdue, self
  assert(card1.innerHTML.includes('chip-due') && card1.innerHTML.includes('overdue'), 'task 1 should show overdue due chip');
  assert(card1.innerHTML.includes('chip-assignee') && card1.innerHTML.includes('Jami'), 'task 1 should show self assignee chip (Jami)');
  assert(card1.innerHTML.includes('Jami'), 'task 1 card should show author label');

  const card2 = listCards()[1]; // future, assignee Sam
  assert(card2.innerHTML.includes('chip-due'), 'task 2 should show due chip');
  assert(!card2.innerHTML.includes('overdue'), 'task 2 is not overdue');
  assert(card2.innerHTML.includes('Sam'), 'task 2 should show assignee Sam');

  // 3) assignee filter population
  const assigneeOpts = elements.filterAssignee.innerHTML;
  assert(assigneeOpts.includes('Jami') && assigneeOpts.includes('Sam'), 'assignee filter should list Jami and Sam, got: ' + assigneeOpts);

  // 4) assignee filter change re-fetches with param
  elements.filterAssignee.value = 'Sam';
  fire('filterAssignee', 'change');
  await sleep(50);
  const lastUrl = fetched[fetched.length - 1];
  assert(lastUrl.includes('assignee=Sam'), 'assignee filter should pass assignee=Sam, got: ' + lastUrl);

  // 5) horizon filter change
  elements.filterHorizon.value = 'overdue';
  fire('filterHorizon', 'change');
  await sleep(50);
  const hUrl = fetched[fetched.length - 1];
  assert(hUrl.includes('horizon=overdue'), 'horizon filter should pass horizon=overdue, got: ' + hUrl);

  // 6) open drawer on a task card → detail fetch + task fields rendered
  const cardClicks = card1._listeners.click || [];
  assert(cardClicks.length >= 1, 'card click handler should be registered');
  cardClicks.forEach(f => f());
  await sleep(50);
  const detailFetch = fetched[fetched.length - 1];
  assert(detailFetch.startsWith('/api/nuggets/t1'), 'drawer should fetch detail, got: ' + detailFetch);
  const drawerHtml = elements.drawerBody.innerHTML;
  assert(drawerHtml.includes('Assignee'), 'drawer should render Assignee field');
  assert(drawerHtml.includes('Due'), 'drawer should render Due field');
  assert(drawerHtml.includes('Author'), 'drawer should render Author field');
  assert(drawerHtml.includes('Jami'), 'drawer should show resolved author name');

  // 7) switch back to Nuggets view
  fire('viewNuggets', 'click');
  await sleep(50);
  const backUrl = fetched[fetched.length - 1];
  assert(backUrl.startsWith('/api/nuggets?'), 'switching back should hit /api/nuggets, got: ' + backUrl);
  assert(!documentStub.body.classList.contains('view-tasks'), 'view-tasks class removed');

  function viewTasksActive() {
    return elements.viewTasks.classList.contains('active') === true;
  }

  console.log('SMOKE OK — fetch sequence:');
  fetched.forEach(u => console.log('  ' + u));
})().catch(e => { console.error('SMOKE FAILED:', e.message); process.exit(1); });
