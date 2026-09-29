'use strict';
const $ = (s, el = document) => el.querySelector(s);
const S = { user: null, busy: false, speak: false, audio: null, profile: null };
const SUGGESTIONS = [
  "I'm preparing my new home. Can you help me choose what I need?",
  'I would like to buy a new smartphone. What would suit me?',
  "That's too expensive. My budget is now 100, and I don't want the first option.",
  'Find something in this category within my budget.',
  "What's in my basket?",
  "Let's check out",
];

function store(key, value) {
  try { value === undefined ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, value); } catch (_) {}
}
function recall(key) { try { return sessionStorage.getItem(key); } catch (_) { return null; } }
function esc(t) { return String(t ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function usd(v) { return `$${Number(v).toFixed(2)}`; }

// Minimal, safe markdown: paragraphs, bullet/numbered lists, **bold**, *italic*.
function md(text) {
  const inline = s => esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/(^|[^*])\*(?!\s)(.+?)\*/g, '$1<em>$2</em>');
  const out = []; let list = null;
  for (const raw of text.split('\n')) {
    const line = raw.trimEnd();
    const m = line.match(/^\s*(?:[-*•]|(\d+)[.)])\s+(.*)$/);
    if (m) {
      const tag = m[1] ? 'ol' : 'ul';
      if (list !== tag) { if (list) out.push(`</${list}>`); out.push(`<${tag}>`); list = tag; }
      out.push(`<li>${inline(m[2])}</li>`);
      continue;
    }
    if (list) { out.push(`</${list}>`); list = null; }
    if (line.trim()) out.push(`<p>${inline(line.replace(/^#+\s*/, ''))}</p>`);
  }
  if (list) out.push(`</${list}>`);
  return out.join('');
}

async function api(path, body) {
  const r = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
  return data;
}

/* ---------- login ---------- */
$('#login-form').addEventListener('submit', async e => {
  e.preventDefault();
  const id = $('#user-id').value.trim().toUpperCase();
  $('#login-error').textContent = '';
  if (!/^U\d{6}$/.test(id)) { $('#login-error').textContent = 'Please enter a valid customer ID: U followed by 6 digits, e.g. U000001.'; return; }
  $('#login-btn').disabled = true;
  try { await openSession(id); } catch (err) { $('#login-error').textContent = err.message; }
  finally { $('#login-btn').disabled = false; }
});

async function openSession(id) {
  const s = await api('/api/session', { user_id: id });
  S.user = s.user_id; S.profile = s.overview;
  store('lc_user', S.user);
  $('#login').hidden = true; $('#app').hidden = false;
  $('#who-id').textContent = S.user;
  $('#messages').innerHTML = '';
  renderProfile(s.overview); renderBasket(s.basket); renderPrefs(s.prefs);
  renderCheckout(s.checkout);
  $('#portrait').textContent = ''; $('#portrait').classList.add('loading');
  loadJustForU(); loadPortrait();
  renderSuggestions();
  showView('chat');
  loadForYou();
  if (s.is_new) await send('', { action: 'session_start' });
  else {
    for (const m of s.history) m.role === 'user' ? addUser(m.text, m.channel) : addBotFromHistory(m);
    scrollDown();
  }
  pushJnps('login');
}

$('#switch').addEventListener('click', async () => {
  if (S.busy) return;
  stopAudio();
  await askXnps('switch');
  // End this customer's contact so it is written to their contact history.
  try { navigator.sendBeacon('/api/session/end', new Blob([JSON.stringify({ user_id: S.user })], { type: 'application/json' })); } catch (_) {}
  S.user = null; store('lc_user');
  $('#app').hidden = true; $('#login').hidden = false; $('#user-id').value = ''; $('#user-id').focus();
});
$('#new-chat').addEventListener('click', async () => {
  if (S.busy) return;
  await askXnps('new_chat');
  $('#new-chat').disabled = true;
  $('#messages').innerHTML = '<p class="muted small-text">Starting a new chat…</p>';
  try {
    await api('/api/session/reset', { user_id: S.user });
  } finally { $('#new-chat').disabled = false; }
  $('#messages').innerHTML = ''; renderPrefs({}); renderCheckout(null);
  await send('', { action: 'session_start' });
  pushJnps('new_chat');
});

