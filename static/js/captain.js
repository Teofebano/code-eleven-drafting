// ── CONSTANTS ─────────────────────────────────────────────────────────────
const LETTERS = ['A','B','C','D'];
const POS_ORDER = { GK: 0, DEF: 1, MID: 2, ATK: 3 };
const POS_CLASS  = { GK: 'pos-gk', DEF: 'pos-def', MID: 'pos-mid', ATK: 'pos-atk' };
let currentSort = 'year';
const GROUP_LABELS = { g1: '≤ 2004', g2: '2005 – 2018', g3: '> 2018' };
const MCQ_SECONDS = 15;

// ── STATE ─────────────────────────────────────────────────────────────────
let me = null;
let gameState = null;
let ws = null;
let timerInterval = null;
let timerEnd = null;
let hasAnswered = false;
let soundCtx = null;

// ── SOUND ENGINE ──────────────────────────────────────────────────────────
function initSound() {
  soundCtx = new (window.AudioContext || window.webkitAudioContext)();
}

function playTone(freq, type, duration, vol = 0.3) {
  if (!soundCtx) return;
  const osc = soundCtx.createOscillator();
  const gain = soundCtx.createGain();
  osc.connect(gain); gain.connect(soundCtx.destination);
  osc.type = type; osc.frequency.value = freq;
  gain.gain.setValueAtTime(vol, soundCtx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.001, soundCtx.currentTime + duration);
  osc.start(); osc.stop(soundCtx.currentTime + duration);
}

function soundReveal() {
  // punchy drumroll-like reveal
  [0, 80, 160].forEach(delay => {
    setTimeout(() => playTone(180, 'sawtooth', 0.12, 0.4), delay);
  });
  setTimeout(() => playTone(440, 'square', 0.3, 0.5), 200);
  setTimeout(() => playTone(660, 'square', 0.25, 0.4), 350);
}

function soundCorrect() {
  playTone(523, 'sine', 0.15, 0.4);
  setTimeout(() => playTone(659, 'sine', 0.15, 0.15), 120);
  setTimeout(() => playTone(784, 'sine', 0.2, 0.35), 230);
  setTimeout(() => playTone(1047,'sine', 0.25, 0.4), 330);
}

function soundWrong() {
  playTone(220, 'sawtooth', 0.2, 0.4);
  setTimeout(() => playTone(180, 'sawtooth', 0.2, 0.3), 150);
}

function soundPick() {
  playTone(440, 'sine', 0.1, 0.3);
  setTimeout(() => playTone(660, 'sine', 0.12, 0.25), 100);
}

function soundUrgent() {
  playTone(880, 'square', 0.05, 0.15);
}

// ── INIT ──────────────────────────────────────────────────────────────────
async function init() {
  // first touch — init audio context
  document.addEventListener('click', () => { if (!soundCtx) initSound(); }, { once: true });

  me = await fetch('/api/auth/me').then(r => r.json());
  if (!me.role || (me.role !== 'captain' && me.role !== 'admin')) {
    window.location.href = '/';
    return;
  }
  document.getElementById('captain-name-header').textContent = me.name;
  connectWS();
  await loadGame();
}

// ── WEBSOCKET ─────────────────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/draft?token=`);
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    handleMsg(msg);
  };
  ws.onclose = () => setTimeout(connectWS, 2000);
  setInterval(() => { if (ws.readyState === 1) ws.send('ping'); }, 20000);
}

function handleMsg(msg) {
  switch (msg.type) {
    case 'round_started':
      hasAnswered = false;
      loadGame();
      startTimer(MCQ_SECONDS);
      soundReveal();
      break;
    case 'captain_answered':
      loadGame();
      break;
    case 'phase_change':
      loadGame();
      if (msg.phase === 'draft') soundPick();
      break;
    case 'player_picked':
      loadGame();
      if (msg.captain_id !== me.sub) soundPick();
      break;
    case 'reset':
      hasAnswered = false;
      loadGame();
      break;
  }
}

// ── DATA ──────────────────────────────────────────────────────────────────
async function loadGame() {
  const data = await fetch('/api/game').then(r => r.json());
  gameState = data;
  render();
}

