const LETTERS=['A','B','C','D'];
const GROUP_LABELS={g1:'≤ 2004',g2:'2005 – 2018',g3:'> 2018'};
const GROUP_BADGE={g1:'badge-g1',g2:'badge-g2',g3:'badge-g3'};
const MCQ_SECONDS=15;

let eid=null, eventData=null, gameState=null, ws=null;
let captains=[], players=[], fixtures=[], standings=null;
let timerInterval=null, timerEnd=null, pendingOrder=[], dragSrcIdx=null;
let activeFixtureId=null, dbSearchResults=[];

async function init(){
  const me=await fetch('/api/auth/me').then(r=>r.json());
  if(!me.role||me.role!=='admin'){window.location.href='/';return;}
  document.getElementById('admin-name-h').textContent=me.name;
  eid=location.pathname.split('/event/')[1];
  if(!eid){window.location.href='/admin';return;}
  setupTabs();
  await loadEventData();
  connectWS();
  loadAll();
}

function setupTabs(){
  document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(x=>x.classList.remove('active'));
    t.classList.add('active'); document.getElementById('tab-'+t.dataset.tab).classList.add('active');
    if(t.dataset.tab==='fixtures-tab'){renderCaptainSelects();loadFixtures();}
    if(t.dataset.tab==='draft-ctrl') loadGame();
  }));
}

async function loadEventData(){
  eventData=await fetch(`/api/events/${eid}`).then(r=>r.json());
  if(eventData.detail){document.getElementById('ev-title').textContent='Error loading event';return;}
  document.getElementById('ev-title').textContent=eventData.name||'Event';
  document.getElementById('ev-desc-line').textContent=eventData.description||'';
  document.getElementById('ev-code').textContent=eventData.access_code||'(none — regen to assign)';
  captains=eventData.captains||[];
}

async function regenCode(){
  const res=await fetch(`/api/events/${eid}/regen-code`,{method:'POST'});
  const data=await res.json();
  document.getElementById('ev-code').textContent=data.access_code;
  toast(`New code: ${data.access_code}`);
}

function connectWS(){
  const proto=location.protocol==='https:'?'wss':'ws';
  ws=new WebSocket(`${proto}://${location.host}/ws/draft?room=event:${eid}`);
  ws.onmessage=e=>{
    const msg=JSON.parse(e.data);
    if(['round_started','captain_answered','phase_change','player_picked','reset'].includes(msg.type)){
      loadGame(); if(msg.type==='round_started') startTimer(MCQ_SECONDS);
    }
    if(msg.type==='players_updated') loadPlayers();
    if(msg.type==='fixtures_updated') loadFixtures();
    if(msg.type==='captains_updated'){loadEventData();}
  };
  ws.onclose=()=>setTimeout(connectWS,2000);
  setInterval(()=>{if(ws&&ws.readyState===1)ws.send('ping');},20000);
}

async function loadAll(){await Promise.all([loadPlayers(),loadGame()]);}

async function loadPlayers(){
  players=await fetch(`/api/events/${eid}/players`).then(r=>r.json());
  renderPlayers();
}

async function loadGame(){
  gameState=await fetch(`/api/events/${eid}/game`).then(r=>r.json());
  captains=gameState.captains||[];
  renderDraftControl();
}

async function loadFixtures(){
  fixtures=await fetch(`/api/events/${eid}/fixtures`).then(r=>r.json());
  standings=await fetch(`/api/events/${eid}/standings`).then(r=>r.json()).catch(()=>null);
  renderFixtures(); renderStandings();
}

// ── ROSTER ────────────────────────────────────────────────────────────────
function getGroup(y){return y<=2004?'g1':y<=2018?'g2':'g3';}

async function addEventPlayer(){
  const name=document.getElementById('ep-name').value.trim();
  const pos=document.getElementById('ep-pos').value;
  const year=parseInt(document.getElementById('ep-year').value);
  if(!name||!year){toast('Fill all fields',true);return;}
  const fd=new FormData();fd.append('name',name);fd.append('position',pos);fd.append('batch_year',year);
  await fetch(`/api/events/${eid}/players`,{method:'POST',body:fd});
  document.getElementById('ep-name').value='';document.getElementById('ep-year').value='';
  toast(`${name} added`);
}