/* ---------- feedback: JNPS (a past purchase, in the chat) and xNPS (the session, when leaving) ---------- */
function npsScale(onPick) {
  const wrap = document.createElement('div');
  wrap.innerHTML = `<div class="nps-scale" role="radiogroup" aria-label="Score from 1 to 10">${
    Array.from({ length: 10 }, (_, i) => i + 1).map(n => `<button type="button" role="radio" aria-checked="false" class="nps-btn ${n <= 6 ? 'det' : n <= 8 ? 'neu' : 'pro'}" data-score="${n}">${n}</button>`).join('')
  }</div><div class="nps-legend"><span>Not likely</span><span>Very likely</span></div>`;
  wrap.querySelectorAll('.nps-btn').forEach(b => b.addEventListener('click', () => {
    wrap.querySelectorAll('.nps-btn').forEach(x => { x.classList.toggle('picked', x === b); x.setAttribute('aria-checked', x === b); });
    onPick(Number(b.dataset.score));
  }));
  return wrap;
}
function surveyForm(question, sub, { onSubmit, onSkip, skipLabel = 'Not now' }) {
  const box = document.createElement('div'); box.className = 'survey';
  box.innerHTML = `<p class="survey-q">${esc(question)}</p>${sub ? `<p class="survey-sub">${esc(sub)}</p>` : ''}`;
  let score = null;
  const comment = document.createElement('textarea');
  comment.className = 'survey-comment'; comment.rows = 2; comment.maxLength = 1000; comment.hidden = true;
  comment.placeholder = 'Tell us why (optional)';
  const actions = document.createElement('div'); actions.className = 'survey-actions';
  actions.innerHTML = '<button type="button" class="btn primary small submit" disabled>Send feedback</button><button type="button" class="btn ghost small skip"></button>';
  actions.querySelector('.skip').textContent = skipLabel;
  box.append(npsScale(n => { score = n; comment.hidden = false; actions.querySelector('.submit').disabled = false; }), comment, actions);
  actions.querySelector('.submit').addEventListener('click', () => { if (score) onSubmit(score, comment.value.trim(), box); });
  actions.querySelector('.skip').addEventListener('click', () => onSkip(box));
  return box;
}
async function pushJnps(trigger) {
  if (!S.user) return;
  let survey;
  try { survey = (await api(`/api/feedback/jnps/${S.user}?trigger=${trigger}`)).survey; } catch (_) { return; }
  if (!survey) return;
  const el = document.createElement('div'); el.className = 'msg bot';
  const bubble = document.createElement('div'); bubble.className = 'bubble survey-card';
  const when = survey.purchased_at ? new Date(survey.purchased_at).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : '';
  bubble.innerHTML = '<p class="survey-kicker">Quick question about your purchase</p>';
  bubble.append(surveyForm(survey.question, `${survey.product_category.replace(/_/g, ' ')}${when ? ` · bought ${when}` : ''}`, {
    onSubmit: async (score, comment, box) => {
      try {
        await api('/api/feedback/jnps', { user_id: S.user, survey_id: survey.survey_id, score, comment });
        box.innerHTML = `<p class="survey-thanks">Thanks for rating ${esc(survey.product_name)}: ${score}/10.</p>`;
        send('', { action: 'jnps_followup', surveyId: survey.survey_id });
      } catch (e) { alert(e.message); }
    },
    onSkip: async box => {
      box.innerHTML = '<p class="survey-thanks">No problem, maybe next time.</p>';
      api('/api/feedback/jnps', { user_id: S.user, survey_id: survey.survey_id, dismissed: true }).catch(() => {});
    },
  }));
  el.append(bubble); $('#messages').append(el); scrollDown();
}
async function askXnps(trigger) {
  let q;
  try {
    const r = await api(`/api/feedback/xnps/eligible/${S.user}`);
    if (!r.eligible) return;
    q = r.question;
  } catch (_) { return; }
  const dlg = $('#xnps');
  const body = $('#xnps-body'); body.innerHTML = '';
  return new Promise(resolve => {
    const done = async payload => {
      try { await api('/api/feedback/xnps', { user_id: S.user, trigger, ...payload }); } catch (_) {}
      dlg.close(); resolve();
    };
    body.append(surveyForm(q, 'Your feedback on this session helps us improve.', {
      skipLabel: 'Skip',
      onSubmit: (score, comment) => done({ score, comment }),
      onSkip: () => done({ dismissed: true }),
    }));
    dlg.onclose = () => resolve();
    dlg.showModal();
  });
}

