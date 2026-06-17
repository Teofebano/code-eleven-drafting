// ── CONSTANTS ─────────────────────────────────────────────────────────────
const GROUP_LABELS = {g1:'≤ 2004',g2:'2005 – 2018',g3:'> 2018'};
const GROUP_BADGE  = {g1:'badge-g1',g2:'badge-g2',g3:'badge-g3'};
const LETTERS = ['A','B','C','D'];
const MCQ_SECONDS = 15;

// ── STATE ─────────────────────────────────────────────────────────────────
let me = null, ws = null, gameState = null;
let currentEventId = null, currentEventName = '';
let events = [], captains = [], players = [], questions = [];
let timerEnd = null, timerInterval = null, pendingOrder = [];
let dragSrcIdx = null;

// ── INIT ──────────────────────────────────────────────────────────────────
async function init() {
  me = await fetch('/api/auth/me').then(r=>r.json());
  if (!me.role || me.role !== 'admin') { window.location.href='/'; return; }
  document.getElementById('admin-name').textContent = me.name;
  setupTabs();
  await loadEvents();
  await loadCaptains();
  connectWS();
}

function setupTabs() {
  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(x=>x.classList.remove('active'));
      t.classList.add('active');
      document.getElementById('tab-'+t.dataset.tab).classList.add('active');
      if (t.dataset.tab==='settings') loadAdmins();
      if (t.dataset.tab==='players-db') loadPlayersDB();
    });
  });
}

// ── WEBSOCKET ─────────────────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol==='https:'?'wss':'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/draft?room=global`);
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'events_updated') loadEvents();
    if (msg.type === 'captains_updated') loadCaptains();
  };
  ws.onclose = () => setTimeout(connectWS, 2000);
  setInterval(()=>{ if(ws.readyState===1) ws.send('ping'); }, 20000);
}

function connectEventWS(eid) {
  const proto = location.protocol==='https:'?'wss':'ws';
  const evtWs = new WebSocket(`${proto}://${location.host}/ws/draft?room=event:${eid}`);
  evtWs.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (['round_started','captain_answered','phase_change','player_picked','reset'].includes(msg.type)) {
      loadGame();
      if (msg.type==='round_started') startTimer(MCQ_SECONDS);
    }
    if (msg.type==='players_updated') loadEventPlayers();
    if (msg.type==='fixtures_updated') loadFixtures();
  };
  evtWs.onclose = () => setTimeout(()=>connectEventWS(eid), 2000);
  return evtWs;
}

// ── EVENTS ────────────────────────────────────────────────────────────────
async function loadEvents() {
  events = await fetch('/api/events').then(r=>r.json());
  renderEventsList();
  renderEventSelector();
}

function renderEventsList() {
  const el = document.getElementById('events-list');
  if (!events.length) { el.innerHTML='<div class="empty-state">No events yet.</div>'; return; }
  el.innerHTML = `<table>
    <thead><tr><th>Event</th><th>Description</th><th>Status</th><th>Created</th><th></th></tr></thead>
    <tbody>${events.map(ev=>`<tr>
      <td><strong>${ev.name}</strong></td>
      <td style="color:var(--muted);font-size:0.85rem">${ev.description||'—'}</td>
      <td><span class="badge ${ev.status==='done'?'badge-g2':'badge-g1'}">${ev.status}</span></td>
      <td class="mono" style="font-size:0.75rem;color:var(--muted)">${(ev.created_at||'').split('T')[0]}</td>
      <td style="display:flex;gap:6px">
        <button class="btn btn-primary btn-sm" onclick="selectEvent('${ev.id}','${ev.name.replace(/'/g,"\\'")}')">Open</button>
        <button class="btn btn-danger btn-sm" onclick="deleteEvent('${ev.id}')">✕</button>
      </td>
    </tr>`).join('')}</tbody></table>`;
}

function renderEventSelector() {
  const sel = document.getElementById('event-selector');
  if (!sel) return;
  sel.innerHTML = '<option value="">— Select Event —</option>' +
    events.map(ev=>`<option value="${ev.id}" ${ev.id===currentEventId?'selected':''}>${ev.name}</option>`).join('');
}

async function createEvent() {
  const name = document.getElementById('ev-name').value.trim();
  const desc = document.getElementById('ev-desc').value.trim();
  if (!name) { toast('Enter event name', true); return; }
  const fd = new FormData(); fd.append('name',name); fd.append('description',desc);
  const res = await fetch('/api/events', {method:'POST',body:fd});
  const data = await res.json();
  document.getElementById('ev-name').value=''; document.getElementById('ev-desc').value='';
  toast(`Event "${name}" created`);
  await loadEvents();
  if (data.id) selectEvent(data.id, name);
}