async function uploadEventCSV(){
  const file=document.getElementById('ep-csv-file').files[0];
  if(!file){toast('Select CSV',true);return;}
  const fd=new FormData();fd.append('file',file);
  const res=await fetch(`/api/events/${eid}/players/csv`,{method:'POST',body:fd});
  const data=await res.json();
  const el=document.getElementById('ep-csv-result');
  if(res.ok){
    el.innerHTML=`<span class="text-lime">✓ ${data.added_to_event} added to roster, ${data.added_to_db} new to DB.</span>`
      +(data.skipped_captains?` <span class="text-muted">(${data.skipped_captains} captains skipped)</span>`:'');
    toast(`+${data.added_to_event} players`);
  } else {el.innerHTML=`<span class="text-danger">${data.detail}</span>`;}
}

async function assignPlayer(pid, cid){
  await fetch(`/api/events/${eid}/players/${pid}/assign`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({captain_id:cid||null})});
}

async function deleteEventPlayer(pid){
  await fetch(`/api/events/${eid}/players/${pid}`,{method:'DELETE'});
}

async function clearEventPlayers(){
  if(!confirm('Remove ALL players from roster?')) return;
  await fetch(`/api/events/${eid}/players`,{method:'DELETE'}); toast('Roster cleared');
}

function renderPlayers(){
  const el=document.getElementById('ep-table');
  const cnt=document.getElementById('ep-count');
  if(cnt) cnt.textContent=`Roster (${players.length})`;
  if(!players.length){el.innerHTML='<div class="empty-state">No players.</div>';return;}
  const capOpts='<option value="">— Unassigned —</option>'+captains.map(c=>`<option value="${c.id}">${c.name}</option>`).join('');
  el.innerHTML=`<table><thead><tr><th>Name</th><th>Pos</th><th>Year</th><th>Group</th><th>Assign to</th><th></th></tr></thead>
    <tbody>${players.map(p=>{
      const g=getGroup(p.batch_year);
      const opts='<option value="">— Unassigned —</option>'+captains.map(c=>`<option value="${c.id}" ${p.taken_by===c.id?'selected':''}>${c.name}</option>`).join('');
      return `<tr>
        <td><strong>${p.name}</strong></td>
        <td><span class="tag-pos">${p.position}</span></td>
        <td class="mono">${p.batch_year}</td>
        <td><span class="badge ${GROUP_BADGE[g]}">${GROUP_LABELS[g]}</span></td>
        <td><select style="padding:5px 8px;font-size:.8rem;min-width:130px" onchange="assignPlayer('${p.id}',this.value)">${opts}</select></td>
        <td><button class="btn btn-danger btn-sm" onclick="deleteEventPlayer('${p.id}')">✕</button></td>
      </tr>`;
    }).join('')}</tbody></table>`;
}

// DB search
async function searchDB(){
  const q=document.getElementById('db-search-input').value.trim();
  if(!q){document.getElementById('db-search-results').innerHTML='<div class="empty-state" style="padding:16px">Type to search…</div>';return;}
  dbSearchResults=await fetch(`/api/players-db?q=${encodeURIComponent(q)}`).then(r=>r.json());
  const rosterIds=new Set(players.filter(p=>p.player_db_id).map(p=>p.player_db_id));
  const capNames=new Set(captains.map(c=>c.name.toLowerCase()));
  document.getElementById('db-search-results').innerHTML=dbSearchResults.length
    ?dbSearchResults.map(p=>{
      const inRoster=rosterIds.has(p.id); const isCap=capNames.has(p.name.toLowerCase());
      return `<div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--border)">
        <div style="flex:1"><span style="font-weight:700">${p.name}</span> <span class="tag-pos" style="margin-left:4px">${p.position}</span>
        <span class="mono" style="font-size:.72rem;color:var(--muted);margin-left:4px">${p.batch_year}${p.city?' · '+p.city:''}</span></div>
        ${isCap?'<span style="font-size:.72rem;color:var(--danger)">Captain</span>':
          inRoster?'<span style="font-size:.72rem;color:var(--lime)">✓ In roster</span>':
          `<button class="btn btn-primary btn-sm" onclick="addFromDB('${p.id}')">Add</button>`}
      </div>`;
    }).join('')
    :'<div class="empty-state" style="padding:12px">No results.</div>';
}

async function addFromDB(dbid){
  const res=await fetch(`/api/events/${eid}/players/from-db`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({player_db_ids:[dbid]})});
  const data=await res.json();
  if(data.skipped_captains) toast('That player is a captain in this event',true);
  else toast('Added to roster');
  searchDB();
}