/* ---------- views (mobile tabs) ---------- */
document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => showView(t.dataset.view)));
function showView(v) {
  document.querySelectorAll('.tab').forEach(t => { t.classList.toggle('active', t.dataset.view === v); t.setAttribute('aria-selected', t.dataset.view === v); });
  $('#view-chat').classList.toggle('active', v === 'chat');
  $('#view-foryou').classList.toggle('active', v === 'foryou');
  $('#view-panel').classList.toggle('active', v === 'panel');
  if (v === 'foryou') loadForYou();
}

/* ---------- chat ---------- */
function scrollDown() { const m = $('#messages'); m.scrollTop = m.scrollHeight; }
function addUser(text, channel) {
  const el = document.createElement('div');
  el.className = 'msg user';
  el.innerHTML = esc(text) + (channel === 'voice' ? '<span class="via">🎤 voice</span>' : '');
  $('#messages').append(el); scrollDown();
}
function addBot() {
  const el = document.createElement('div');
  el.className = 'msg bot';
  el.innerHTML = '<div class="bubble" hidden></div><div class="extras"></div><div class="status"><span class="dots">Thinking</span></div>';
  $('#messages').append(el); scrollDown();
  return el;
}
function addBotFromHistory(m) {
  const el = addBot();
  el.querySelector('.status').remove();
  if (m.text) { const b = el.querySelector('.bubble'); b.hidden = false; b.innerHTML = md(m.text); }
  for (const it of m.items || []) renderItem(el, it, true);
}
function renderItem(el, ev, historic) {
  const box = el.querySelector('.extras');
  if (ev.type === 'products') box.append(cardsBlock(ev.title, ev.items, true));
  if (ev.type === 'basket_card') box.append(basketCard(ev.basket));
  if (ev.type === 'order') box.append(orderBox(ev.order));
  if (ev.type === 'checkout' && historic) {
    const d = document.createElement('div'); d.className = 'muted small-text'; d.textContent = `Checkout summary prepared: total ${usd(ev.checkout.summary.total)}`; box.append(d);
  }
}

async function send(text, { channel = 'text', action = null, surveyId = null } = {}) {
  if (S.busy || !S.user) return;
  if (!text && !action) return;
  S.busy = true; $('#send').disabled = true; stopAudio();
  if (text && action !== 'session_start') addUser(text, channel);
  const el = addBot();
  const bubble = el.querySelector('.bubble'), status = el.querySelector('.status');
  let reply = '';
  try {
    const r = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user_id: S.user, message: text, channel, action, survey_id: surveyId }) });
    if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || 'The companion is unavailable.'); }
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        switch (ev.type) {
          case 'text': reply += ev.delta; bubble.hidden = false; bubble.innerHTML = md(reply); break;
          case 'status': status.innerHTML = `<span class="dots">${esc(ev.text)}</span>`; break;
          case 'products': case 'basket_card': renderItem(el, ev); break;
          case 'basket': renderBasket(ev.basket); break;
          case 'checkout': renderCheckout(ev.checkout); break;
          case 'checkout_cleared': renderCheckout(null); break;
          case 'order': renderItem(el, ev); renderCheckout(null); break;
          case 'prefs': renderPrefs(ev.prefs); break;
          case 'error': { const n = document.createElement('p'); n.className = 'error-note'; n.textContent = ev.message; el.querySelector('.extras').append(n); break; }
        }
        scrollDown();
      }
    }
  } catch (err) {
    const n = document.createElement('p'); n.className = 'error-note'; n.textContent = err.message; el.querySelector('.extras').append(n);
  } finally {
    status.remove(); S.busy = false; $('#send').disabled = false; scrollDown();
    loadJustForU(); loadPortrait();
    if (reply.trim() && (S.speak || channel === 'voice')) speak(reply);
  }
}