async function deleteEvent(eid) {
  if (!confirm('Delete this event and all its data?')) return;
  await fetch(`/api/events/${eid}`, {method:'DELETE'});
  if (currentEventId===eid) { currentEventId=null; hideEventPanel(); }
  toast('Event deleted'); loadEvents();
}

let eventWs = null;
function selectEvent(eid, name) {
  currentEventId = eid; currentEventName = name;
  document.getElementById('current-event-name').textContent = name;
  document.getElementById('event-panel').style.display = 'block';
  document.getElementById('event-selector').value = eid;
  if (eventWs) { try { eventWs.close(); } catch(e){} }
  eventWs = connectEventWS(eid);
  loadEventPlayers(); loadGame(); loadFixtures();
  // switch to draft tab
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(t=>t.classList.remove('active'));
  document.querySelector('[data-tab="draft"]').classList.add('active');
  document.getElementById('tab-draft').classList.add('active');
}

function hideEventPanel() {
  document.getElementById('event-panel').style.display='none';
  document.getElementById('current-event-name').textContent='';
}

// ── CAPTAINS ──────────────────────────────────────────────────────────────
async function loadCaptains() {
  captains = await fetch('/api/captains').then(r=>r.json());
  renderCaptainsTable();
  renderCaptainSelects();
}

async function addCaptain() {
  const name=document.getElementById('cap-name').value.trim();
  const pw=document.getElementById('cap-pw').value;
  if (!name||!pw){toast('Fill name and password',true);return;}
  const fd=new FormData(); fd.append('name',name); fd.append('password',pw);
  await fetch('/api/captains',{method:'POST',body:fd});
  document.getElementById('cap-name').value=''; document.getElementById('cap-pw').value='';
  toast(`${name} added`); loadCaptains();
}

async function deleteCaptain(id) {
  if (!confirm('Remove captain?')) return;
  await fetch(`/api/captains/${id}`,{method:'DELETE'}); loadCaptains();
}

async function updateCaptainPw(id) {
  const pw=prompt('New password:'); if(!pw) return;
  const fd=new FormData(); fd.append('password',pw);
  await fetch(`/api/captains/${id}/password`,{method:'PUT',body:fd}); toast('Password updated');
}

function renderCaptainsTable() {
  const el=document.getElementById('captains-table');
  if (!captains.length){el.innerHTML='<div class="empty-state">No captains yet.</div>';return;}
  el.innerHTML=`<table><thead><tr><th>Name</th><th>Created</th><th></th></tr></thead>
    <tbody>${captains.map(c=>`<tr>
      <td><strong>${c.name}</strong></td>
      <td class="mono" style="font-size:0.75rem;color:var(--muted)">${(c.created_at||'').split('T')[0]||'—'}</td>
      <td style="display:flex;gap:6px">
        <button class="btn btn-secondary btn-sm" onclick="updateCaptainPw('${c.id}')">🔑</button>
        <button class="btn btn-danger btn-sm" onclick="deleteCaptain('${c.id}')">✕</button>
      </td></tr>`).join('')}</tbody></table>`;
}

function renderCaptainSelects() {
  // landing page captain select
  const sel=document.getElementById('captain-select-landing');
  if (sel) { sel.innerHTML='<option value="">— Select —</option>'+captains.map(c=>`<option value="${c.id}">${c.name}</option>`).join(''); }
  // fixture selects
  ['fixture-home','fixture-away'].forEach(id=>{
    const s=document.getElementById(id);
    if(s) { s.innerHTML='<option value="">— Captain —</option>'+captains.map(c=>`<option value="${c.id}">${c.name}</option>`).join(''); }
  });
}

// ── PLAYERS DB ────────────────────────────────────────────────────────────
let dbPlayers = [], dbSearchTimeout = null;

async function loadPlayersDB(q='') {
  const url = q ? `/api/players-db?q=${encodeURIComponent(q)}` : '/api/players-db';
  dbPlayers = await fetch(url).then(r=>r.json());
  renderPlayersDB();
}

function renderPlayersDB() {
  const el=document.getElementById('players-db-table');
  const cnt=document.getElementById('players-db-count');
  if(cnt) cnt.textContent=`Player Registry (${dbPlayers.length})`;
  if (!dbPlayers.length){el.innerHTML='<div class="empty-state">No players in registry.</div>';return;}
  el.innerHTML=`<table><thead><tr><th>Name</th><th>Pos</th><th>Year</th><th>City</th><th></th></tr></thead>
    <tbody>${dbPlayers.map(p=>`<tr>
      <td><strong>${p.name}</strong></td>
      <td><span class="tag-pos">${p.position}</span></td>
      <td class="mono">${p.batch_year}</td>
      <td style="color:var(--muted);font-size:0.85rem">${p.city||'—'}</td>
      <td style="display:flex;gap:6px">
        <button class="btn btn-secondary btn-sm" onclick="editPlayerDB('${p.id}','${p.name.replace(/'/g,"\\'")}','${p.position}',${p.batch_year},'${(p.city||'').replace(/'/g,"\\'")}')">Edit</button>
        <button class="btn btn-danger btn-sm" onclick="deletePlayerDB('${p.id}')">✕</button>
      </td></tr>`).join('')}</tbody></table>`;
}