// ── DRAFT ─────────────────────────────────────────────────────────────────
async function startRound(){const res=await fetch(`/api/events/${eid}/game/start-round`,{method:'POST'});if(!res.ok){const d=await res.json();toast(d.detail,true);}}
async function endDraft(){if(!confirm('End draft?'))return;await fetch(`/api/events/${eid}/game/end-draft`,{method:'POST'});}
async function resetDraft(){if(!confirm('Reset draft progress?'))return;await fetch(`/api/events/${eid}/game/reset`,{method:'POST'});}
async function applyDraftOrder(){
  await fetch(`/api/events/${eid}/game/set-draft-order`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order:pendingOrder})});
  toast('Order applied');
}
function exportFile(type){window.location.href=`/api/events/${eid}/export/${type}`;}

function startTimer(s){
  clearInterval(timerInterval);timerEnd=Date.now()+s*1000;
  timerInterval=setInterval(()=>{
    const left=Math.max(0,Math.ceil((timerEnd-Date.now())/1000));
    const el=document.getElementById('admin-timer');
    if(el){el.textContent=left+'s';el.className='timer-display'+(left<=5?' urgent':'');}
    if(left<=0)clearInterval(timerInterval);
  },250);
}

function renderDraftControl(){
  if(!gameState) return;
  const{phase,group_index,current_group,draft_order,current_picker_index,question,answers,history}=gameState;
  document.getElementById('st-phase').textContent=phase||'lobby';
  document.getElementById('st-group').textContent=current_group?GROUP_LABELS[current_group]:'—';
  document.getElementById('st-picks').textContent=history?history.length:0;
  const mcqP=document.getElementById('mcq-panel');
  if(phase==='mcq'&&question){
    mcqP.style.display='block';
    document.getElementById('admin-question').textContent=question.text;
    document.getElementById('admin-options').innerHTML=question.options.map((o,i)=>
      `<div style="color:${i===question.correct_index?'var(--lime)':'var(--muted)'}"><strong>${LETTERS[i]}.</strong> ${o} ${i===question.correct_index?'✓':''}</div>`).join('');
  } else {mcqP.style.display='none';}
  renderAnswerLog(answers||[]);
  const oc=document.getElementById('order-override-card');
  if(draft_order&&draft_order.length){
    oc.style.display='block';pendingOrder=[...draft_order];
    renderOrderList(draft_order,current_picker_index);
  } else if(phase==='mcq'&&answers&&answers.length){
    const correct=answers.filter(a=>a.is_correct).sort((a,b)=>a.answered_at_ms-b.answered_at_ms);
    pendingOrder=correct.map(a=>a.captain_id);
    if(pendingOrder.length){oc.style.display='block';renderOrderList(pendingOrder,-1);}
  } else {oc.style.display='none';}
  renderLiveTeams();
  renderPickHistory(history||[]);
  renderTeamRenames();
}

function renderAnswerLog(answers){
  const el=document.getElementById('answer-log');
  if(!answers.length){el.innerHTML='<div class="empty-state">No answers yet.</div>';return;}
  const sorted=[...answers].sort((a,b)=>a.answered_at_ms-b.answered_at_ms);
  const correct=sorted.filter(a=>a.is_correct);
  el.innerHTML='<div class="answer-log">'+sorted.map(a=>{
    const cap=captains.find(c=>c.id===a.captain_id);
    const rank=a.is_correct?correct.findIndex(c=>c.captain_id===a.captain_id)+1:null;
    return `<div class="answer-entry ${rank?'rank-'+rank:'wrong'} fade-in">
      <div class="rank-num">${rank||'✗'}</div>
      <div><div style="font-weight:700">${cap?.name||a.captain_id}</div>
      <div style="font-size:.75rem;color:var(--muted)">${a.chosen_index!==null?LETTERS[a.chosen_index]:'—'} · ${a.is_correct?'<span class="text-lime">Correct</span>':'<span class="text-danger">Wrong</span>'}</div></div>
      <div class="mono" style="font-size:.72rem;color:var(--muted);margin-left:auto">${new Date(a.answered_at_ms).toLocaleTimeString()}</div>
    </div>`;
  }).join('')+'</div>';
}

function renderOrderList(order,pickerIdx){
  document.getElementById('order-list').innerHTML=order.map((cid,i)=>{
    const cap=captains.find(c=>c.id===cid);
    return `<li class="order-item ${i===pickerIdx?'current-pick':''}" draggable="true" data-index="${i}"
      ondragstart="dragStart(event,${i})" ondragover="dragOver(event)" ondrop="dragDrop(event,${i})" ondragleave="dragLeave(event)">
      <span class="order-rank">${i+1}</span><span class="order-name">${cap?.name||cid}</span><span style="color:var(--muted)">⠿</span>
    </li>`;
  }).join('');
}