$('#composer').addEventListener('submit', e => {
  e.preventDefault();
  const t = $('#input').value.trim();
  if (!t) return;
  $('#input').value = '';
  send(t);
});

function renderSuggestions() {
  const box = $('#suggestions'); box.innerHTML = '';
  for (const s of SUGGESTIONS) {
    const b = document.createElement('button'); b.type = 'button'; b.textContent = s;
    b.addEventListener('click', () => send(s)); box.append(b);
  }
}

/* ---------- product cards ---------- */
function cardsBlock(title, items, numbered) {
  const wrap = document.createElement('div'); wrap.className = 'cards-block';
  if (title) { const h = document.createElement('p'); h.className = 'cards-title'; h.textContent = title; wrap.append(h); }
  const grid = document.createElement('div'); grid.className = 'cards';
  items.forEach((p, i) => grid.append(productCard(p, numbered ? i + 1 : null)));
  wrap.append(grid);
  return wrap;
}
function productCard(p, n) {
  const el = $('#product-tpl').content.firstElementChild.cloneNode(true);
  $('.p-num', el).textContent = n ? `Option ${n}` : '';
  $('.p-cat', el).textContent = p.category.replace(/_/g, ' ');
  $('.p-name', el).textContent = p.name;
  $('.p-price strong', el).textContent = usd(p.price);
  $('.p-price s', el).textContent = p.discount_pct ? usd(p.list_price) : '';
  $('.badge', el).textContent = p.discount_pct ? `-${p.discount_pct}%` : '';
  const meta = [`Quality ${p.quality_tier}/5`, p.styles.join(' · ')];
  if (p.subscription) meta.push('Subscription');
  if (p.compatible_os) meta.push(`${p.compatible_os} only`);
  $('.p-meta', el).textContent = meta.join(' • ');
  $('.p-why', el).textContent = p.why || '';
  if (p.eligible === false) { el.classList.add('muted-card'); $('.p-why', el).textContent = p.not_eligible_reason; $('.add', el).disabled = true; }
  $('.add', el).addEventListener('click', () => { showView('chat'); send(`Add ${p.name} to my basket`); });
  $('.more', el).addEventListener('click', () => { showView('chat'); send(`Tell me more about ${p.name}`); });
  return el;
}

async function loadForYou() {
  if (!S.user) return;
  const box = $('#view-foryou');
  if (box.dataset.user === S.user && box.childElementCount) return;
  box.dataset.user = S.user; box.innerHTML = '<p class="muted">Loading your picks…</p>';
  try {
    const d = await api(`/api/for-you/${S.user}`);
    box.innerHTML = '';
    for (const rail of d.rails) {
      if (!rail.items.length) continue;
      const sec = document.createElement('section'); sec.className = 'rail';
      sec.innerHTML = `<h3>${esc(rail.title)}<small>${esc(rail.section)}</small></h3>`;
      sec.append(cardsBlock('', rail.items, false)); box.append(sec);
    }
  } catch (e) { box.innerHTML = `<p class="error-note">${esc(e.message)}</p>`; }
}

