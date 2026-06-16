// ── CONSTANTS ────────────────────────────────────────────────────────────
const GROUP_LABELS = { g1: '≤ 2004', g2: '2005 – 2018', g3: '> 2018' };
const GROUP_BADGE  = { g1: 'badge-g1', g2: 'badge-g2', g3: 'badge-g3' };
const LETTERS = ['A','B','C','D'];
const MCQ_SECONDS = 15;

// ── STATE ─────────────────────────────────────────────────────────────────
let gameState = null;
let ws = null;
let timerInterval = null;
let timerEnd = null;
let pendingDraftOrder = [];
let dragSrcIndex = null;

// ── INIT ──────────────────────────────────────────────────────────────────
async function init() {
  const me = await fetch('/api/auth/me').then(r => r.json());
  if (!me.role || me.role !== 'admin') {
    window.location.href = '/';
    return;
  }
  document.getElementById('admin-name').textContent = me.name;
  setupTabs();
  connectWS(me);
  loadAll();
}

function setupTabs() {
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
      if (tab.dataset.tab === 'settings') loadAdmins();
    });
  });
}

// ── WEBSOCKET ─────────────────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/draft?token=`);
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    handleWSMessage(msg);
  };
  ws.onclose = () => setTimeout(connectWS, 2000);
  // keepalive
  setInterval(() => { if (ws.readyState === 1) ws.send('ping'); }, 20000);
}

function handleWSMessage(msg) {
  switch (msg.type) {
    case 'round_started':
      loadGame();
      startClientTimer(MCQ_SECONDS);
      toast(`Round ${msg.group_index + 1} started — ${GROUP_LABELS[msg.group_id]}!`);
      break;
    case 'captain_answered':
      loadGame();
      break;
    case 'phase_change':
      loadGame();
      if (msg.phase === 'draft') toast('Draft order locked — pick phase started!');
      if (msg.phase === 'done') toast('Draft complete!');
      break;
    case 'player_picked':
      loadGame();
      break;
    case 'players_updated':
    case 'captains_updated':
      loadAll();
      break;
    case 'reset':
      loadGame();
      loadAll();
      toast('Draft has been reset.');
      break;
  }
}

// ── DATA LOADING ──────────────────────────────────────────────────────────
async function loadAll() {
  await Promise.all([loadGame(), loadCaptains(), loadPlayers(), loadQuestions()]);
}

async function loadGame() {
  const data = await fetch('/api/game').then(r => r.json());
  gameState = data;
  renderDraftControl();
}

async function loadCaptains() {
  const caps = await fetch('/api/captains').then(r => r.json());
  renderCaptainsTable(caps);
}

async function loadPlayers() {
  const players = await fetch('/api/players').then(r => r.json());
  renderPlayersTable(players);
}

async function loadQuestions() {
  const qs = await fetch('/api/questions').then(r => r.json());
  renderQuestions(qs);
}

async function loadAdmins() {
  const admins = await fetch('/api/admins').then(r => r.json());
  renderAdminsTable(admins);
}

// ── CAPTAINS ──────────────────────────────────────────────────────────────
async function addCaptain() {
  const name = document.getElementById('cap-name').value.trim();
  const pw   = document.getElementById('cap-pw').value;
  if (!name || !pw) { toast('Fill name and password', true); return; }
  const fd = new FormData();
  fd.append('name', name); fd.append('password', pw);
  await fetch('/api/captains', { method: 'POST', body: fd });
  document.getElementById('cap-name').value = '';
  document.getElementById('cap-pw').value = '';
  toast(`${name} added as captain`);
  loadCaptains();
}

async function deleteCaptain(id) {
  if (!confirm('Remove this captain?')) return;
  await fetch(`/api/captains/${id}`, { method: 'DELETE' });
  toast('Captain removed');
  loadCaptains();
}

async function updateCaptainPw(id) {
  const pw = prompt('New password for captain:');
  if (!pw) return;
  const fd = new FormData(); fd.append('password', pw);
  await fetch(`/api/captains/${id}/password`, { method: 'PUT', body: fd });
  toast('Password updated');
}