function dragStart(e,i){dragSrcIdx=i;e.dataTransfer.effectAllowed='move';}
function dragOver(e){e.preventDefault();e.currentTarget.classList.add('drag-over');}
function dragLeave(e){e.currentTarget.classList.remove('drag-over');}
function dragDrop(e,ti){
  e.currentTarget.classList.remove('drag-over');
  if(dragSrcIdx===null||dragSrcIdx===ti) return;
  const o=[...pendingOrder];const[m]=o.splice(dragSrcIdx,1);o.splice(ti,0,m);
  pendingOrder=o;renderOrderList(o,-1);dragSrcIdx=null;
}

function renderLiveTeams(){
  const el=document.getElementById('live-teams');
  if(!captains.length){el.innerHTML='<div class="empty-state">No captains.</div>';return;}
  const pls=gameState?.players||[];
  el.innerHTML='<div class="teams-grid">'+captains.map(c=>{
    const mine=pls.filter(p=>p.taken_by===c.id);
    const displayName=c.team_name||c.name;
    return `<div class="team-col"><div class="team-header">${displayName}<span class="mono" style="font-size:.8rem;color:var(--muted);font-weight:normal">${mine.length}</span></div>
      ${mine.length?mine.map(p=>`<div class="team-player"><span>${p.name}</span><span class="tag-pos">${p.position}</span></div>`).join(''):'<div style="font-size:.8rem;color:var(--muted);padding:6px 0">No picks</div>'}
    </div>`;
  }).join('')+'</div>';
}

function renderPickHistory(hist){
  const el=document.getElementById('pick-history');
  if(!hist.length){el.innerHTML='<div class="empty-state">No picks.</div>';return;}
  const gl={g1:'≤2004',g2:'2005-2018',g3:'>2018'};
  el.innerHTML=`<table><thead><tr><th>#</th><th>Captain</th><th>Player</th><th>Pos</th><th>Year</th><th>Group</th></tr></thead>
    <tbody>${hist.map(h=>`<tr><td class="mono">${h.pick_number}</td><td><strong>${h.captain_name}</strong></td>
      <td>${h.player_name}</td><td><span class="tag-pos">${h.player_position}</span></td>
      <td class="mono">${h.player_year}</td><td><span class="badge ${GROUP_BADGE[h.group_id]}">${gl[h.group_id]||h.group_id}</span></td>
    </tr>`).join('')}</tbody></table>`;
}

// ── FIXTURES ──────────────────────────────────────────────────────────────
function renderCaptainSelects(){
  ['fixture-home','fixture-away','fe-captain'].forEach(id=>{
    const s=document.getElementById(id);
    if(s) s.innerHTML='<option value="">— Select —</option>'+captains.map(c=>`<option value="${c.id}">${c.name}</option>`).join('');
  });
}

async function addFixture(){
  const home=document.getElementById('fixture-home').value;
  const away=document.getElementById('fixture-away').value;
  const date=document.getElementById('fixture-date').value;
  const pitchName=document.getElementById('fixture-pitch-name').value.trim();
  const pitchUrl=document.getElementById('fixture-pitch-url').value.trim();
  if(!home||!away){toast('Select both teams',true);return;}
  if(home===away){toast('Teams must be different',true);return;}
  const fd=new FormData();
  fd.append('home_captain_id',home);fd.append('away_captain_id',away);fd.append('match_date',date);
  fd.append('pitch_name',pitchName);fd.append('pitch_url',pitchUrl);
  await fetch(`/api/events/${eid}/fixtures`,{method:'POST',body:fd});
  toast('Fixture added');
  document.getElementById('fixture-pitch-name').value='';
  document.getElementById('fixture-pitch-url').value='';
}

async function deleteFixture(fid){
  if(!confirm('Delete fixture?'))return;
  await fetch(`/api/events/${eid}/fixtures/${fid}`,{method:'DELETE'});
}

async function setResult(fid){
  const hs=prompt('Home score:');if(hs===null)return;
  const as_=prompt('Away score:');if(as_===null)return;
  await fetch(`/api/events/${eid}/fixtures/${fid}/result`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({home_score:parseInt(hs),away_score:parseInt(as_)})});
  toast('Result saved');
}