async function addPlayerDB() {
  const name=document.getElementById('db-name').value.trim();
  const pos=document.getElementById('db-pos').value;
  const year=parseInt(document.getElementById('db-year').value);
  const city=document.getElementById('db-city').value.trim();
  if (!name||!year){toast('Fill name and year',true);return;}
  const fd=new FormData(); fd.append('name',name);fd.append('position',pos);fd.append('batch_year',year);fd.append('city',city);
  const res=await fetch('/api/players-db',{method:'POST',body:fd});
  if (!res.ok){const d=await res.json();toast(d.detail,true);return;}
  document.getElementById('db-name').value='';document.getElementById('db-year').value='';document.getElementById('db-city').value='';
  toast(`${name} added to registry`); loadPlayersDB();
}

async function editPlayerDB(id,name,pos,year,city) {
  const newName=prompt('Name:',name); if(!newName) return;
  const newPos=prompt('Position (GK/DEF/MID/ATK):',pos)||pos;
  const newYear=parseInt(prompt('Batch Year:',year)||year);
  const newCity=prompt('City:',city)||'';
  const fd=new FormData(); fd.append('name',newName);fd.append('position',newPos);fd.append('batch_year',newYear);fd.append('city',newCity);
  await fetch(`/api/players-db/${id}`,{method:'PUT',body:fd});
  toast('Player updated'); loadPlayersDB();
}

async function deletePlayerDB(id) {
  if (!confirm('Remove from registry?')) return;
  await fetch(`/api/players-db/${id}`,{method:'DELETE'}); loadPlayersDB();
}

async function uploadDBCSV() {
  const file=document.getElementById('db-csv-file').files[0];
  if (!file){toast('Select CSV',true);return;}
  const fd=new FormData(); fd.append('file',file);
  const res=await fetch('/api/players-db/csv',{method:'POST',body:fd});
  const data=await res.json();
  const el=document.getElementById('db-csv-result');
  if(res.ok){
    el.innerHTML=`<span class="text-lime">✓ Added ${data.added}, updated ${data.updated}.</span>`;
    toast(`DB: +${data.added} new, ${data.updated} updated`); loadPlayersDB();
  } else { el.innerHTML=`<span class="text-danger">${data.detail}</span>`; }
}

// ── EVENT PLAYERS ─────────────────────────────────────────────────────────
async function loadEventPlayers() {
  if (!currentEventId) return;
  players = await fetch(`/api/events/${currentEventId}/players`).then(r=>r.json());
  renderEventPlayers();
}

function renderEventPlayers() {
  const el=document.getElementById('event-players-table');
  const cnt=document.getElementById('event-players-count');
  if(cnt) cnt.textContent=`Roster (${players.length})`;
  if (!players.length){el.innerHTML='<div class="empty-state">No players in this event roster.</div>';return;}
  el.innerHTML=`<table><thead><tr><th>Name</th><th>Pos</th><th>Year</th><th>Assign to</th><th></th></tr></thead>
    <tbody>${players.map(p=>{
      const g=getGroup(p.batch_year);
      const opts='<option value="">— Unassigned —</option>'+captains.map(c=>`<option value="${c.id}" ${p.taken_by===c.id?'selected':''}>${c.name}</option>`).join('');
      return `<tr>
        <td><strong>${p.name}</strong></td>
        <td><span class="tag-pos">${p.position}</span></td>
        <td class="mono">${p.batch_year} <span class="badge ${GROUP_BADGE[g]}" style="margin-left:4px">${GROUP_LABELS[g]}</span></td>
        <td><select style="padding:5px 8px;font-size:0.8rem;width:100%;min-width:130px" onchange="assignPlayer('${p.id}',this.value)">${opts}</select></td>
        <td><button class="btn btn-danger btn-sm" onclick="deleteEventPlayer('${p.id}')">✕</button></td>
      </tr>`;
    }).join('')}</tbody></table>`;
}

function getGroup(y) { return y<=2004?'g1':y<=2018?'g2':'g3'; }