function renderCaptainsTable(caps) {
  const el = document.getElementById('captains-table');
  if (!caps.length) { el.innerHTML = '<div class="empty-state">No captains yet.</div>'; return; }
  el.innerHTML = `<table>
    <thead><tr><th>Name</th><th>ID</th><th>Created</th><th></th></tr></thead>
    <tbody>${caps.map(c => `
      <tr>
        <td><strong>${c.name}</strong></td>
        <td class="mono" style="font-size:0.72rem;color:var(--muted)">${c.id.slice(0,8)}…</td>
        <td class="mono" style="font-size:0.75rem;color:var(--muted)">${c.created_at ? c.created_at.split('T')[0] : '—'}</td>
        <td style="display:flex;gap:6px">
          <button class="btn btn-secondary btn-sm" onclick="updateCaptainPw('${c.id}')">🔑 Password</button>
          <button class="btn btn-danger btn-sm" onclick="deleteCaptain('${c.id}')">✕</button>
        </td>
      </tr>`).join('')}
    </tbody></table>`;
}

// ── PLAYERS ───────────────────────────────────────────────────────────────
function getGroup(y) {
  if (y <= 2004) return 'g1';
  if (y <= 2018) return 'g2';
  return 'g3';
}

async function addPlayer() {
  const name = document.getElementById('pl-name').value.trim();
  const pos  = document.getElementById('pl-pos').value;
  const year = parseInt(document.getElementById('pl-year').value);
  if (!name || !year) { toast('Fill all fields', true); return; }
  const fd = new FormData();
  fd.append('name', name); fd.append('position', pos); fd.append('batch_year', year);
  await fetch('/api/players', { method: 'POST', body: fd });
  document.getElementById('pl-name').value = '';
  document.getElementById('pl-year').value = '';
  toast(`${name} added`);
  loadPlayers();
}

async function uploadCSV() {
  const file = document.getElementById('csv-file').files[0];
  if (!file) { toast('Select a CSV file first', true); return; }
  const fd = new FormData(); fd.append('file', file);
  const res = await fetch('/api/players/csv', { method: 'POST', body: fd });
  const data = await res.json();
  const resultEl = document.getElementById('csv-result');
  if (res.ok) {
    resultEl.innerHTML = `<span class="text-lime">✓ Added ${data.added} players.</span>` +
      (data.errors.length ? `<br><span class="text-danger">Errors: ${data.errors.join(', ')}</span>` : '');
    toast(`${data.added} players uploaded`);
    loadPlayers();
  } else {
    resultEl.innerHTML = `<span class="text-danger">Error: ${data.detail}</span>`;
    toast('CSV upload failed', true);
  }
}

async function deletePlayer(id) {
  await fetch(`/api/players/${id}`, { method: 'DELETE' });
  loadPlayers();
}

async function assignPlayer(playerId, captainId) {
  await fetch(`/api/players/${playerId}/assign`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ captain_id: captainId || null }),
  });
  loadPlayers();
  loadGame();
}

async function clearPlayers() {
  if (!confirm('Remove ALL players? This cannot be undone.')) return;
  await fetch('/api/players', { method: 'DELETE' });
  toast('All players removed');
  loadPlayers();
}

function renderPlayersTable(players) {
  const el = document.getElementById('players-table');
  const countEl = document.getElementById('players-count');
  if (countEl) countEl.textContent = `Players (${players.length})`;
  if (!players.length) { el.innerHTML = '<div class="empty-state" style="padding:32px">No players yet.</div>'; return; }
  const caps = gameState ? (gameState.captains || []) : [];
  el.innerHTML = `<table>
    <thead><tr><th>Name</th><th>Pos</th><th>Year</th><th>Group</th><th>Assign to</th><th></th></tr></thead>
    <tbody>${players.map(p => {
      const g = getGroup(p.batch_year);
      const opts = '<option value="">— Unassigned —</option>' +
        caps.map(c => `<option value="${c.id}" ${p.taken_by===c.id?'selected':''}>${c.name}</option>`).join('');
      return `<tr>
        <td><strong>${p.name}</strong></td>
        <td><span class="tag-pos">${p.position}</span></td>
        <td class="mono">${p.batch_year}</td>
        <td><span class="badge ${GROUP_BADGE[g]}">${GROUP_LABELS[g]}</span></td>
        <td>
          <select style="padding:5px 8px;font-size:0.8rem;width:100%;min-width:130px"
            onchange="assignPlayer('${p.id}', this.value)">
            ${opts}
          </select>
        </td>
        <td><button class="btn btn-danger btn-sm" onclick="deletePlayer('${p.id}')">✕</button></td>
      </tr>`;
    }).join('')}</tbody></table>`;
}