function openEventModal(fid){
  activeFixtureId=fid;
  renderCaptainSelects();
  renderFEPlayers();
  document.getElementById('fixture-event-modal').classList.add('open');
}
function closeEventModal(){document.getElementById('fixture-event-modal').classList.remove('open');}

function renderFEPlayers(){
  const cid=document.getElementById('fe-captain').value;
  const capPlayers=players.filter(p=>p.taken_by===cid);
  const sel=document.getElementById('fe-player');
  sel.innerHTML='<option value="">— Select player —</option>'+capPlayers.map(p=>`<option value="${p.id}|${p.name}">${p.name} (${p.position})</option>`).join('');
}

async function addFixtureEvent(){
  const cid=document.getElementById('fe-captain').value;
  const pval=document.getElementById('fe-player').value;
  const etype=document.getElementById('fe-type').value;
  if(!pval){toast('Select player',true);return;}
  const[pid,pname]=pval.split('|');
  await fetch(`/api/events/${eid}/fixtures/${activeFixtureId}/events`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({player_id:pid,player_name:pname,captain_id:cid,event_type:etype,minute:null})});
  toast(`${etype} logged`);
}

async function deleteFixtureEvent(fid,evid){
  await fetch(`/api/events/${eid}/fixtures/${fid}/events/${evid}`,{method:'DELETE'});
}