async function addEventPlayer() {
  const name=document.getElementById('ep-name').value.trim();
  const pos=document.getElementById('ep-pos').value;
  const year=parseInt(document.getElementById('ep-year').value);
  if (!name||!year){toast('Fill all fields',true);return;}
  const fd=new FormData(); fd.append('name',name);fd.append('position',pos);fd.append('batch_year',year);
  await fetch(`/api/events/${currentEventId}/players`,{method:'POST',body:fd});
  document.getElementById('ep-name').value=''; document.getElementById('ep-year').value='';
  toast(`${name} added`); loadEventPlayers();
}

async function assignPlayer(pid, cid) {
  await fetch(`/api/events/${currentEventId}/players/${pid}/assign`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({captain_id:cid||null})});
  loadEventPlayers(); loadGame();
}

async function deleteEventPlayer(pid) {
  await fetch(`/api/events/${currentEventId}/players/${pid}`,{method:'DELETE'}); loadEventPlayers();
}

async function clearEventPlayers() {
  if (!confirm('Remove ALL players from this event roster?')) return;
  await fetch(`/api/events/${currentEventId}/players`,{method:'DELETE'}); loadEventPlayers();
}

async function uploadEventCSV() {
  const file=document.getElementById('ep-csv-file').files[0];
  if (!file){toast('Select CSV',true);return;}
  const fd=new FormData(); fd.append('file',file);
  const res=await fetch(`/api/events/${currentEventId}/players/csv`,{method:'POST',body:fd});
  const data=await res.json();
  const el=document.getElementById('ep-csv-result');
  if(res.ok){
    el.innerHTML=`<span class="text-lime">✓ ${data.added_to_event} added to event, ${data.added_to_db} new to registry.</span>`
      +(data.errors?`<br><span class="text-danger">${data.errors} errors</span>`:'');
    toast(`Roster: +${data.added_to_event} players`); loadEventPlayers();
  } else { el.innerHTML=`<span class="text-danger">${data.detail}</span>`; }
}

// add from DB modal
let dbSearchResults = [];
async function searchDB() {
  const q=document.getElementById('db-search-input').value.trim();
  const url=q?`/api/players-db?q=${encodeURIComponent(q)}`:'/api/players-db';
  dbSearchResults = await fetch(url).then(r=>r.json());
  renderDBSearchResults();
}

function renderDBSearchResults() {
  const el=document.getElementById('db-search-results');
  if (!dbSearchResults.length){el.innerHTML='<div class="empty-state">No results.</div>';return;}
  const rosterIds=new Set(players.filter(p=>p.player_db_id).map(p=>p.player_db_id));
  el.innerHTML=dbSearchResults.map(p=>{
    const inRoster=rosterIds.has(p.id);
    return `<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)">
      <div style="flex:1">
        <span style="font-weight:700">${p.name}</span>
        <span class="tag-pos" style="margin-left:6px">${p.position}</span>
        <span class="mono" style="font-size:0.75rem;color:var(--muted);margin-left:4px">${p.batch_year} ${p.city?'· '+p.city:''}</span>
      </div>
      ${inRoster
        ?'<span style="font-size:0.75rem;color:var(--lime)">✓ In roster</span>'
        :`<button class="btn btn-primary btn-sm" onclick="addFromDB('${p.id}')">Add</button>`}
    </div>`;
  }).join('');
}

async function addFromDB(dbid) {
  await fetch(`/api/events/${currentEventId}/players/from-db`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({player_db_ids:[dbid]})});
  toast('Player added to roster'); loadEventPlayers(); searchDB();
}

// ── QUESTIONS ─────────────────────────────────────────────────────────────
async function loadQuestions() {
  questions = await fetch('/api/questions').then(r=>r.json());
  renderQuestions();
}

async function addQuestion() {
  const text=document.getElementById('q-text').value.trim();
  const opts=Array.from(document.querySelectorAll('.q-opt')).map(i=>i.value.trim());
  const correct=parseInt(document.getElementById('q-correct').value);
  if (!text||opts.some(o=>!o)){toast('Fill all fields',true);return;}
  const fd=new FormData(); fd.append('text',text);
  fd.append('option_a',opts[0]);fd.append('option_b',opts[1]);fd.append('option_c',opts[2]);fd.append('option_d',opts[3]);
  fd.append('correct_index',correct);
  await fetch('/api/questions',{method:'POST',body:fd});
  document.getElementById('q-text').value='';
  document.querySelectorAll('.q-opt').forEach(i=>i.value='');
  toast('Question added'); loadQuestions();
}

async function deleteQuestion(id) {
  await fetch(`/api/questions/${id}`,{method:'DELETE'}); loadQuestions();
}