// ── QUESTIONS ─────────────────────────────────────────────────────────────
async function addQuestion() {
  const text = document.getElementById('q-text').value.trim();
  const opts = Array.from(document.querySelectorAll('.q-opt')).map(i => i.value.trim());
  const correct = parseInt(document.getElementById('q-correct').value);
  if (!text || opts.some(o => !o)) { toast('Fill all fields', true); return; }
  const fd = new FormData();
  fd.append('text', text);
  fd.append('option_a', opts[0]); fd.append('option_b', opts[1]);
  fd.append('option_c', opts[2]); fd.append('option_d', opts[3]);
  fd.append('correct_index', correct);
  await fetch('/api/questions', { method: 'POST', body: fd });
  document.getElementById('q-text').value = '';
  document.querySelectorAll('.q-opt').forEach(i => i.value = '');
  toast('Question added');
  loadQuestions();
}

async function deleteQuestion(id) {
  await fetch(`/api/questions/${id}`, { method: 'DELETE' });
  toast('Question removed');
  loadQuestions();
}

function renderQuestions(qs) {
  const el = document.getElementById('questions-list');
  if (!qs.length) { el.innerHTML = '<div class="empty-state">No questions yet.</div>'; return; }
  el.innerHTML = qs.map((q, i) => `
    <div class="card fade-in" style="margin-bottom:12px">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">
        <div style="flex:1">
          <div class="mono" style="font-size:0.7rem;color:var(--muted);margin-bottom:6px">Q${i+1}</div>
          <div style="font-weight:600;margin-bottom:10px">${q.text}</div>
          <div style="display:flex;flex-direction:column;gap:3px">
            ${[q.option_a, q.option_b, q.option_c, q.option_d].map((o, oi) => `
              <div style="font-size:0.83rem;color:${oi===q.correct_index?'var(--lime)':'var(--muted)'}">
                <strong>${LETTERS[oi]}.</strong> ${o} ${oi===q.correct_index?'✓':''}
              </div>`).join('')}
          </div>
        </div>
        <button class="btn btn-danger btn-sm" onclick="deleteQuestion('${q.id}')">✕</button>
      </div>
    </div>`).join('');
}

// ── DRAFT CONTROL ─────────────────────────────────────────────────────────
async function startRound() {
  const res = await fetch('/api/game/start-round', { method: 'POST' });
  if (!res.ok) { const d = await res.json(); toast(d.detail, true); return; }
  toast('Round started!');
}

async function endDraft() {
  if (!confirm('End the draft? This finalises all teams.')) return;
  await fetch('/api/game/end-draft', { method: 'POST' });
  toast('Draft ended!');
}

async function resetDraft() {
  if (!confirm('Reset draft progress? All picks and MCQ answers will be cleared.')) return;
  await fetch('/api/game/reset', { method: 'POST' });
  toast('Draft reset');
}

async function applyDraftOrder() {
  const order = pendingDraftOrder;
  if (!order.length) { toast('No order to apply', true); return; }
  await fetch('/api/game/set-draft-order', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ order }),
  });
  toast('Draft order applied!');
}

function startClientTimer(seconds) {
  clearInterval(timerInterval);
  timerEnd = Date.now() + seconds * 1000;
  const el = document.getElementById('admin-timer');
  timerInterval = setInterval(() => {
    const left = Math.max(0, Math.ceil((timerEnd - Date.now()) / 1000));
    if (el) {
      el.textContent = left + 's';
      el.className = 'timer-display' + (left <= 5 ? ' urgent' : '');
    }
    if (left <= 0) clearInterval(timerInterval);
  }, 250);
}

function renderDraftControl() {
  if (!gameState) return;
  const { phase, group_index, current_group, draft_order, current_picker_index,
          question, answers, captains, players, history } = gameState;

  // status bar
  document.getElementById('st-phase').textContent = phase || 'lobby';
  document.getElementById('st-group').textContent = current_group ? GROUP_LABELS[current_group] : '—';
  document.getElementById('st-picks').textContent = history ? history.length : 0;

  // MCQ panel
  const mcqPanel = document.getElementById('mcq-panel');
  if (phase === 'mcq' && question) {
    mcqPanel.style.display = 'block';
    document.getElementById('admin-question').textContent = question.text;
    document.getElementById('admin-options').innerHTML = question.options
      .map((o, i) => `<div style="color:${i===question.correct_index?'var(--lime)':'var(--muted)'}">
        <strong>${LETTERS[i]}.</strong> ${o} ${i===question.correct_index?'✓':''}</div>`).join('');
  } else {
    mcqPanel.style.display = 'none';
  }

  // answer log
  renderAnswerLog(answers || [], captains || []);

  // draft order override (show when MCQ done but before draft, or during draft)
  const overrideCard = document.getElementById('order-override-card');
  if (draft_order && draft_order.length > 0) {
    overrideCard.style.display = 'block';
    pendingDraftOrder = [...draft_order];
    renderOrderList(draft_order, captains || [], current_picker_index);
  } else if (phase === 'mcq' && answers && answers.length > 0) {
    // compute preliminary order
    const correct = answers.filter(a => a.is_correct).sort((a,b) => a.answered_at_ms - b.answered_at_ms);
    pendingDraftOrder = correct.map(a => a.captain_id);
    if (pendingDraftOrder.length) {
      overrideCard.style.display = 'block';
      renderOrderList(pendingDraftOrder, captains || [], -1);
    }
  } else {
    overrideCard.style.display = 'none';
  }

  // live teams
  renderLiveTeams(players || [], captains || []);

  // pick history
  renderPickHistory(history || []);
}