// ── TIMER ─────────────────────────────────────────────────────────────────
function startTimer(seconds) {
  clearInterval(timerInterval);
  timerEnd = Date.now() + seconds * 1000;
  let lastUrgent = false;
  timerInterval = setInterval(() => {
    const left = Math.max(0, Math.ceil((timerEnd - Date.now()) / 1000));
    const el = document.getElementById('mcq-timer');
    if (el) {
      el.textContent = left + 's';
      const urgent = left <= 5;
      el.className = 'mcq-timer' + (urgent ? ' urgent' : '');
      if (urgent && !lastUrgent) { soundUrgent(); lastUrgent = true; }
    }
    if (left <= 0) clearInterval(timerInterval);
  }, 250);
}

// ── RENDER ────────────────────────────────────────────────────────────────
function render() {
  if (!gameState) return;
  const { phase } = gameState;
  const el = document.getElementById('main-content');
  if (phase === 'lobby' || !phase) {
    el.innerHTML = lobbyHTML();
  } else if (phase === 'mcq') {
    el.innerHTML = mcqHTML();
  } else if (phase === 'draft') {
    el.innerHTML = draftHTML();
  } else if (phase === 'done') {
    el.innerHTML = doneHTML();
  }
}

function lobbyHTML() {
  return `<div class="waiting fade-in">
    <div class="waiting-icon">⚽</div>
    <div class="waiting-title">Draft Room</div>
    <div class="waiting-sub"><span class="pulse-dot">●</span> Waiting for admin to start the first round…</div>
    <div style="margin-top:32px">` + teamsHTML() + `</div>
  </div>`;
}

function mcqHTML() {
  const { question, answers, group_index, current_group } = gameState;
  const myAnswer = (answers || []).find(a => a.captain_id === me.sub);
  const groupLabel = GROUP_LABELS[current_group] || '';

  if (myAnswer) {
    return answeredHTML(myAnswer);
  }

  if (!question) return '<div class="waiting"><div class="waiting-title">Loading question…</div></div>';

  return `<div class="mcq-container fade-in">
    <div class="round-label">Round ${group_index + 1} of 3 — ${groupLabel}</div>
    <div class="mcq-timer" id="mcq-timer">${MCQ_SECONDS}s</div>
    <div class="question-text">${question.text}</div>
    <div class="mcq-options">
      ${question.options.map((opt, i) => `
        <button class="mcq-btn" id="opt-${i}" onclick="submitAnswer(${i})">
          <span class="opt-letter">${LETTERS[i]}</span>
          ${opt}
        </button>`).join('')}
    </div>
  </div>`;
}