function renderQuestions() {
  const el=document.getElementById('questions-list');
  if (!questions.length){el.innerHTML='<div class="empty-state">No questions.</div>';return;}
  el.innerHTML=questions.map((q,i)=>`<div class="card fade-in" style="margin-bottom:12px">
    <div style="display:flex;justify-content:space-between;gap:12px">
      <div style="flex:1">
        <div class="mono" style="font-size:0.7rem;color:var(--muted);margin-bottom:6px">Q${i+1}</div>
        <div style="font-weight:600;margin-bottom:8px">${q.text}</div>
        ${[q.option_a,q.option_b,q.option_c,q.option_d].map((o,oi)=>`
          <div style="font-size:0.82rem;color:${oi===q.correct_index?'var(--lime)':'var(--muted)'}">
            <strong>${LETTERS[oi]}.</strong> ${o} ${oi===q.correct_index?'✓':''}
          </div>`).join('')}
      </div>
      <button class="btn btn-danger btn-sm" onclick="deleteQuestion('${q.id}')">✕</button>
    </div></div>`).join('');
}

// ── DRAFT CONTROL ─────────────────────────────────────────────────────────
async function loadGame() {
  if (!currentEventId) return;
  gameState = await fetch(`/api/events/${currentEventId}/game`).then(r=>r.json());
  renderDraftControl();
}

async function startRound() {
  const res=await fetch(`/api/events/${currentEventId}/game/start-round`,{method:'POST'});
  if (!res.ok){const d=await res.json();toast(d.detail,true);}
}

async function endDraft() {
  if (!confirm('End draft?')) return;
  await fetch(`/api/events/${currentEventId}/game/end-draft`,{method:'POST'});
}

async function resetDraft() {
  if (!confirm('Reset draft progress for this event?')) return;
  await fetch(`/api/events/${currentEventId}/game/reset`,{method:'POST'});
}

async function applyDraftOrder() {
  await fetch(`/api/events/${currentEventId}/game/set-draft-order`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order:pendingOrder})});
  toast('Order applied');
}

function startTimer(s) {
  clearInterval(timerInterval); timerEnd=Date.now()+s*1000;
  timerInterval=setInterval(()=>{
    const left=Math.max(0,Math.ceil((timerEnd-Date.now())/1000));
    const el=document.getElementById('admin-timer');
    if(el){el.textContent=left+'s';el.className='timer-display'+(left<=5?' urgent':'');}
    if(left<=0) clearInterval(timerInterval);
  },250);
}

function renderDraftControl() {
  if (!gameState) return;
  const {phase,group_index,current_group,draft_order,current_picker_index,question,answers,captains:caps,players:pls,history} = gameState;
  document.getElementById('st-phase').textContent=phase||'lobby';
  document.getElementById('st-group').textContent=current_group?GROUP_LABELS[current_group]:'—';
  document.getElementById('st-picks').textContent=history?history.length:0;
  // MCQ panel
  const mcqPanel=document.getElementById('mcq-panel');
  if (phase==='mcq'&&question) {
    mcqPanel.style.display='block';
    document.getElementById('admin-question').textContent=question.text;
    document.getElementById('admin-options').innerHTML=question.options.map((o,i)=>
      `<div style="color:${i===question.correct_index?'var(--lime)':'var(--muted)'}"><strong>${LETTERS[i]}.</strong> ${o} ${i===question.correct_index?'✓':''}</div>`).join('');
  } else { mcqPanel.style.display='none'; }
  // answer log
  renderAnswerLog(answers||[], caps||[]);
  // order override
  const oc=document.getElementById('order-override-card');
  if (draft_order&&draft_order.length) {
    oc.style.display='block'; pendingOrder=[...draft_order];
    renderOrderList(draft_order, caps||[], current_picker_index);
  } else if (phase==='mcq'&&answers&&answers.length) {
    const correct=answers.filter(a=>a.is_correct).sort((a,b)=>a.answered_at_ms-b.answered_at_ms);
    pendingOrder=correct.map(a=>a.captain_id);
    if(pendingOrder.length){oc.style.display='block';renderOrderList(pendingOrder,caps||[],-1);}
  } else { oc.style.display='none'; }
  renderLiveTeams(pls||[], caps||[]);
  renderPickHistory(history||[]);
}

function renderAnswerLog(answers, caps) {
  const el=document.getElementById('answer-log');
  if (!answers.length){el.innerHTML='<div class="empty-state">No answers yet.</div>';return;}
  const sorted=[...answers].sort((a,b)=>a.answered_at_ms-b.answered_at_ms);
  const correct=sorted.filter(a=>a.is_correct);
  el.innerHTML='<div class="answer-log">'+sorted.map(a=>{
    const cap=caps.find(c=>c.id===a.captain_id);
    const rank=a.is_correct?correct.findIndex(c=>c.captain_id===a.captain_id)+1:null;
    return `<div class="answer-entry ${rank?'rank-'+rank:'wrong'} fade-in">
      <div class="rank-num">${rank||'✗'}</div>
      <div><div style="font-weight:700">${cap?.name||a.captain_id}</div>
      <div style="font-size:0.75rem;color:var(--muted)">${a.chosen_index!==null?LETTERS[a.chosen_index]:'—'} · ${a.is_correct?'<span class="text-lime">Correct</span>':'<span class="text-danger">Wrong</span>'}</div></div>
      <div class="mono" style="font-size:0.72rem;color:var(--muted);margin-left:auto">${new Date(a.answered_at_ms).toLocaleTimeString()}</div>
    </div>`;
  }).join('')+'</div>';
}