function renderAnswerLog(answers, captains) {
  const el = document.getElementById('answer-log');
  if (!answers.length) { el.innerHTML = '<div class="empty-state">No answers yet.</div>'; return; }

  const sorted = [...answers].sort((a,b) => a.answered_at_ms - b.answered_at_ms);
  const correct = sorted.filter(a => a.is_correct);

  el.innerHTML = `<div class="answer-log">${sorted.map(a => {
    const cap = captains.find(c => c.id === a.captain_id);
    const rank = a.is_correct ? correct.findIndex(c => c.captain_id === a.captain_id) + 1 : null;
    const rankClass = rank ? `rank-${rank}` : 'wrong';
    const rankIcon = rank ? rank : '✗';
    return `<div class="answer-entry ${rankClass} fade-in">
      <div class="rank-num">${rankIcon}</div>
      <div>
        <div style="font-weight:700">${cap?.name || a.captain_id}</div>
        <div style="font-size:0.75rem;color:var(--muted)">
          ${a.chosen_index !== null ? LETTERS[a.chosen_index] : '—'} · 
          ${a.is_correct ? '<span class="text-lime">Correct</span>' : '<span class="text-danger">Wrong</span>'}
        </div>
      </div>
      <div class="mono" style="font-size:0.72rem;color:var(--muted);margin-left:auto">
        ${new Date(a.answered_at_ms).toLocaleTimeString()}
      </div>
    </div>`;
  }).join('')}</div>`;
}

function renderOrderList(order, captains, pickerIndex) {
  const el = document.getElementById('order-list');
  el.innerHTML = order.map((cid, i) => {
    const cap = captains.find(c => c.id === cid);
    return `<li class="order-item ${i === pickerIndex ? 'current-pick' : ''}"
      draggable="true"
      data-index="${i}"
      ondragstart="dragStart(event,${i})"
      ondragover="dragOver(event)"
      ondrop="dragDrop(event,${i})"
      ondragleave="dragLeave(event)">
      <span class="order-rank">${i+1}</span>
      <span class="order-name">${cap?.name || cid}</span>
      <span style="font-size:0.75rem;color:var(--muted)">⠿</span>
    </li>`;
  }).join('');
}

// drag-and-drop
function dragStart(e, i) { dragSrcIndex = i; e.dataTransfer.effectAllowed = 'move'; }
function dragOver(e) { e.preventDefault(); e.currentTarget.classList.add('drag-over'); }
function dragLeave(e) { e.currentTarget.classList.remove('drag-over'); }
function dragDrop(e, targetIndex) {
  e.currentTarget.classList.remove('drag-over');
  if (dragSrcIndex === null || dragSrcIndex === targetIndex) return;
  const newOrder = [...pendingDraftOrder];
  const [moved] = newOrder.splice(dragSrcIndex, 1);
  newOrder.splice(targetIndex, 0, moved);
  pendingDraftOrder = newOrder;
  renderOrderList(newOrder, gameState.captains || [], -1);
  dragSrcIndex = null;
}

function renderLiveTeams(players, captains) {
  const el = document.getElementById('live-teams');
  if (!captains.length) { el.innerHTML = '<div class="empty-state">No captains.</div>'; return; }
  el.innerHTML = `<div class="teams-grid">${captains.map(c => {
    const myPlayers = players.filter(p => p.taken_by === c.id);
    return `<div class="team-col">
      <div class="team-header">${c.name}<span class="mono" style="font-size:0.8rem;color:var(--muted);font-weight:normal">${myPlayers.length}</span></div>
      ${myPlayers.length ? myPlayers.map(p => `
        <div class="team-player">
          <span>${p.name}</span>
          <span class="tag-pos">${p.position} · ${p.batch_year}</span>
        </div>`).join('') : '<div style="font-size:0.8rem;color:var(--muted);padding:6px 0">No picks</div>'}
    </div>`;
  }).join('')}</div>`;
}