/* ---------- side panel ---------- */
function renderBasket(b) {
  const box = $('#basket');
  const n = b.items.reduce((a, i) => a + i.quantity, 0);
  $('#tab-count').hidden = !n; $('#tab-count').textContent = n;
  if (!b.items.length) { box.innerHTML = '<p class="muted small-text">Your basket is empty.</p>'; return; }
  box.innerHTML = '';
  for (const i of b.items) {
    const line = document.createElement('div'); line.className = 'line';
    line.innerHTML = `<span class="name">${esc(i.product_name)} <span class="qty">× ${i.quantity}${i.is_subscription ? ' · subscription' : ''}</span></span><span>${esc(usd(i.line_total))}</span>`;
    const x = document.createElement('button'); x.className = 'x'; x.title = `Remove ${i.product_name}`; x.setAttribute('aria-label', x.title); x.textContent = '×';
    x.addEventListener('click', () => { showView('chat'); send(`Remove ${i.product_name} from my basket`); });
    line.append(x); box.append(line);
  }
  const t = document.createElement('div'); t.className = 'total'; t.innerHTML = `<span>Total</span><span>${esc(usd(b.total))}</span>`; box.append(t);
}
function renderCheckout(c) {
  const box = $('#checkout'); box.innerHTML = '';
  if (!c) return;
  const s = c.summary;
  const d = document.createElement('div'); d.className = 'checkout-box';
  const b = c.bucks || { points: 0 };
  const canUse = c.bucks_usable > 0;
  d.innerHTML = `<h3>Checkout summary</h3>${s.items.map(i => `<div class="line"><span class="name">${esc(i.product_name)} <span class="qty">× ${i.quantity}</span></span><span>${esc(usd(i.line_total))}</span></div>`).join('')}
    <div class="total"><span>Total</span><span>${esc(usd(s.total))}</span></div>
    <label class="bucks-toggle${canUse ? '' : ' disabled'}"><input type="checkbox" id="use-bucks" ${b.points ? 'checked' : ''} ${canUse ? '' : 'disabled'}>
      <span>Use my Bucks <span class="muted">(${esc(c.bucks_balance.toLocaleString())} available)</span></span></label>
    ${b.points ? `<div class="line bucks-line"><span class="name">Bucks (${esc(b.points.toLocaleString())})</span><span>−${esc(usd(b.value))}</span></div>
    <div class="total"><span>To pay (simulated)</span><span>${esc(usd(b.amount_due))}</span></div>` : ''}
    <div class="actions"><button class="btn ok" id="confirm-btn">Confirm order</button><button class="btn ghost" id="cancel-btn">Cancel</button></div>`;
  box.append(d);
  $('#use-bucks').addEventListener('change', async e => {
    e.target.disabled = true;
    try {
      const r = await api('/api/checkout/bucks', { user_id: S.user, use: e.target.checked });
      renderCheckout(r.checkout); renderBucks(r.bucks);
    } catch (err) { e.target.checked = !e.target.checked; e.target.disabled = false; alert(err.message); }
  });
  $('#confirm-btn').addEventListener('click', () => { showView('chat'); send('Confirm order', { action: 'confirm' }); });
  $('#cancel-btn').addEventListener('click', () => { showView('chat'); send('Cancel the checkout for now', { action: 'cancel_checkout' }); });
}
// The real basket, shown in the chat whenever the companion talks about it.
function basketCard(b) {
  const d = document.createElement('div'); d.className = 'basket-card';
  d.innerHTML = `<p class="cards-title">🛒 Your basket</p>${b.items.map(i => `<div class="line"><span class="name">${esc(i.product_name)} <span class="qty">× ${i.quantity}</span></span><span>${esc(usd(i.line_total))}</span></div>`).join('')}
    <div class="total"><span>Total</span><span>${esc(usd(b.total))}</span></div>`;
  return d;
}
function orderBox(o) {
  const d = document.createElement('div'); d.className = 'order-box';
  const used = o.bucks && o.bucks.points ? `<br>Bucks used ${esc(o.bucks.points.toLocaleString())} (−${esc(usd(o.bucks.value))}) · Paid ${esc(usd(o.amount_paid))}` : '';
  d.innerHTML = `<strong>✓ Order placed (simulated)</strong><br>${o.items.map(i => `${esc(i.quantity)} × ${esc(i.product_name)}`).join(', ')}<br>Total ${esc(usd(o.total))}${used}<br><code>${esc(o.order_id)}</code>`;
  return d;
}
/* ---------- Just For U: Bucks, Next Level (upsell), For U (cross-sell) ---------- */
// Collapsed by default; while collapsed the (+) blinks to invite the customer to open it.
function setJfuOpen(open, remember = true) {
  $('#jfu-body').hidden = !open;
  $('#jfu').classList.toggle('nudge', !open);
  $('#jfu').classList.toggle('open', open);
  const t = $('#jfu-toggle');
  t.textContent = open ? '−' : '+';
  t.setAttribute('aria-expanded', open);
  t.setAttribute('aria-label', open ? 'Minimise Just For U' : 'Open Just For U');
  t.title = open ? 'Minimise' : 'Open';
  if (remember) { try { localStorage.setItem('lc_jfu_open', open ? '1' : '0'); } catch (_) {} }
}
$('#jfu-toggle').addEventListener('click', e => { e.stopPropagation(); setJfuOpen($('#jfu-body').hidden); });
$('#jfu-head').addEventListener('click', () => { if ($('#jfu-body').hidden) setJfuOpen(true); });
(() => { let open = false; try { open = localStorage.getItem('lc_jfu_open') === '1'; } catch (_) {} setJfuOpen(open, false); })();