function renderOrderList(order, caps, pickerIdx) {
  document.getElementById('order-list').innerHTML=order.map((cid,i)=>{
    const cap=caps.find(c=>c.id===cid);
    return `<li class="order-item ${i===pickerIdx?'current-pick':''}" draggable="true"
      data-index="${i}" ondragstart="dragStart(event,${i})" ondragover="dragOver(event)"
      ondrop="dragDrop(event,${i})" ondragleave="dragLeave(event)">
      <span class="order-rank">${i+1}</span>
      <span class="order-name">${cap?.name||cid}</span>
      <span style="font-size:0.75rem;color:var(--muted)">⠿</span>
    </li>`;
  }).join('');
}

function dragStart(e,i){dragSrcIdx=i;e.dataTransfer.effectAllowed='move';}
function dragOver(e){e.preventDefault();e.currentTarget.classList.add('drag-over');}
function dragLeave(e){e.currentTarget.classList.remove('drag-over');}
function dragDrop(e,ti){
  e.currentTarget.classList.remove('drag-over');
  if(dragSrcIdx===null||dragSrcIdx===ti) return;
  const o=[...pendingOrder]; const [m]=o.splice(dragSrcIdx,1); o.splice(ti,0,m);
  pendingOrder=o; renderOrderList(o, gameState?.captains||[], -1); dragSrcIdx=null;
}

function renderLiveTeams(pls, caps) {
  const el=document.getElementById('live-teams');
  if (!caps.length){el.innerHTML='<div class="empty-state">No captains.</div>';return;}
  el.innerHTML='<div class="teams-grid">'+caps.map(c=>{
    const mine=pls.filter(p=>p.taken_by===c.id);
    return `<div class="team-col">
      <div class="team-header">${c.name}<span class="mono" style="font-size:0.8rem;color:var(--muted);font-weight:normal">${mine.length}</span></div>
      ${mine.length?mine.map(p=>`<div class="team-player"><span>${p.name}</span><span class="tag-pos">${p.position}</span></div>`).join(''):'<div style="font-size:0.8rem;color:var(--muted);padding:6px 0">No picks</div>'}
    </div>`;
  }).join('')+'</div>';
}

function renderPickHistory(hist) {
  const el=document.getElementById('pick-history');
  if (!hist.length){el.innerHTML='<div class="empty-state">No picks yet.</div>';return;}
  const gl={g1:'≤2004',g2:'2005-2018',g3:'>2018'};
  el.innerHTML=`<table><thead><tr><th>#</th><th>Captain</th><th>Player</th><th>Pos</th><th>Year</th><th>Group</th></tr></thead>
    <tbody>${hist.map(h=>`<tr>
      <td class="mono">${h.pick_number}</td><td><strong>${h.captain_name}</strong></td>
      <td>${h.player_name}</td><td><span class="tag-pos">${h.player_position}</span></td>
      <td class="mono">${h.player_year}</td>
      <td><span class="badge ${GROUP_BADGE[h.group_id]}">${gl[h.group_id]||h.group_id}</span></td>
    </tr>`).join('')}</tbody></table>`;
}

// ── FIXTURES ──────────────────────────────────────────────────────────────
let fixtures = [], standings = null;

async function loadFixtures() {
  if (!currentEventId) return;
  fixtures = await fetch(`/api/events/${currentEventId}/fixtures`).then(r=>r.json());
  standings = await fetch(`/api/events/${currentEventId}/standings`).then(r=>r.json());
  renderFixtures(); renderStandings();
}

async function addFixture() {
  const home=document.getElementById('fixture-home').value;
  const away=document.getElementById('fixture-away').value;
  const date=document.getElementById('fixture-date').value;
  if (!home||!away){toast('Select both teams',true);return;}
  if (home===away){toast('Teams must be different',true);return;}
  const fd=new FormData(); fd.append('home_captain_id',home); fd.append('away_captain_id',away); fd.append('match_date',date);
  await fetch(`/api/events/${currentEventId}/fixtures`,{method:'POST',body:fd});
  toast('Fixture added'); loadFixtures();
}

