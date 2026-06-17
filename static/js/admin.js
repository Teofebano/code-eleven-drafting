const LETTERS=['A','B','C','D'];
let me=null, events=[], dbPlayers=[], questions=[];

async function init(){
  me=await fetch('/api/auth/me').then(r=>r.json());
  if(!me.role||me.role!=='admin'){window.location.href='/';return;}
  document.getElementById('admin-name').textContent=me.name;
  setupTabs();
  loadEvents(); loadQuestions(); loadPlayersDB();
}

function setupTabs(){
  document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(x=>x.classList.remove('active'));
    t.classList.add('active'); document.getElementById('tab-'+t.dataset.tab).classList.add('active');
    if(t.dataset.tab==='settings') loadAdmins();
    if(t.dataset.tab==='players-db') loadPlayersDB();
  }));
}

// ── EVENTS ────────────────────────────────────────────────────────────────
async function loadEvents(){
  events=await fetch('/api/events').then(r=>r.json());
  renderEvents();
}

function renderEvents(){
  const el=document.getElementById('events-grid');
  if(!events.length){el.innerHTML='<div class="empty-state">No events yet.</div>';return;}
  el.innerHTML=events.map(ev=>`
    <div class="event-card">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px">
        <div class="event-card-name">${ev.name}</div>
        <span class="badge ${ev.status==='done'?'badge-g2':'badge-g1'}">${ev.status}</span>
      </div>
      <div class="event-card-meta">${ev.description||''}</div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
        <span style="font-size:.72rem;color:var(--muted)">Code:</span>
        <span class="code-badge">${ev.access_code}</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <a href="/event/${ev.id}" class="btn btn-primary btn-sm">Open Event</a>
        <button class="btn btn-danger btn-sm" onclick="deleteEvent('${ev.id}')">Delete</button>
      </div>
    </div>`).join('');
}

function renderCaptainInputs(){
  const n=parseInt(document.getElementById('ev-num-teams').value)||0;
  const el=document.getElementById('captain-inputs');
  if(!n||n<1){el.innerHTML='<div style="font-size:.8rem;color:var(--muted)">Enter number of teams first</div>';return;}
  // build datalist from player DB
  const datalistId='cap-name-suggestions';
  let datalistHtml=`<datalist id="${datalistId}">${dbPlayers.map(p=>`<option value="${p.name}">`).join('')}</datalist>`;
  el.innerHTML=datalistHtml+Array.from({length:n},(_,i)=>`
    <div class="captain-input-row">
      <span style="font-family:'JetBrains Mono',monospace;font-size:.8rem;color:var(--muted);width:20px">${i+1}</span>
      <input class="cap-name-input" placeholder="Captain ${i+1} name" list="${datalistId}" autocomplete="off"/>
    </div>`).join('');
}

async function submitCreateEvent(){
  const name=document.getElementById('ev-name').value.trim();
  const desc=document.getElementById('ev-desc').value.trim();
  const capPw=document.getElementById('ev-cap-pw').value.trim();
  const num=parseInt(document.getElementById('ev-num-teams').value)||0;
  const names=Array.from(document.querySelectorAll('.cap-name-input')).map(i=>i.value.trim());

  if(!name){alert('Enter event name');return;}
  if(!capPw){alert('Enter captain password');return;}
  if(!num||num<1){alert('Enter number of teams');return;}

  const btn=document.querySelector('[onclick="submitCreateEvent()"]');
  if(btn){btn.disabled=true;btn.textContent='Creating…';}

  try {
    const fd=new FormData();
    fd.append('name',name); fd.append('description',desc);
    fd.append('captain_password',capPw); fd.append('num_teams',num);
    fd.append('captain_names',JSON.stringify(names));
    const res=await fetch('/api/events',{method:'POST',body:fd});
    const data=await res.json();
    if(res.ok){
      alert(`Event "${name}" created!\nAccess code: ${data.access_code}`);
      window.location.href=`/event/${data.id}`;
    } else {
      alert('Error: '+(data.detail||JSON.stringify(data)));
    }
  } catch(e) {
    alert('Network error: '+e.message);
  } finally {
    if(btn){btn.disabled=false;btn.textContent='Create Event';}
  }
}