function renderFixtures(){
  const el=document.getElementById('fixtures-list');
  if(!fixtures.length){el.innerHTML='<div class="empty-state">No fixtures.</div>';return;}
  el.innerHTML=fixtures.map(f=>`<div class="card fade-in" style="margin-bottom:10px">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <div style="font-size:.75rem;color:var(--muted);min-width:80px">${f.match_date||'TBD'}</div>
      <div style="flex:1;display:flex;align-items:center;gap:10px;justify-content:center">
        <span style="font-weight:700;text-align:right;flex:1">${f.home_name}</span>
        <span style="font-family:'Bebas Neue',sans-serif;font-size:1.4rem;color:${f.status==='played'?'var(--lime)':'var(--muted)'};min-width:60px;text-align:center">
          ${f.status==='played'?`${f.home_score} — ${f.away_score}`:'vs'}
        </span>
        <span style="font-weight:700;flex:1">${f.away_name}</span>
      </div>
      <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
        ${f.pitch_name?`<a href="${f.pitch_url||'#'}" target="_blank" style="font-size:.75rem;color:var(--muted);text-decoration:none;border:1px solid var(--border);border-radius:3px;padding:3px 8px" title="${f.pitch_url||'No link'}">📍 ${f.pitch_name}</a>`:''}
        <button class="btn btn-primary btn-sm" onclick="setResult('${f.id}')">Score</button>
        <button class="btn btn-secondary btn-sm" onclick="openEventModal('${f.id}')">Events</button>
        <button class="btn btn-danger btn-sm" onclick="deleteFixture('${f.id}')">✕</button>
      </div>
    </div>
    ${f.events&&f.events.length?`<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px">
      ${f.events.map(ev=>`<div style="background:var(--pitch-mid);border:1px solid var(--border);border-radius:3px;padding:3px 8px;font-size:.75rem;display:flex;align-items:center;gap:5px">
        <span>${ev.event_type==='goal'?'⚽':ev.event_type==='assist'?'🎯':'🧤'}</span>
        <span>${ev.player_name}</span>
        ${ev.minute?`<span class="mono" style="color:var(--muted)">${ev.minute}'</span>`:''}
        <button onclick="deleteFixtureEvent('${f.id}','${ev.id}')" style="background:none;border:none;color:var(--danger);cursor:pointer;padding:0;font-size:.8rem">✕</button>
      </div>`).join('')}
    </div>`:''}
  </div>`).join('');
}

function renderStandings(){
  if(!standings) return;
  const{table,player_stats}=standings;
  const el=document.getElementById('standings-table');
  if(!table.length){el.innerHTML='<div class="empty-state">No matches played.</div>';return;}
  el.innerHTML=`<div style="overflow-x:auto"><table>
    <thead><tr><th>#</th><th>Team</th><th>P</th><th>W</th><th>D</th><th>L</th><th>GF</th><th>GA</th><th>GD</th><th>Pts</th></tr></thead>
    <tbody>${table.map((t,i)=>`<tr style="${i===0?'background:rgba(184,255,63,.05)':''}">
      <td class="mono" style="color:var(--muted)">${i+1}</td><td><strong>${t.name}</strong></td>
      <td class="mono">${t.p}</td><td class="mono">${t.w}</td><td class="mono">${t.d}</td><td class="mono">${t.l}</td>
      <td class="mono">${t.gf}</td><td class="mono">${t.ga}</td>
      <td class="mono" style="color:${t.gd>0?'var(--lime)':t.gd<0?'var(--danger)':'var(--muted)'}">${t.gd>0?'+':''}${t.gd}</td>
      <td><strong style="color:var(--lime)">${t.pts}</strong></td>
    </tr>`).join('')}</tbody></table></div>`;
  const ps=document.getElementById('player-stats-table');
  if(!player_stats.length){ps.innerHTML='<div class="empty-state">No stats yet.</div>';return;}
  ps.innerHTML=`<table><thead><tr><th>Player</th><th>⚽</th><th>🎯</th><th>🧤</th></tr></thead>
    <tbody>${player_stats.slice(0,20).map(p=>`<tr><td><strong>${p.name}</strong></td>
      <td class="mono" style="color:var(--lime)">${p.goals||0}</td>
      <td class="mono">${p.assists||0}</td><td class="mono">${p.cleansheets||0}</td>
    </tr>`).join('')}</tbody></table>`;
}

// ── TEAM RENAME ──────────────────────────────────────────────────────────
async function renameTeam(cid, currentName){
  const newName = prompt(`Team name for ${currentName}:`, currentName);
  if(!newName || newName.trim() === currentName) return;
  const res = await fetch(`/api/events/${eid}/captains/${cid}/team-name`, {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({team_name: newName.trim()})
  });
  if(res.ok){ toast(`Team renamed to "${newName.trim()}"`); loadGame(); }
  else { const d=await res.json(); toast(d.detail||'Error',true); }
}

function renderTeamRenames(){
  const phase = gameState?.phase;
  const el = document.getElementById('team-rename-section');
  if(!el) return;
  if(phase !== 'done'){ el.style.display='none'; return; }
  el.style.display='block';
  el.innerHTML = `<div class="card-title" style="margin-bottom:12px">Rename Teams</div>`
    + `<p style="font-size:.82rem;color:var(--muted);margin-bottom:14px">Draft is complete. Captains can now give their team a custom name.</p>`
    + `<div style="display:flex;flex-direction:column;gap:8px">`
    + captains.map(c => {
        const display = c.team_name || c.name;
        return `<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)">
          <div style="flex:1">
            <span style="font-weight:700;color:var(--lime)">${display}</span>
            ${c.team_name ? `<span style="font-size:.75rem;color:var(--muted);margin-left:8px">(captain: ${c.name})</span>` : ''}
          </div>
          <button class="btn btn-secondary btn-sm" onclick="renameTeam('${c.id}','${(c.team_name||c.name).replace(/'/g,"\'")}')">Rename</button>
        </div>`;
      }).join('')
    + `</div>`;
}

// ── RESTORE ───────────────────────────────────────────────────────────────
async function restoreTeams(){
  const file=document.getElementById('restore-teams-file').files[0];
  if(!file){toast('Select file',true);return;}

  // Read CSV to detect event info for confirmation
  const text=await file.text();
  const lines=text.trim().split('\n').filter(l=>l.trim());
  const total=Math.max(0,lines.length-1); // minus header

  const confirmed=confirm(
    `Ready to restore from CSV.\n\n` +
    `Detected rows: ${total}\n` +
    `Target event: ${eventData?.name||eid}\n\n` +
    `This will add missing players and reassign them to captains by name match.\nProceed?`
  );
  if(!confirmed) return;

  const fd=new FormData();fd.append('file',file);
  const res=await fetch(`/api/events/${eid}/restore/teams`,{method:'POST',body:fd});
  const data=await res.json();
  const el=document.getElementById('restore-result');
  if(res.ok){
    el.innerHTML=`<span class="text-lime">✓ ${data.added} players added, ${data.assigned} assigned.</span>`
      +(data.skipped&&data.skipped.length?`<br><span class="text-danger">Skipped: ${data.skipped.join(' · ')}</span>`:'');
    toast('Restore complete');loadPlayers();
  } else {el.innerHTML=`<span class="text-danger">${data.detail}</span>`;}
}

async function logout(){await fetch('/api/auth/logout',{method:'POST'});window.location.href='/';}
function toast(msg,e=false){const el=document.getElementById('toast');el.textContent=msg;el.className='show'+(e?' error':'');clearTimeout(toast._t);toast._t=setTimeout(()=>el.className='',2800);}

init();