async function deleteFixture(fid) {
  if (!confirm('Delete fixture?')) return;
  await fetch(`/api/events/${currentEventId}/fixtures/${fid}`,{method:'DELETE'}); loadFixtures();
}

async function setResult(fid) {
  const hs=prompt('Home score:'); if(hs===null) return;
  const as_=prompt('Away score:'); if(as_===null) return;
  await fetch(`/api/events/${currentEventId}/fixtures/${fid}/result`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({home_score:parseInt(hs),away_score:parseInt(as_)})});
  toast('Result saved'); loadFixtures();
}

function renderFixtures() {
  const el=document.getElementById('fixtures-list');
  if (!fixtures.length){el.innerHTML='<div class="empty-state">No fixtures yet.</div>';return;}
  el.innerHTML=fixtures.map(f=>`
    <div class="card fade-in" style="margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <div style="font-size:0.75rem;color:var(--muted);min-width:80px">${f.match_date||'TBD'}</div>
        <div style="flex:1;display:flex;align-items:center;gap:10px;justify-content:center">
          <span style="font-weight:700;text-align:right;flex:1">${f.home_name}</span>
          <span style="font-family:'Bebas Neue',sans-serif;font-size:1.4rem;color:${f.status==='played'?'var(--lime)':'var(--muted)'};min-width:60px;text-align:center">
            ${f.status==='played'?`${f.home_score} — ${f.away_score}`:'vs'}
          </span>
          <span style="font-weight:700;flex:1">${f.away_name}</span>
        </div>
        <div style="display:flex;gap:6px;flex-shrink:0">
          <button class="btn btn-primary btn-sm" onclick="setResult('${f.id}')">Score</button>
          <button class="btn btn-secondary btn-sm" onclick="openEventModal('${f.id}','${f.home_captain_id}','${f.away_captain_id}')">Events</button>
          <button class="btn btn-danger btn-sm" onclick="deleteFixture('${f.id}')">✕</button>
        </div>
      </div>
      ${f.events&&f.events.length?`<div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:6px">
        ${f.events.map(ev=>`
          <div style="background:var(--pitch-mid);border:1px solid var(--border);border-radius:3px;padding:4px 8px;font-size:0.75rem;display:flex;align-items:center;gap:6px">
            <span>${ev.event_type==='goal'?'⚽':ev.event_type==='assist'?'🎯':'🧤'}</span>
            <span>${ev.player_name}</span>
            ${ev.minute?`<span class="mono" style="color:var(--muted)">${ev.minute}'</span>`:''}
            <button onclick="deleteFixtureEvent('${f.id}','${ev.id}')" style="background:none;border:none;color:var(--danger);cursor:pointer;padding:0;font-size:0.8rem">✕</button>
          </div>`).join('')}
      </div>`:''}
    </div>`).join('');
}

// fixture event modal
let activeFixtureId='', activeHomeCap='', activeAwayCap='';
function openEventModal(fid, homeCap, awayCap) {
  activeFixtureId=fid; activeHomeCap=homeCap; activeAwayCap=awayCap;
  renderEventModalPlayers();
  document.getElementById('fixture-event-modal').style.display='flex';
}
function closeEventModal() { document.getElementById('fixture-event-modal').style.display='none'; }

function renderEventModalPlayers() {
  const capSel=document.getElementById('fe-captain');
  capSel.innerHTML=captains.map(c=>`<option value="${c.id}">${c.name}</option>`).join('');
  capSel.onchange=()=>renderEventModalPlayerList();
  renderEventModalPlayerList();
}

function renderEventModalPlayerList() {
  const cid=document.getElementById('fe-captain').value;
  const capPlayers=players.filter(p=>p.taken_by===cid);
  const sel=document.getElementById('fe-player');
  sel.innerHTML='<option value="">— Select player —</option>'+
    capPlayers.map(p=>`<option value="${p.id}|${p.name}">${p.name} (${p.position})</option>`).join('');
}

async function addFixtureEvent() {
  const cid=document.getElementById('fe-captain').value;
  const pval=document.getElementById('fe-player').value;
  const etype=document.getElementById('fe-type').value;
  const min=document.getElementById('fe-minute').value;
  if (!pval){toast('Select player',true);return;}
  const [pid,pname]=pval.split('|');
  await fetch(`/api/events/${currentEventId}/fixtures/${activeFixtureId}/events`,{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({player_id:pid,player_name:pname,captain_id:cid,event_type:etype,minute:min?parseInt(min):null})
  });
  toast(`${etype} logged`); loadFixtures();
}

async function deleteFixtureEvent(fid, evid) {
  await fetch(`/api/events/${currentEventId}/fixtures/${fid}/events/${evid}`,{method:'DELETE'});
  loadFixtures();
}

function renderStandings() {
  if (!standings) return;
  const el=document.getElementById('standings-table');
  const {table,player_stats}=standings;
  if (!table.length){el.innerHTML='<div class="empty-state">No matches played yet.</div>';return;}
  el.innerHTML=`<div style="overflow-x:auto"><table>
    <thead><tr><th>#</th><th>Team</th><th>P</th><th>W</th><th>D</th><th>L</th><th>GF</th><th>GA</th><th>GD</th><th>Pts</th></tr></thead>
    <tbody>${table.map((t,i)=>`<tr style="${i===0?'background:rgba(184,255,63,0.05)':''}">
      <td class="mono" style="color:var(--muted)">${i+1}</td>
      <td><strong>${t.name}</strong></td>
      <td class="mono">${t.p}</td><td class="mono">${t.w}</td><td class="mono">${t.d}</td><td class="mono">${t.l}</td>
      <td class="mono">${t.gf}</td><td class="mono">${t.ga}</td>
      <td class="mono" style="color:${t.gd>0?'var(--lime)':t.gd<0?'var(--danger)':'var(--muted)'}">${t.gd>0?'+':''}${t.gd}</td>
      <td><strong style="color:var(--lime)">${t.pts}</strong></td>
    </tr>`).join('')}
    </tbody></table></div>`;
  // player stats
  const ps=document.getElementById('player-stats-table');
  if (!player_stats.length){ps.innerHTML='<div class="empty-state">No stats yet.</div>';return;}
  ps.innerHTML=`<table><thead><tr><th>Player</th><th>⚽ Goals</th><th>🎯 Assists</th><th>🧤 Cleansheets</th></tr></thead>
    <tbody>${player_stats.slice(0,20).map(p=>`<tr>
      <td><strong>${p.name}</strong></td>
      <td class="mono" style="color:var(--lime)">${p.goals||0}</td>
      <td class="mono">${p.assists||0}</td>
      <td class="mono">${p.cleansheets||0}</td>
    </tr>`).join('')}</tbody></table>`;
}

// ── RESTORE ───────────────────────────────────────────────────────────────
async function restoreTeams() {
  const file=document.getElementById('restore-teams-file').files[0];
  if (!file||!currentEventId){toast('Select file and open an event',true);return;}
  const fd=new FormData(); fd.append('file',file);
  const res=await fetch(`/api/events/${currentEventId}/restore/teams`,{method:'POST',body:fd});
  const data=await res.json();
  const el=document.getElementById('restore-result');
  if(res.ok){
    el.innerHTML=`<span class="text-lime">✓ ${data.added} added, ${data.assigned} assigned.</span>`
      +(data.skipped.length?`<br><span class="text-danger">${data.skipped.join(' · ')}</span>`:'');
    toast('Restore complete'); loadEventPlayers(); loadGame();
  } else { el.innerHTML=`<span class="text-danger">${data.detail}</span>`; }
}

// ── ADMINS ────────────────────────────────────────────────────────────────
async function loadAdmins() {
  const admins=await fetch('/api/admins').then(r=>r.json());
  const el=document.getElementById('admins-table');
  el.innerHTML=`<table><thead><tr><th>Username</th><th>Created</th><th></th></tr></thead>
    <tbody>${admins.map(a=>`<tr>
      <td><strong>${a.username}</strong></td>
      <td class="mono" style="font-size:0.75rem;color:var(--muted)">${(a.created_at||'').split('T')[0]||'—'}</td>
      <td><button class="btn btn-danger btn-sm" onclick="deleteAdmin('${a.id}')">✕</button></td>
    </tr>`).join('')}</tbody></table>`;
}

async function addAdmin() {
  const u=document.getElementById('new-admin-user').value.trim();
  const p=document.getElementById('new-admin-pw').value;
  if(!u||!p){toast('Fill all fields',true);return;}
  const fd=new FormData(); fd.append('username',u); fd.append('password',p);
  const res=await fetch('/api/admins',{method:'POST',body:fd});
  if(res.ok){toast('Admin added');loadAdmins();}
  else{const d=await res.json();toast(d.detail,true);}
}

async function deleteAdmin(id) {
  if(!confirm('Remove admin?')) return;
  await fetch(`/api/admins/${id}`,{method:'DELETE'}); toast('Removed'); loadAdmins();
}

// ── LOGOUT + TOAST ────────────────────────────────────────────────────────
async function logout() { await fetch('/api/auth/logout',{method:'POST'}); window.location.href='/'; }

function toast(msg, isError=false) {
  const el=document.getElementById('toast'); el.textContent=msg;
  el.className='show'+(isError?' error':'');
  clearTimeout(toast._t); toast._t=setTimeout(()=>el.className='',2800);
}

init();