async function deleteEvent(eid){
  if(!confirm('Delete this event and all its data?')) return;
  await fetch(`/api/events/${eid}`,{method:'DELETE'}); toast('Deleted'); loadEvents();
}

// ── EDIT MODAL ────────────────────────────────────────────────────────────
let editEid=null;
async function openEditModal(eid){
  editEid=eid;
  const ev=await fetch(`/api/events/${eid}`).then(r=>r.json());
  document.getElementById('edit-ev-id').value=eid;
  document.getElementById('edit-ev-name').value=ev.name;
  document.getElementById('edit-ev-desc').value=ev.description||'';
  document.getElementById('edit-ev-cap-pw').value='';
  renderEditCaptains(ev.captains||[]);
  document.getElementById('edit-event-modal').classList.add('open');
}

function renderEditCaptains(caps){
  document.getElementById('edit-captains-list').innerHTML=caps.map(c=>`
    <div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">
      <span style="font-family:'JetBrains Mono',monospace;font-size:.8rem;color:var(--muted);width:20px">${c.team_number}</span>
      <input value="${c.name}" id="edit-cap-${c.id}" style="flex:1;padding:6px 10px;font-size:.85rem"/>
      <button class="btn btn-secondary btn-sm" onclick="saveCaptainName('${editEid}','${c.id}')">Save</button>
      <button class="btn btn-danger btn-sm" onclick="deleteCaptainFromEvent('${editEid}','${c.id}')">✕</button>
    </div>`).join('');
}

async function saveCaptainName(eid,cid){
  const name=document.getElementById(`edit-cap-${cid}`).value.trim();
  if(!name) return;
  const fd=new FormData(); fd.append('name',name);
  await fetch(`/api/events/${eid}/captains/${cid}`,{method:'PUT',body:fd});
  toast('Captain updated');
}

async function deleteCaptainFromEvent(eid,cid){
  if(!confirm('Remove captain?')) return;
  await fetch(`/api/events/${eid}/captains/${cid}`,{method:'DELETE'});
  const ev=await fetch(`/api/events/${eid}`).then(r=>r.json());
  renderEditCaptains(ev.captains||[]); toast('Captain removed');
}

async function addCaptainToEvent(){
  const name=document.getElementById('new-cap-name').value.trim();
  if(!name||!editEid) return;
  const fd=new FormData(); fd.append('name',name);
  await fetch(`/api/events/${editEid}/captains`,{method:'POST',body:fd});
  document.getElementById('new-cap-name').value='';
  const ev=await fetch(`/api/events/${editEid}`).then(r=>r.json());
  renderEditCaptains(ev.captains||[]); toast(`${name} added`);
}

async function saveEventEdit(){
  const name=document.getElementById('edit-ev-name').value.trim();
  const desc=document.getElementById('edit-ev-desc').value.trim();
  const capPw=document.getElementById('edit-ev-cap-pw').value;
  if(!name){toast('Name required',true);return;}
  const fd=new FormData(); fd.append('name',name); fd.append('description',desc);
  if(capPw) fd.append('captain_password',capPw);
  await fetch(`/api/events/${editEid}`,{method:'PUT',body:fd});
  toast('Event saved'); closeEditModal(); loadEvents();
}

function closeEditModal(){document.getElementById('edit-event-modal').classList.remove('open');}

// ── PLAYER DB ─────────────────────────────────────────────────────────────
async function loadPlayersDB(q=''){
  dbPlayers=await fetch(q?`/api/players-db?q=${encodeURIComponent(q)}`:'/api/players-db').then(r=>r.json());
  renderPlayersDB();
}