function answeredHTML(myAnswer) {
  const { answers, captains, question, group_index, current_group } = gameState;
  const sorted = [...(answers || [])].sort((a,b) => a.answered_at_ms - b.answered_at_ms);
  const correct = sorted.filter(a => a.is_correct);
  const myRank = myAnswer.is_correct ? correct.findIndex(a => a.captain_id === me.sub) + 1 : null;
  const groupLabel = GROUP_LABELS[current_group] || '';

  return `<div class="mcq-container fade-in">
    <div class="round-label">Round ${group_index + 1} of 3 — ${groupLabel}</div>
    <div style="font-size:1.3rem;font-weight:700;margin:20px 0 8px;color:${myAnswer.is_correct?'var(--lime)':'var(--danger)'}">
      ${myAnswer.is_correct ? `✓ Correct! You ranked #${myRank}` : '✗ Wrong — no pick slot this round'}
    </div>
    ${question ? `<div style="font-size:0.88rem;color:var(--muted);margin-bottom:20px">
      You answered: <strong style="color:var(--white)">${myAnswer.chosen_index !== null ? LETTERS[myAnswer.chosen_index] : '—'}</strong>
      ${!myAnswer.is_correct && question.correct_index !== undefined ? ` · Correct was <strong style="color:var(--lime)">${LETTERS[question.correct_index]}</strong>` : ''}
    </div>` : ''}
    <div class="card-title">Leaderboard</div>
    ${sorted.map(a => {
      const cap = (captains || []).find(c => c.id === a.captain_id);
      const rank = a.is_correct ? correct.findIndex(c => c.captain_id === a.captain_id) + 1 : null;
      return `<div style="display:flex;align-items:center;gap:10px;padding:10px;border-bottom:1px solid var(--border)">
        <div style="font-family:'Bebas Neue',sans-serif;font-size:1.3rem;width:26px;color:${a.is_correct?'var(--lime)':'var(--danger)'}">
          ${rank || '✗'}
        </div>
        <div style="font-weight:700">${cap?.name || a.captain_id} ${a.captain_id===me.sub?'<span style="color:var(--lime)">(you)</span>':''}</div>
        <div style="margin-left:auto;font-size:0.75rem;color:var(--muted)">${new Date(a.answered_at_ms).toLocaleTimeString()}</div>
      </div>`;
    }).join('')}
    <div style="margin-top:16px;font-size:0.83rem;color:var(--muted);text-align:center">
      <span class="pulse-dot">●</span> Waiting for all captains to answer…
    </div>
  </div>`;
}

function draftHTML() {
  const { draft_order, current_picker_index, players, captains, current_group, group_index } = gameState;
  const order = draft_order || [];
  const pickerIndex = current_picker_index ?? 0;
  const currentPicker = order[pickerIndex];
  const isMyTurn = currentPicker === me.sub;
  const groupLabel = GROUP_LABELS[current_group] || '';

  const groupPlayers = (players || [])
    .filter(p => getGroup(p.batch_year) === current_group)
    .sort((a,b) => a.name.localeCompare(b.name));
  const allPicked = groupPlayers.every(p => p.taken_by);

  let html = `<div class="fade-in">
    <div class="draft-header">
      <span>Draft · <span class="dh-group">${groupLabel}</span></span>
      <span class="mono" style="font-size:0.8rem;color:var(--muted)">Round ${(group_index||0)+1}/3</span>
    </div>
    <div class="order-chips">
      ${order.map((cid, i) => {
        const cap = (captains || []).find(c => c.id === cid);
        return `<div class="order-chip ${i===pickerIndex&&!allPicked?'active-pick':''}">
          <span class="chip-rank">${i+1}</span>${cap?.name||cid}
        </div>`;
      }).join('')}
    </div>`;

  if (allPicked) {
    html += `<div class="waiting-banner">All players in this group picked ✓ — waiting for admin to start next round.</div>`;
  } else if (isMyTurn) {
    html += `<div class="my-turn-banner">🟢 YOUR PICK — Select a player below</div>`;
  } else {
    const pickerName = (captains||[]).find(c=>c.id===currentPicker)?.name || currentPicker;
    html += `<div class="waiting-banner"><span class="pulse-dot">●</span> Waiting for <strong>${pickerName}</strong> to pick…</div>`;
  }

  html += `<div class="player-grid">
    ${groupPlayers.map(p => {
      const taken = !!p.taken_by;
      return `<div class="player-card ${taken?'taken':isMyTurn?'pickable':''}"
        ${!taken&&isMyTurn?`onclick="pickPlayer('${p.id}')"`:''}>
        <div class="pc-name">${p.name}</div>
        <div class="pc-meta">${p.position} · ${p.batch_year}</div>
        ${taken?`<div class="pc-owner">→ ${p.captain_name||p.taken_by}</div>`:''}
      </div>`;
    }).join('')}
  </div>
  <div class="divider"></div>
  <div class="card-title">Live Teams</div>
  ${teamsHTML()}
  </div>`;

  return html;
}

function doneHTML() {
  return `<div class="fade-in">
    <div class="results-header">Draft Complete 🏆</div>
    <p style="text-align:center;color:var(--muted);margin-bottom:28px">Final squads from today's draft.</p>
    ${teamsHTML()}
    <div style="text-align:center;margin-top:28px">
      <button class="btn btn-secondary" onclick="logout()">Exit Draft Room</button>
    </div>
  </div>`;
}

function posClass(pos) {
  return ({ GK:'pos-gk', DEF:'pos-def', MID:'pos-mid', ATK:'pos-atk' })[pos?.toUpperCase()] || 'pos-unknown';
}

function sortPlayers(players) {
  const sorted = [...players];
  if (currentSort === 'position') {
    const o = { GK:0, DEF:1, MID:2, ATK:3 };
    sorted.sort((a,b) => {
      const pa = o[a.position?.toUpperCase()] ?? 99;
      const pb = o[b.position?.toUpperCase()] ?? 99;
      return pa !== pb ? pa - pb : a.name.localeCompare(b.name);
    });
  } else if (currentSort === 'name') {
    sorted.sort((a,b) => a.name.localeCompare(b.name));
  } else {
    sorted.sort((a,b) => a.batch_year - b.batch_year);
  }
  return sorted;
}

function sortBar() {
  const labels = { year:'Batch Year', position:'Position', name:'Name' };
  return '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:16px;background:var(--card);border:1px solid var(--border);border-radius:5px;padding:10px 14px">'
    + '<span style="font-size:0.72rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px">Sort:</span>'
    + Object.keys(labels).map(s =>
        '<button onclick="reSort(' + "'" + s + "'" + ')" style="font-size:0.78rem;font-weight:600;padding:5px 12px;border-radius:3px;cursor:pointer;text-transform:uppercase;letter-spacing:0.4px;border:1.5px solid '
        + (currentSort===s ? 'var(--lime)' : 'var(--border)') + ';background:'
        + (currentSort===s ? 'rgba(184,255,63,0.07)' : 'transparent') + ';color:'
        + (currentSort===s ? 'var(--lime)' : 'var(--muted)') + '">'
        + labels[s] + '</button>'
      ).join('')
    + '</div>';
}

function reSort(s) {
  currentSort = s;
  const wrap = document.getElementById('teams-sort-wrap');
  if (wrap) wrap.innerHTML = sortBar() + teamsInnerHTML();
}

function teamsHTML() {
  return '<div id="teams-sort-wrap">' + sortBar() + teamsInnerHTML() + '</div>';
}

function teamsInnerHTML() {
  const { captains, players } = gameState || {};
  if (!captains || !captains.length) return '';
  return '<div class="teams-grid">' + captains.map(c => {
    const myPlayers = sortPlayers((players||[]).filter(p => p.taken_by === c.id));
    return '<div class="team-col">'
      + '<div class="team-header">' + c.name + '<span class="mono" style="font-size:0.78rem;color:var(--muted);font-weight:normal">' + myPlayers.length + '</span></div>'
      + (myPlayers.length
          ? myPlayers.map(p =>
              '<div class="team-player"><span>' + p.name + '</span>'
              + '<div style="display:flex;gap:6px;align-items:center">'
              + '<span class="pos-badge ' + posClass(p.position) + '">' + (p.position||'?') + '</span>'
              + '<span class="year-tag">' + p.batch_year + '</span>'
              + '</div></div>'
            ).join('')
          : '<div style="font-size:0.8rem;color:var(--muted);padding:6px 0">No picks yet</div>')
      + '</div>';
  }).join('') + '</div>';
}

// ── ACTIONS ───────────────────────────────────────────────────────────────
async function submitAnswer(chosenIndex) {
  if (hasAnswered) return;
  hasAnswered = true;
  if (!soundCtx) initSound();

  // disable all buttons immediately
  document.querySelectorAll('.mcq-btn').forEach(b => b.disabled = true);

  const res = await fetch('/api/game/answer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chosen_index: chosenIndex }),
  });

  if (res.ok) {
    const data = await res.json();
    // highlight
    const btn = document.getElementById(`opt-${chosenIndex}`);
    if (btn) btn.classList.add(data.is_correct ? 'correct' : 'wrong');
    if (data.is_correct) soundCorrect();
    else soundWrong();
    await loadGame();
  } else {
    hasAnswered = false;
    document.querySelectorAll('.mcq-btn').forEach(b => b.disabled = false);
  }
}

async function pickPlayer(playerId) {
  soundPick();
  const res = await fetch('/api/game/pick', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ player_id: playerId }),
  });
  if (!res.ok) {
    const d = await res.json();
    toast(d.detail || 'Pick failed', true);
  }
}

// ── HELPERS ───────────────────────────────────────────────────────────────
function getGroup(y) {
  if (y <= 2004) return 'g1';
  if (y <= 2018) return 'g2';
  return 'g3';
}

async function logout() {
  await fetch('/api/auth/logout', { method: 'POST' });
  window.location.href = '/';
}

function toast(msg, isError = false) {
  let el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show' + (isError ? ' error' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.className = '', 2800);
}

init();

// ── SORT HELPERS (injected) ───────────────────────────────────────────────