function renderPickHistory(history) {
  const el = document.getElementById('pick-history');
  if (!history.length) { el.innerHTML = '<div class="empty-state">No picks yet.</div>'; return; }
  const gl = { g1:'≤2004', g2:'2005-2018', g3:'>2018' };
  el.innerHTML = `<table>
    <thead><tr><th>#</th><th>Captain</th><th>Player</th><th>Pos</th><th>Year</th><th>Group</th><th>Time</th></tr></thead>
    <tbody>${history.map(h => `
      <tr>
        <td class="mono">${h.pick_number}</td>
        <td><strong>${h.captain_name}</strong></td>
        <td>${h.player_name}</td>
        <td><span class="tag-pos">${h.player_position}</span></td>
        <td class="mono">${h.player_year}</td>
        <td><span class="badge ${GROUP_BADGE[h.group_id]}">${gl[h.group_id]||h.group_id}</span></td>
        <td class="mono" style="font-size:0.72rem;color:var(--muted)">${h.picked_at ? h.picked_at.split('T')[1]?.slice(0,8) : '—'}</td>
      </tr>`).join('')}
    </tbody></table>`;
}

// ── ADMINS ────────────────────────────────────────────────────────────────
async function addAdmin() {
  const user = document.getElementById('new-admin-user').value.trim();
  const pw   = document.getElementById('new-admin-pw').value;
  if (!user || !pw) { toast('Fill all fields', true); return; }
  const fd = new FormData(); fd.append('username', user); fd.append('password', pw);
  const res = await fetch('/api/admins', { method: 'POST', body: fd });
  if (res.ok) { toast('Admin added'); loadAdmins(); }
  else { const d = await res.json(); toast(d.detail, true); }
}

async function deleteAdmin(id) {
  if (!confirm('Remove this admin?')) return;
  await fetch(`/api/admins/${id}`, { method: 'DELETE' });
  toast('Admin removed'); loadAdmins();
}

function renderAdminsTable(admins) {
  const el = document.getElementById('admins-table');
  el.innerHTML = `<table>
    <thead><tr><th>Username</th><th>Created</th><th></th></tr></thead>
    <tbody>${admins.map(a => `
      <tr>
        <td><strong>${a.username}</strong></td>
        <td class="mono" style="font-size:0.75rem;color:var(--muted)">${a.created_at?.split('T')[0]||'—'}</td>
        <td><button class="btn btn-danger btn-sm" onclick="deleteAdmin('${a.id}')">✕</button></td>
      </tr>`).join('')}
    </tbody></table>`;
}

// ── RESTORE ───────────────────────────────────────────────────────────────
async function restoreTeams() {
  const file = document.getElementById('restore-teams-file').files[0];
  if (!file) { toast('Select teams.csv first', true); return; }
  const fd = new FormData(); fd.append('file', file);
  const res = await fetch('/api/restore/teams', { method: 'POST', body: fd });
  const data = await res.json();
  const el = document.getElementById('restore-teams-result');
  if (res.ok) {
    el.innerHTML = `<span class="text-lime">✓ Added ${data.added} players, assigned ${data.assigned}.</span>` +
      (data.skipped.length ? `<br><span class="text-danger">Skipped: ${data.skipped.join(' · ')}</span>` : '');
    toast(`Restored: ${data.added} players, ${data.assigned} assignments`);
    loadAll();
  } else {
    el.innerHTML = `<span class="text-danger">Error: ${data.detail}</span>`;
    toast('Restore failed', true);
  }
}

async function restoreHistory() {
  const file = document.getElementById('restore-history-file').files[0];
  if (!file) { toast('Select draft-history.csv first', true); return; }
  const fd = new FormData(); fd.append('file', file);
  const res = await fetch('/api/restore/history', { method: 'POST', body: fd });
  const data = await res.json();
  const el = document.getElementById('restore-history-result');
  if (res.ok) {
    el.innerHTML = `<span class="text-lime">✓ Restored ${data.restored} history entries.</span>` +
      (data.skipped.length ? `<br><span class="text-danger">Skipped: ${data.skipped.join(' · ')}</span>` : '');
    toast(`History restored: ${data.restored} entries`);
    loadGame();
  } else {
    el.innerHTML = `<span class="text-danger">Error: ${data.detail}</span>`;
    toast('History restore failed', true);
  }
}

// ── LOGOUT ────────────────────────────────────────────────────────────────
async function logout() {
  await fetch('/api/auth/logout', { method: 'POST' });
  window.location.href = '/';
}

// ── TOAST ─────────────────────────────────────────────────────────────────
function toast(msg, isError = false) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show' + (isError ? ' error' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.className = '', 2800);
}

init();