let editingDbId=null;
function renderPlayersDB(){
  const el=document.getElementById('players-db-table');
  const cnt=document.getElementById('db-count');
  if(cnt) cnt.textContent=`Player Registry (${dbPlayers.length})`;
  if(!dbPlayers.length){el.innerHTML='<div class="empty-state">No players.</div>';return;}
  el.innerHTML=`<table><thead><tr><th>Name</th><th>Pos</th><th>Year</th><th>City</th><th></th></tr></thead>
    <tbody>${dbPlayers.map(p=>{
      if(p.id===editingDbId) return `<tr class="edit-row" id="edit-row-${p.id}">
        <td><input id="e-name-${p.id}" value="${p.name}" style="width:100%"/></td>
        <td><select id="e-pos-${p.id}">
          ${['GK','DEF','MID','ATK'].map(x=>`<option value="${x}" ${p.position===x?'selected':''}>${x}</option>`).join('')}
        </select></td>
        <td><input type="number" id="e-year-${p.id}" value="${p.batch_year}" style="width:80px"/></td>
        <td><input id="e-city-${p.id}" value="${p.city||''}" style="width:100%"/></td>
        <td style="display:flex;gap:6px">
          <button class="btn btn-primary btn-sm" onclick="savePlayerDB('${p.id}')">Save</button>
          <button class="btn btn-secondary btn-sm" onclick="cancelEditDB()">Cancel</button>
        </td>
      </tr>`;
      return `<tr>
        <td><strong>${p.name}</strong></td>
        <td><span class="tag-pos">${p.position}</span></td>
        <td class="mono">${p.batch_year}</td>
        <td style="color:var(--muted);font-size:.85rem">${p.city||'—'}</td>
        <td style="display:flex;gap:6px">
          <button class="btn btn-secondary btn-sm" onclick="startEditDB('${p.id}')">Edit</button>
          <button class="btn btn-danger btn-sm" onclick="deletePlayerDB('${p.id}')">✕</button>
        </td>
      </tr>`;
    }).join('')}</tbody></table>`;
}

function startEditDB(id){editingDbId=id;renderPlayersDB();}
function cancelEditDB(){editingDbId=null;renderPlayersDB();}

async function savePlayerDB(id){
  const name=document.getElementById(`e-name-${id}`).value.trim();
  const pos=document.getElementById(`e-pos-${id}`).value;
  const year=parseInt(document.getElementById(`e-year-${id}`).value);
  const city=document.getElementById(`e-city-${id}`).value.trim();
  if(!name||!year){toast('Fill name and year',true);return;}
  const fd=new FormData(); fd.append('name',name);fd.append('position',pos);fd.append('batch_year',year);fd.append('city',city);
  await fetch(`/api/players-db/${id}`,{method:'PUT',body:fd});
  editingDbId=null; toast('Player updated'); loadPlayersDB();
}

async function addPlayerDB(){
  const name=document.getElementById('db-name').value.trim();
  const pos=document.getElementById('db-pos').value;
  const year=parseInt(document.getElementById('db-year').value);
  const city=document.getElementById('db-city').value.trim();
  if(!name||!year){toast('Fill name and year',true);return;}
  const fd=new FormData(); fd.append('name',name);fd.append('position',pos);fd.append('batch_year',year);fd.append('city',city);
  const res=await fetch('/api/players-db',{method:'POST',body:fd});
  if(!res.ok){const d=await res.json();toast(d.detail,true);return;}
  document.getElementById('db-name').value='';document.getElementById('db-year').value='';document.getElementById('db-city').value='';
  toast('Player added'); loadPlayersDB();
}

async function deletePlayerDB(id){
  if(!confirm('Remove from registry?')) return;
  await fetch(`/api/players-db/${id}`,{method:'DELETE'}); loadPlayersDB();
}

async function uploadDBCSV(){
  const file=document.getElementById('db-csv-file').files[0];
  if(!file){toast('Select CSV',true);return;}
  const fd=new FormData(); fd.append('file',file);
  const res=await fetch('/api/players-db/csv',{method:'POST',body:fd});
  const data=await res.json();
  const el=document.getElementById('db-csv-result');
  if(res.ok){el.innerHTML=`<span class="text-lime">✓ Added ${data.added}, updated ${data.updated}.</span>`;toast('Import done');loadPlayersDB();}
  else{el.innerHTML=`<span class="text-danger">${data.detail}</span>`;}
}