function renderBucks(b) {
  $('#bucks-points').textContent = `${b.points.toLocaleString()}`;
  $('#bucks-value').textContent = `= ${usd(b.value)}`;
  $('#jfu-balance').textContent = `Balance: ${Number(b.value).toFixed(2)} USD`;
}
function miniList(el, items, empty) {
  el.innerHTML = '';
  if (!items.length) { el.innerHTML = `<li class="mini-empty">${esc(empty)}</li>`; return; }
  for (const p of items) {
    const li = document.createElement('li');
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'mini'; b.title = `Add ${p.name} to my basket`;
    b.innerHTML = `<span class="mini-main"><span class="mini-name">${esc(p.name)}</span><span class="mini-why">${esc(p.why)}</span></span>
      <span class="mini-price">${esc(usd(p.price))}${p.discount_pct ? `<small>-${p.discount_pct}%</small>` : ''}</span><span class="mini-add" aria-hidden="true">+</span>`;
    b.addEventListener('click', () => { showView('chat'); send(`Add ${p.name} to my basket`); });
    li.append(b); el.append(li);
  }
}
async function loadJustForU() {
  if (!S.user) return;
  try {
    const d = await api(`/api/just-for-u/${S.user}`);
    renderBucks(d.bucks);
    const picks = d.next_level.length + d.for_u.length;
    $('#jfu-teaser').textContent = `${d.bucks.points.toLocaleString()} Bucks (${usd(d.bucks.value)})` + (picks ? ` · ${picks} picks for you` : '');
    miniList($('#next-level'), d.next_level, 'No upgrades to suggest yet.');
    miniList($('#for-u'), d.for_u, 'Add something to your basket to see what goes with it.');
  } catch (_) { /* the block is a bonus; the chat keeps working */ }
}
// A gentle portrait of the customer; the server only rewrites it when their signals change.
async function loadPortrait() {
  if (!S.user) return;
  const user = S.user, el = $('#portrait');
  try {
    const { text } = await api(`/api/portrait/${user}`);
    if (user !== S.user || !text) return;
    if (el.textContent !== text) { el.classList.remove('fresh'); void el.offsetWidth; el.classList.add('fresh'); }
    el.textContent = text;
  } catch (_) { /* optional */ } finally { el.classList.remove('loading'); }
}
function renderPrefs(p) {
  const box = $('#prefs');
  const keys = Object.keys(p || {});
  if (!keys.length) { box.className = 'muted small-text'; box.textContent = 'Tell me your budget, style or goal and I will keep it in mind.'; return; }
  box.className = '';
  const chips = [];
  if (p.goal) chips.push(`<span class="chip">🎯 ${esc(p.goal)}</span>`);
  if (p.budget != null) chips.push(`<span class="chip">💰 ${esc(p.budget)} ${p.budget_scope === 'total' ? 'total' : 'per item'}</span>`);
  if (p.min_quality) chips.push(`<span class="chip">Quality ${esc(p.min_quality)}+</span>`);
  for (const s of p.liked_styles || []) chips.push(`<span class="chip">♥ ${esc(s)}</span>`);
  for (const r of p.rejected_products || []) chips.push(`<span class="chip no">${esc(r.replace(/\s*\(I\d+\)/, ''))}</span>`);
  for (const n of p.notes || []) chips.push(`<span class="chip">📝 ${esc(n)}</span>`);
  box.innerHTML = `<div class="chips">${chips.join('')}</div>`;
}
function renderProfile(o) {
  const p = o.profile, a = o.activity;
  const rows = [['Region', p.region], ['Age', p.age_band], ['Household', p.household_size], ['Membership', p.membership_tier],
    ['Device', p.device], ['Monthly budget', usd(p.monthly_budget)],
    ['Interests', p.declared_interests.join(', ').replace(/_/g, ' ') || '—'], ['Top categories', a.top_categories.slice(0, 3).join(', ').replace(/_/g, ' ') || '—'],
    ['Marketing', p.marketing_opt_in ? 'opted in' : 'opted out']];
  $('#profile').innerHTML = `<dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;
}

/* ---------- voice: speech in (Web Speech API), speech out (Amazon Polly) ---------- */
const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null, listening = false;
if (!Rec) { $('#mic').title = 'Voice input needs Chrome, Edge or Safari'; }
$('#mic').addEventListener('click', () => {
  if (!Rec) { $('#voice-state').textContent = 'Voice input is not supported in this browser. Try Chrome, Edge or Safari.'; return; }
  if (listening) { rec.stop(); return; }
  stopAudio();
  rec = new Rec(); rec.lang = navigator.language || 'en-US'; rec.interimResults = true; rec.maxAlternatives = 1;
  let finalText = '';
  rec.onstart = () => { listening = true; $('#mic').classList.add('listening'); $('#voice-state').textContent = 'Listening… speak now.'; };
  rec.onresult = e => {
    let interim = '';
    for (const r of e.results) (r.isFinal ? (finalText = r[0].transcript) : (interim += r[0].transcript));
    $('#input').value = finalText || interim;
  };
  rec.onerror = e => { $('#voice-state').textContent = e.error === 'not-allowed' ? 'Microphone permission was denied.' : `Voice input error: ${e.error}`; };
  rec.onend = () => {
    listening = false; $('#mic').classList.remove('listening');
    const t = (finalText || $('#input').value).trim();
    if (t) { $('#input').value = ''; $('#voice-state').textContent = ''; send(t, { channel: 'voice' }); }
    else if (!$('#voice-state').textContent.startsWith('Voice') && !$('#voice-state').textContent.startsWith('Micro')) $('#voice-state').textContent = '';
  };
  rec.start();
});
$('#speaker').addEventListener('click', () => {
  S.speak = !S.speak; $('#speaker').setAttribute('aria-pressed', S.speak);
  $('#voice-state').textContent = S.speak ? 'Replies will be read aloud.' : '';
  if (!S.speak) stopAudio();
});
function stopAudio() {
  if (S.audio) { S.audio.pause(); S.audio = null; }
  if (window.speechSynthesis) speechSynthesis.cancel();
}
async function speak(text) {
  const plain = text.replace(/\*\*/g, '').replace(/^\s*[-*•]\s+/gm, '').replace(/\n+/g, ' ').slice(0, 2500);
  try {
    const r = await fetch('/api/tts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: plain }) });
    if (!r.ok) throw new Error('tts');
    const url = URL.createObjectURL(await r.blob());
    S.audio = new Audio(url); S.audio.onended = () => URL.revokeObjectURL(url);
    await S.audio.play();
  } catch (_) {
    if (window.speechSynthesis) speechSynthesis.speak(new SpeechSynthesisUtterance(plain));
  }
}

/* ---------- restore the session on reload ---------- */
const saved = recall('lc_user');
if (saved) openSession(saved).catch(() => store('lc_user'));
