"""dashboard.py — live webhook + stream inspector served at /dashboard.

Self-contained: no CDN, no build step, no external fonts.

Two independent live panes, because the two channels answer different
questions and move at different speeds:

  HTTP webhooks   the call's control plane — REST we send, XML we return,
                  webhooks Vobiz posts back. Sparse, and the ORDER is the story.
  Stream events   the media plane — the WebSocket carrying audio. Control
                  frames (start / dtmf / playedStream / clearAudio / stop) shown
                  individually; audio frames rolled up, since they arrive ~50x
                  a second in each direction and would bury everything else.
"""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ICICI Lombard × Vobiz — AI Customer Support</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --line: #30363d;
    --text: #e6edf3; --dim: #8b949e;
    --out: #58a6ff; --in: #3fb950; --xml: #d29922; --err: #f85149; --stream: #bc8cff;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;

    /* Brand accent, shared by both marks. Kept separate from the event
       colours above, which encode direction and must stay distinguishable. */
    --brand: #ee4000;
    --brand-ink: #fff3ee;
  }
  * { box-sizing: border-box; }
  /* Full-height app shell: the page itself never scrolls. Header and sidebar
     stay put; only the two feeds scroll, each on its own. */
  html, body { height: 100%; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 15px/1.5 system-ui, -apple-system, sans-serif;
         display: flex; flex-direction: column; overflow: hidden; }
  header { padding: 12px 20px; border-bottom: 1px solid var(--line);
           display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
           background: var(--panel); flex: 0 0 auto;
           box-shadow: inset 0 2px 0 var(--brand); }
  h1 { font-size: 15px; margin: 0; font-weight: 600; letter-spacing: -0.01em; }

  /* Both marks sit on the dark chrome. The Vobiz mark is dark-on-transparent,
     so it gets a light plate; the ICICI Lombard mark is already reversed. */
  .brands { display: flex; align-items: center; gap: 12px; }
  .brands img { display: block; }
  .brands .icici { height: 22px; }
  .brands .vobiz { height: 20px; }
  .plate { background: #fff; border-radius: 5px; padding: 5px 9px; display: flex; }
  .cross { color: var(--dim); font-size: 15px; font-weight: 300; }
  .tagline { font-size: 12px; color: var(--dim); border-left: 1px solid var(--line);
             padding-left: 14px; }
  .status { font-size: 13px; color: var(--dim); display: flex; align-items: center; gap: 7px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--err); }
  .dot.live { background: var(--in); box-shadow: 0 0 8px var(--in); }

  /* min-height:0 is what lets a grid child actually scroll instead of growing
     the page — without it the feeds would push the layout taller and the whole
     window would scroll again. */
  main { flex: 1 1 auto; min-height: 0; display: grid;
         grid-template-columns: 320px 1fr 1fr; overflow: hidden; }

  aside { padding: 18px 20px; border-right: 1px solid var(--line);
          overflow-y: auto; min-height: 0; }
  fieldset { border: 1px solid var(--line); border-radius: 8px; padding: 14px; margin: 0 0 16px; }
  legend { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
           color: var(--dim); padding: 0 6px; }
  label { display: block; font-size: 12px; color: var(--dim); margin: 10px 0 4px; }
  input, select, button { width: 100%; padding: 9px 11px; border-radius: 6px;
    border: 1px solid var(--line); background: var(--bg); color: var(--text);
    font-size: 14px; font-family: inherit; }
  input:focus, select:focus { outline: 2px solid var(--brand); outline-offset: -1px; }
  button { background: var(--brand); color: var(--brand-ink); border: none; font-weight: 600;
           cursor: pointer; margin-top: 12px; }
  button:hover { filter: brightness(1.12); }
  button.ghost { background: transparent; color: var(--dim); border: 1px solid var(--line);
                 font-weight: 400; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .row { display: flex; gap: 8px; }
  .row button { margin-top: 0; }
  .seg { display: flex; margin-top: 4px; }
  .seg button { margin: 0; border-radius: 0; background: var(--bg); color: var(--dim);
                border: 1px solid var(--line); font-weight: 500; }
  .seg button:first-child { border-radius: 6px 0 0 6px; }
  .seg button:last-child { border-radius: 0 6px 6px 0; border-left: none; }
  .seg button.on { background: var(--brand); color: var(--brand-ink); border-color: var(--brand); }
  .hint { font-size: 12px; color: var(--dim); margin-top: 8px; line-height: 1.45; }
  .connected { font-size: 13px; margin: 2px 0 0; }
  .connected b { font-family: var(--mono); color: var(--brand); }
  .connected span { color: var(--dim); font-size: 12px; }
  .msg { font-size: 12px; margin-top: 10px; padding: 8px 10px; border-radius: 6px;
         display: none; white-space: pre-wrap; word-break: break-word; }
  .msg.ok { display: block; background: #0f2f1a; color: var(--in); }
  .msg.bad { display: block; background: #2d1214; color: var(--err); }

  /* Each pane is its own column: fixed heading, scrolling feed beneath it. */
  .pane { padding: 16px 18px 0; min-width: 0; min-height: 0;
          display: flex; flex-direction: column; overflow: hidden; }
  .pane + .pane { border-left: 1px solid var(--line); }
  .feed { flex: 1 1 auto; overflow-y: auto; min-height: 0;
          padding-bottom: 16px; scrollbar-gutter: stable; }
  .panehead { display: flex; align-items: baseline; gap: 10px; margin-bottom: 4px;
              flex: 0 0 auto; }
  .panehead h2 { font-size: 14px; margin: 0; font-weight: 600; }
  .panehead .n { font-family: var(--mono); font-size: 12px; color: var(--dim); }
  .legend { display: flex; gap: 12px; font-size: 11.5px; color: var(--dim);
            flex-wrap: wrap; margin-bottom: 12px; flex: 0 0 auto; }
  .key { display: inline-flex; align-items: center; gap: 5px; }
  .swatch { width: 9px; height: 9px; border-radius: 2px; }

  .ev { border: 1px solid var(--line); border-left-width: 3px; border-radius: 7px;
        margin-bottom: 7px; background: var(--panel); overflow: hidden; }
  .ev.out { border-left-color: var(--out); }
  .ev.in  { border-left-color: var(--in); }
  .ev.xml { border-left-color: var(--xml); }
  .ev.stream-in  { border-left-color: var(--stream); }
  .ev.stream-out { border-left-color: var(--stream); }
  .ev > summary { padding: 9px 12px; cursor: pointer; display: flex;
                  align-items: center; gap: 10px; flex-wrap: wrap; list-style: none; }
  .ev > summary::-webkit-details-marker { display: none; }
  .ev > summary:hover { background: #1c2129; }
  /* Own caret: the default marker is hidden, so without this there is nothing
     to say a row opens. Rotates when expanded. */
  .caret { color: var(--dim); font-size: 10px; transition: transform .12s;
           display: inline-block; width: 9px; }
  .ev[open] .caret { transform: rotate(90deg); }
  .ev[open] > summary { background: #1c2129; }
  /* Fixed width keeps the badges aligned down the column, so the arrow is the
     only thing the eye has to track. */
  .arrow { font-family: var(--mono); font-size: 11px; font-weight: 700;
           padding: 2px 8px; border-radius: 4px; white-space: nowrap;
           display: inline-flex; align-items: center; gap: 6px; }
  .arrow .ep { letter-spacing: 0.02em; }
  .arrow .dir { font-size: 13px; line-height: 1; opacity: .95; }
  .out .arrow { background: #0d2d4d; color: var(--out); }
  .in  .arrow { background: #0f2f1a; color: var(--in); }
  .xml .arrow { background: #3a2d08; color: var(--xml); }
  .stream-in .arrow, .stream-out .arrow { background: #2e1f47; color: var(--stream); }
  .ts { font-family: var(--mono); font-size: 11.5px; color: var(--dim); }
  .label { font-weight: 600; font-size: 13.5px; }
  .note { font-size: 12px; color: var(--dim); font-style: italic; }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; width: 100%; padding: 0 12px 9px; }
  .chip { font-family: var(--mono); font-size: 11px; background: var(--bg);
          border: 1px solid var(--line); border-radius: 4px; padding: 2px 7px; color: var(--dim); }
  .chip b { color: var(--text); font-weight: 600; }
  pre { margin: 0; padding: 11px 12px; background: #0a0e13; border-top: 1px solid var(--line);
        font-family: var(--mono); font-size: 11.5px; overflow-x: auto;
        color: #b6c2cf; white-space: pre-wrap; word-break: break-word; max-height: 320px; }
  .empty { color: var(--dim); font-size: 13.5px; padding: 24px 0; text-align: center; }

  @media (max-width: 1150px) {
    main { grid-template-columns: 300px 1fr; }
    .pane.stream { grid-column: 2; border-left: none; border-top: 1px solid var(--line); }
  }
  @media (max-width: 820px) {
    body { overflow: auto; height: auto; }
    main { grid-template-columns: 1fr; overflow: visible; }
    aside, .pane { overflow: visible; }
    .pane.stream { grid-column: 1; }
    .feed { overflow: visible; }
  }
</style>
</head>
<body>
<header>
  <div class="brands">
    <img class="icici" src="/static/icici-lombard-logo.png" alt="ICICI Lombard">
    <span class="cross">×</span>
    <span class="plate"><img class="vobiz" src="/static/vobiz-logo.png" alt="Vobiz"></span>
  </div>
  <div class="tagline">AI Customer Support — live call inspector</div>
  <div class="status"><span class="dot" id="dot"></span><span id="conn">connecting…</span></div>
  <div class="status">APP = Vobiz voice backend</div>
  <div class="status" id="persist"></div>
</header>

<main>
<aside>
  <fieldset>
    <legend>Your Vobiz account</legend>
    <div id="acct-form">
      <label for="aid">Auth ID</label>
      <input id="aid" placeholder="MA_XXXXXXXX" autocomplete="off">
      <label for="atok">Auth Token</label>
      <input id="atok" type="password" placeholder="••••••••" autocomplete="off">
      <button id="connectbtn">Connect account</button>
      <p class="hint">Held in memory for this session only — never stored on
        disk, never shown in the event feed.</p>
    </div>
    <div id="acct-on" hidden>
      <p class="connected">Connected <b id="acct-id"></b>
        <span id="acct-n"></span></p>
      <button class="ghost" id="disconnectbtn">Disconnect</button>
    </div>
    <div class="msg" id="acctmsg"></div>
  </fieldset>

  <fieldset>
    <legend>Place a customer call</legend>
    <label for="from" id="fromlabel">From (your number)</label>
    <select id="from"><option value="">server default</option></select>
    <label for="num">Customer number</label>
    <input id="num" placeholder="+91… (E.164)">
    <button id="callbtn">Call</button>
    <div class="msg" id="callmsg"></div>
  </fieldset>

  <fieldset>
    <legend>Voice agent</legend>
    <div class="seg">
      <button class="on" data-agent="ours">This agent</button>
      <button data-agent="theirs">Customer's agent</button>
    </div>
    <p class="hint" id="agenthint">ICICI Lombard agent running on this server.</p>

    <div id="extwrap" hidden>
      <label for="wss">Customer's WebSocket URL</label>
      <input id="wss" placeholder="wss://their-agent.example.com/ws">

      <label for="enc">Encoding</label>
      <select id="enc">
        <option value="audio/x-mulaw">audio/x-mulaw (G.711u)</option>
        <option value="audio/x-l16">audio/x-l16 (linear PCM)</option>
      </select>

      <label for="rate">Sample rate</label>
      <select id="rate">
        <option value="8000">8000 Hz</option>
        <option value="16000">16000 Hz</option>
        <option value="24000">24000 Hz — not in every region</option>
      </select>

      <div id="endianwrap" hidden>
        <label for="endian">L16 byte order</label>
        <select id="endian">
          <option value="be">be — big-endian (RFC 2586)</option>
          <option value="le">le — little-endian</option>
        </select>
        <p class="hint">If L16 frames arrive the right size but produce no
          audio, flip this.</p>
      </div>

      <p class="hint">Vobiz streams the call to their server, so <b>their</b>
        agent talks. The stream pane stays empty — this server never sees that
        audio — but every HTTP webhook still lands here.</p>
    </div>
  </fieldset>

  <fieldset>
    <legend>Escalate to human specialist</legend>
    <label>Which leg</label>
    <div class="seg">
      <button class="on" data-legs="aleg">A-leg</button>
      <button data-legs="bleg">B-leg</button>
    </div>
    <p class="hint" id="leghint">A-leg is the caller. The other leg keeps
      running its current flow.</p>

    <label>Destination type</label>
    <div class="seg">
      <button class="on" data-type="pstn">PSTN number</button>
      <button data-type="sip">SIP endpoint</button>
    </div>
    <label for="dest" id="destlabel">Phone number</label>
    <input id="dest" placeholder="+91…">

    <div id="sipwrap" hidden>
      <label for="siphdr">SIP headers <span style="opacity:.7">(optional)</span></label>
      <input id="siphdr" placeholder="X-VH-Ref=abc123,X-VH-Clinic=alpha">
      <p class="hint">Each key must <b>start</b> with <code>X-VH-</code>, and the
        key stem and value must be alphanumeric. Free text is not carriable —
        send an opaque id and look it up on the receiving side.</p>
    </div>

    <label for="uuid">Call UUID</label>
    <select id="uuid"></select>
    <button id="xferbtn">Transfer</button>
    <div class="msg" id="xfermsg"></div>
    <p class="hint" id="hint">Redirects the A-leg to a new XML document
      containing &lt;Dial&gt;&lt;Number&gt;.</p>
  </fieldset>

  <div class="row">
    <button class="ghost" id="refreshbtn">Refresh</button>
    <button class="ghost" id="clearbtn">Clear feed</button>
  </div>
</aside>

<section class="pane http">
  <div class="panehead"><h2>HTTP webhooks</h2><span class="n" id="n-http"></span></div>
  <div class="legend">
    <span class="key"><span class="swatch" style="background:var(--out)"></span>APP → VOBIZ · REST call</span>
    <span class="key"><span class="swatch" style="background:var(--xml)"></span>APP → VOBIZ · XML reply</span>
    <span class="key"><span class="swatch" style="background:var(--in)"></span>APP ← VOBIZ · webhook</span>
  </div>
  <div class="feed" id="feed-http"><p class="empty">Place a call to begin.</p></div>
</section>

<section class="pane stream">
  <div class="panehead"><h2>Stream events</h2><span class="n" id="n-stream"></span></div>
  <div class="legend">
    <span class="key"><span class="swatch" style="background:var(--stream)"></span>APP ⇄ VOBIZ · WebSocket media plane</span>
    <span class="key">audio frames rolled up every 2s</span>
  </div>
  <div class="feed" id="feed-stream"><p class="empty">Appears when the media stream opens.</p></div>
</section>
</main>

<script>
// A single uncaught error used to kill the rest of this script silently: no
// button handlers, no SSE, and nothing on screen to say why. Surface it in the
// header instead of leaving a dead page.
window.addEventListener('error', e => {
  const c = document.getElementById('conn');
  if (c) { c.textContent = 'script error — see console'; c.style.color = '#f85149'; }
  console.error('[dashboard]', e.error || e.message);
});

const $ = id => document.getElementById(id);
let type = 'pstn';
// 'ours' = this server's Pipecat agent (default). 'theirs' = the customer's
// WebSocket. Declared here, with the other UI state, because describeAgent()
// reads it during init — declaring it further down put it in a temporal dead
// zone and the ReferenceError killed the rest of the script.
let agent = 'ours';
let pending = null;      // call UUID to re-select after a refresh rebuilds the list
let userPicked = false;  // once you choose a UUID, new calls stop stealing the selection
const uuids = new Set();
const n = {http: 0, stream: 0};

let legs = 'aleg';

const LEG_HINT = {
  aleg: 'A-leg is the caller. The other leg keeps running its current flow.',
  bleg: 'B-leg is the callee/agent — swap who the caller is talking to without disturbing them.',
};

function describe() {
  const sip = type === 'sip';
  const who = legs === 'bleg' ? 'the B-leg' : 'the A-leg';
  $('destlabel').textContent = sip ? 'SIP URI' : 'Phone number';
  $('dest').placeholder = sip ? 'sip:agent@registrar.vobiz.ai' : '+91…';
  $('sipwrap').hidden = !sip;
  $('leghint').textContent = LEG_HINT[legs];
  $('hint').innerHTML = sip
    ? `Redirects ${who} to XML containing &lt;Dial&gt;&lt;User&gt;, which bridges onto a registered SIP endpoint.`
    : `Redirects ${who} to a new XML document containing &lt;Dial&gt;&lt;Number&gt;.`;
}

// Each .seg is its own radio group, so only clear the siblings in that group.
document.querySelectorAll('.seg').forEach(group => {
  group.querySelectorAll('button').forEach(b => b.onclick = () => {
    group.querySelectorAll('button').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    if (b.dataset.type) type = b.dataset.type;
    if (b.dataset.legs) legs = b.dataset.legs;
    if (b.dataset.agent) { agent = b.dataset.agent; describeAgent(); }
    describe();
  });
});
describe();

function flash(el, text, ok) {
  el.textContent = text;
  el.className = 'msg ' + (ok ? 'ok' : 'bad');
}

async function post(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  const t = await r.text();
  let d; try { d = JSON.parse(t); } catch { d = {detail: t}; }
  if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
  return d;
}

// Session id for a connected account. Kept in memory only — deliberately not
// localStorage, so closing the tab drops it.
let acctSession = null;

function describeAgent() {
  const theirs = agent === 'theirs';
  $('extwrap').hidden = !theirs;
  $('agenthint').textContent = theirs
    ? "Vobiz streams to the URL below. This server still handles the answer XML, transfers and webhooks."
    : "ICICI Lombard agent running on this server.";
  $('endianwrap').hidden = !(theirs && $('enc').value === 'audio/x-l16');
}

// L16 is the only encoding with a byte-order choice. Attached here, after the
// definition and after `agent` exists — calling it earlier threw a temporal
// dead zone ReferenceError that killed the whole script.
$('enc').onchange = describeAgent;
describeAgent();


function setNumbers(numbers) {
  const sel = $('from');
  sel.innerHTML = '';
  if (!numbers) {
    sel.innerHTML = '<option value="">server default</option>';
    return;
  }
  numbers.forEach(n => {
    const o = document.createElement('option');
    o.value = n.e164;
    o.textContent = n.e164 + (n.country ? '  ·  ' + n.country : '');
    sel.appendChild(o);
  });
}

$('connectbtn').onclick = async () => {
  const b = $('connectbtn'); b.disabled = true;
  try {
    const d = await post('/account/connect', {
      auth_id: $('aid').value.trim(), auth_token: $('atok').value.trim()
    });
    acctSession = d.session;
    $('atok').value = '';                 // do not leave the token in the DOM
    $('acct-id').textContent = d.auth_id;
    $('acct-n').textContent = '· ' + d.numbers.length + ' numbers';
    $('acct-form').hidden = true;
    $('acct-on').hidden = false;
    setNumbers(d.numbers);
    flash($('acctmsg'), 'Calls will be placed from your account.', true);
  } catch (e) {
    // The error div sits below a hidden block, so make sure it is actually
    // seen rather than silently rendered off-screen.
    flash($('acctmsg'), e.message, false);
    $('acctmsg').scrollIntoView({block: 'nearest'});
    console.error('[account/connect]', e);
  }
  b.disabled = false;
};

$('disconnectbtn').onclick = async () => {
  try { await post('/account/disconnect', {session: acctSession}); } catch {}
  acctSession = null;
  $('acct-form').hidden = false;
  $('acct-on').hidden = true;
  setNumbers(null);
  flash($('acctmsg'), 'Disconnected — back to the server account.', true);
};

$('callbtn').onclick = async () => {
  const b = $('callbtn'); b.disabled = true;
  try {
    const body = {phone_number: $('num').value.trim()};
    if (acctSession) body.session = acctSession;
    if ($('from').value) body.from_number = $('from').value;
    if (agent === 'theirs') {
      body.stream_url  = $('wss').value.trim();
      body.encoding    = $('enc').value;
      body.sample_rate = $('rate').value;
      if ($('enc').value === 'audio/x-l16') body.l16_endian = $('endian').value;
    }
    const d = await post('/start', body);
    flash($('callmsg'), 'Ringing — ' + d.call_uuid
      + (agent === 'theirs' ? '\nStreaming to the customer\'s agent.' : ''), true);
  } catch (e) { flash($('callmsg'), e.message, false); }
  b.disabled = false;
};

$('xferbtn').onclick = async () => {
  const b = $('xferbtn'); b.disabled = true;
  try {
    const d = await post('/initiate-transfer', {
      call_uuid: $('uuid').value, type, legs,
      destination: $('dest').value.trim(),
      sip_headers: type === 'sip' ? $('siphdr').value.trim() : ''
    });
    // Surface header warnings rather than letting a silently-dropped header set
    // look like success.
    const warn = (d.sip_header_warnings || []);
    flash($('xfermsg'),
      `Transfer accepted — ${legs} → ${type}` + (warn.length ? '\n⚠ ' + warn.join('\n⚠ ') : ''),
      warn.length === 0);
  } catch (e) { flash($('xfermsg'), e.message, false); }
  b.disabled = false;
};

$('uuid').onchange = () => { userPicked = true; };

$('clearbtn').onclick = async () => {
  await fetch('/events/clear', {method: 'POST'});
  $('feed-http').innerHTML = '<p class="empty">Cleared.</p>';
  $('feed-stream').innerHTML = '<p class="empty">Cleared.</p>';
  n.http = n.stream = 0; paint();
  uuids.clear(); $('uuid').innerHTML = ''; userPicked = false; pending = null;
};

function paint() {
  $('n-http').textContent = n.http ? n.http + ' events' : '';
  $('n-stream').textContent = n.stream ? n.stream + ' events' : '';
}

const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function render(ev) {
  const isStream = ev.kind === 'stream';
  const group = isStream ? 'stream' : 'http';
  const feed = $('feed-' + group);
  if (n[group] === 0) feed.innerHTML = '';
  n[group]++; paint();

  if (ev.call_uuid && !uuids.has(ev.call_uuid)) {
    uuids.add(ev.call_uuid);
    const o = document.createElement('option');
    o.value = o.textContent = ev.call_uuid;
    $('uuid').appendChild(o);
    if (pending && pending === ev.call_uuid) { $('uuid').value = pending; pending = null; }
    else if (!userPicked && !pending) { $('uuid').value = ev.call_uuid; }
  }

  const cls = isStream ? ('stream-' + ev.direction)
                       : (ev.kind === 'xml' ? 'xml' : ev.direction);
  // Both endpoints are named, and their sides never move: APP on the left,
  // VOBIZ on the right. Only the arrow flips, so direction is scannable.
  const arrow = `<span class="ep">APP</span>`
              + `<span class="dir">${ev.direction === 'out' ? '&rarr;' : '&larr;'}</span>`
              + `<span class="ep">VOBIZ</span>`;
  const chips = Object.entries(ev.highlights || {})
    .map(([k, v]) => `<span class="chip">${esc(k)} <b>${esc(v)}</b></span>`).join('');
  const raw = ev.raw && ev.raw.xml ? ev.raw.xml : JSON.stringify(ev.raw, null, 2);

  const d = document.createElement('details');
  d.className = 'ev ' + cls;
  d.innerHTML =
    `<summary>
       <span class="caret">▶</span>
       <span class="arrow">${arrow}</span>
       <span class="ts">${esc(ev.ts)}</span>
       <span class="label">${esc(ev.label)}</span>
       ${ev.note ? `<span class="note">${esc(ev.note)}</span>` : ''}
     </summary>
     ${chips ? `<div class="chips">${chips}</div>` : ''}
     <pre>${esc(raw)}</pre>`;
  feed.prepend(d);
}

// The SSE endpoint replays stored history before going live, so reconnecting
// is also how "refresh" is implemented — no second code path to drift.
let es = null;

function connect() {
  if (es) es.close();
  pending = $('uuid').value;
  $('feed-http').innerHTML = '<p class="empty">Loading…</p>';
  $('feed-stream').innerHTML = '<p class="empty">Loading…</p>';
  n.http = n.stream = 0; paint();
  uuids.clear(); $('uuid').innerHTML = '';

  setTimeout(() => {
    if (n.http === 0) $('feed-http').innerHTML = '<p class="empty">Place a call to begin.</p>';
    if (n.stream === 0) $('feed-stream').innerHTML =
      '<p class="empty">Appears when the media stream opens.</p>';
  }, 700);

  es = new EventSource('/events');
  es.onopen = () => { $('dot').classList.add('live'); $('conn').textContent = 'live'; };
  es.onerror = () => { $('dot').classList.remove('live'); $('conn').textContent = 'reconnecting…'; };
  es.onmessage = e => { try { render(JSON.parse(e.data)); } catch {} };
}

$('refreshbtn').onclick = connect;
connect();

fetch('/events/info').then(r => r.json())
  .then(d => { $('persist').textContent = d.persisted + ' stored · ' + d.file; })
  .catch(() => {});
</script>
</body>
</html>
"""