// ── QUESTIONS ─────────────────────────────────────────────────────────────
async function loadQuestions(){
  questions=await fetch('/api/questions').then(r=>r.json());
  renderQuestions();
}
async function addQuestion(){
  const text=document.getElementById('q-text').value.trim();
  const opts=Array.from(document.querySelectorAll('.q-opt')).map(i=>i.value.trim());
  const correct=parseInt(document.getElementById('q-correct').value);
  if(!text||opts.some(o=>!o)){toast('Fill all fields',true);return;}
  const fd=new FormData();fd.append('text',text);fd.append('option_a',opts[0]);fd.append('option_b',opts[1]);fd.append('option_c',opts[2]);fd.append('option_d',opts[3]);fd.append('correct_index',correct);
  await fetch('/api/questions',{method:'POST',body:fd});
  document.getElementById('q-text').value='';document.querySelectorAll('.q-opt').forEach(i=>i.value='');
  toast('Question added');loadQuestions();
}
async function deleteQuestion(id){await fetch(`/api/questions/${id}`,{method:'DELETE'});loadQuestions();}
function renderQuestions(){
  const el=document.getElementById('questions-list');
  if(!questions.length){el.innerHTML='<div class="empty-state">No questions.</div>';return;}
  el.innerHTML=questions.map((q,i)=>`<div class="card fade-in" style="margin-bottom:12px">
    <div style="display:flex;justify-content:space-between;gap:12px">
      <div style="flex:1">
        <div class="mono" style="font-size:.7rem;color:var(--muted);margin-bottom:6px">Q${i+1}</div>
        <div style="font-weight:600;margin-bottom:8px">${q.text}</div>
        ${[q.option_a,q.option_b,q.option_c,q.option_d].map((o,oi)=>`<div style="font-size:.82rem;color:${oi===q.correct_index?'var(--lime)':'var(--muted)'}"><strong>${LETTERS[oi]}.</strong> ${o} ${oi===q.correct_index?'✓':''}</div>`).join('')}
      </div>
      <button class="btn btn-danger btn-sm" onclick="deleteQuestion('${q.id}')">✕</button>
    </div></div>`).join('');
}

// ── ADMINS ────────────────────────────────────────────────────────────────
async function loadAdmins(){
  const admins=await fetch('/api/admins').then(r=>r.json());
  document.getElementById('admins-table').innerHTML=`<table><thead><tr><th>Username</th><th>Created</th><th></th></tr></thead>
    <tbody>${admins.map(a=>`<tr><td><strong>${a.username}</strong></td><td class="mono" style="font-size:.75rem;color:var(--muted)">${(a.created_at||'').split('T')[0]||'—'}</td>
    <td><button class="btn btn-danger btn-sm" onclick="deleteAdmin('${a.id}')">✕</button></td></tr>`).join('')}</tbody></table>`;
}
async function addAdmin(){
  const u=document.getElementById('new-admin-user').value.trim(),p=document.getElementById('new-admin-pw').value;
  if(!u||!p){toast('Fill all fields',true);return;}
  const fd=new FormData();fd.append('username',u);fd.append('password',p);
  const res=await fetch('/api/admins',{method:'POST',body:fd});
  if(res.ok){toast('Admin added');loadAdmins();}else{const d=await res.json();toast(d.detail,true);}
}
async function deleteAdmin(id){if(!confirm('Remove?'))return;await fetch(`/api/admins/${id}`,{method:'DELETE'});toast('Removed');loadAdmins();}

async function logout(){await fetch('/api/auth/logout',{method:'POST'});window.location.href='/';}
function toast(msg,e=false){const el=document.getElementById('toast');el.textContent=msg;el.className='show'+(e?' error':'');clearTimeout(toast._t);toast._t=setTimeout(()=>el.className='',2800);}

init();
