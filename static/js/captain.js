const LETTERS = ['A','B','C','D'];
const GROUP_LABELS = {g1:'≤ 2004',g2:'2005 – 2018',g3:'> 2018'};
const POS_ORDER = {GK:0,DEF:1,MID:2,ATK:3};
const POS_CLASS  = {GK:'pos-gk',DEF:'pos-def',MID:'pos-mid',ATK:'pos-atk'};
const MCQ_SECONDS = 15;

let me=null, gameState=null, ws=null;
let timerInterval=null, timerEnd=null, hasAnswered=false, soundCtx=null;
let currentEventId=null, currentSort='year';
let events=[];

// ── SOUND ──────────────────────────────────────────────────────────────────
function initSound(){soundCtx=new(window.AudioContext||window.webkitAudioContext)();}
function playTone(f,t,d,v=.3){
  if(!soundCtx)return;
  const o=soundCtx.createOscillator(),g=soundCtx.createGain();
  o.connect(g);g.connect(soundCtx.destination);
  o.type=t;o.frequency.value=f;
  g.gain.setValueAtTime(v,soundCtx.currentTime);
  g.gain.exponentialRampToValueAtTime(.001,soundCtx.currentTime+d);
  o.start();o.stop(soundCtx.currentTime+d);
}
function soundReveal(){[0,80,160].forEach(d=>setTimeout(()=>playTone(180,'sawtooth',.12,.4),d));setTimeout(()=>playTone(440,'square',.3,.5),200);setTimeout(()=>playTone(660,'square',.25,.4),350);}
function soundCorrect(){playTone(523,'sine',.15,.4);setTimeout(()=>playTone(659,'sine',.15,.15),120);setTimeout(()=>playTone(784,'sine',.2,.35),230);setTimeout(()=>playTone(1047,'sine',.25,.4),330);}
function soundWrong(){playTone(220,'sawtooth',.2,.4);setTimeout(()=>playTone(180,'sawtooth',.2,.3),150);}
function soundPick(){playTone(440,'sine',.1,.3);setTimeout(()=>playTone(660,'sine',.12,.25),100);}
function soundUrgent(){playTone(880,'square',.05,.15);}

// ── INIT ───────────────────────────────────────────────────────────────────
async function init() {
  document.addEventListener('click',()=>{if(!soundCtx)initSound();},{once:true});
  me = await fetch('/api/auth/me').then(r=>r.json());
  if (!me.role||!['captain','admin'].includes(me.role)){window.location.href='/';return;}
  document.getElementById('captain-name-header').textContent=me.name;
  events = await fetch('/api/events').then(r=>r.json());
  if (!events.length){
    document.getElementById('main-content').innerHTML='<div class="waiting"><div class="waiting-title">No events yet</div><div class="waiting-sub">Ask admin to create an event.</div></div>';
    return;
  }
  // show event picker if multiple events
  if (events.length===1) {
    selectEvent(events[0].id);
  } else {
    renderEventPicker();
  }
}

function renderEventPicker() {
  const el=document.getElementById('main-content');
  el.innerHTML=`<div class="fade-in" style="max-width:500px;margin:0 auto;padding-top:32px">
    <div class="section-header">Select Event</div>
    <p class="section-sub">Choose which event you're drafting for.</p>
    <div style="display:flex;flex-direction:column;gap:10px">
      ${events.map(ev=>`
        <div onclick="selectEvent('${ev.id}')" style="background:var(--card);border:1.5px solid var(--border);border-radius:6px;padding:16px 20px;cursor:pointer;transition:all .15s"
          onmouseover="this.style.borderColor='var(--lime)'" onmouseout="this.style.borderColor='var(--border)'">
          <div style="font-family:'Bebas Neue',sans-serif;font-size:1.2rem;letter-spacing:1px;color:var(--lime)">${ev.name}</div>
          <div style="font-size:.82rem;color:var(--muted);margin-top:4px">${ev.description||''} · ${ev.status}</div>
        </div>`).join('')}
    </div>
  </div>`;
}

function selectEvent(eid) {
  currentEventId=eid;
  connectWS();
  loadGame();
}

// ── WEBSOCKET ──────────────────────────────────────────────────────────────
function connectWS() {
  const proto=location.protocol==='https:'?'wss':'ws';
  ws=new WebSocket(`${proto}://${location.host}/ws/draft?room=event:${currentEventId}`);
  ws.onmessage=e=>{
    const msg=JSON.parse(e.data);
    if(msg.type==='round_started'){hasAnswered=false;loadGame();startTimer(MCQ_SECONDS);soundReveal();}
    else if(msg.type==='captain_answered'){loadGame();}
    else if(msg.type==='phase_change'){loadGame();if(msg.phase==='draft')soundPick();}
    else if(msg.type==='player_picked'){loadGame();if(msg.captain_id!==me.sub)soundPick();}
    else if(msg.type==='reset'){hasAnswered=false;loadGame();}
    else if(msg.type==='players_updated'){loadGame();}
  };
  ws.onclose=()=>setTimeout(()=>{if(currentEventId)connectWS();},2000);
  setInterval(()=>{if(ws&&ws.readyState===1)ws.send('ping');},20000);
}

// ── DATA ───────────────────────────────────────────────────────────────────
async function loadGame() {
  if (!currentEventId) return;
  gameState=await fetch(`/api/events/${currentEventId}/game`).then(r=>r.json());
  render();
}

// ── TIMER ──────────────────────────────────────────────────────────────────
function startTimer(s) {
  clearInterval(timerInterval); timerEnd=Date.now()+s*1000;
  let lastUrgent=false;
  timerInterval=setInterval(()=>{
    const left=Math.max(0,Math.ceil((timerEnd-Date.now())/1000));
    const el=document.getElementById('mcq-timer');
    if(el){el.textContent=left+'s';const u=left<=5;el.className='mcq-timer'+(u?' urgent':'');if(u&&!lastUrgent){soundUrgent();lastUrgent=true;}}
    if(left<=0)clearInterval(timerInterval);
  },250);
}

// ── RENDER ─────────────────────────────────────────────────────────────────
function render() {
  if(!gameState)return;
  const el=document.getElementById('main-content');
  const {phase}=gameState;
  if(!phase||phase==='lobby') el.innerHTML=lobbyHTML();
  else if(phase==='mcq')     el.innerHTML=mcqHTML();
  else if(phase==='draft')   el.innerHTML=draftHTML();
  else if(phase==='done')    el.innerHTML=doneHTML();
}

function lobbyHTML(){
  return `<div class="waiting fade-in">
    <div class="waiting-icon">⚽</div>
    <div class="waiting-title">${gameState.event?.name||'Draft Room'}</div>
    <div class="waiting-sub"><span class="pulse-dot">●</span> Waiting for admin to start the first round…</div>
    <div style="margin-top:32px">${teamsHTML()}</div>
  </div>`;
}

function mcqHTML() {
  const {question,answers,group_index,current_group}=gameState;
  const myAns=(answers||[]).find(a=>a.captain_id===me.sub);
  const groupLabel=GROUP_LABELS[current_group]||'';
  if (myAns) return answeredHTML(myAns);
  if (!question) return '<div class="waiting"><div class="waiting-title">Loading…</div></div>';
  return `<div class="mcq-container fade-in">
    <div class="round-label">Round ${group_index+1} of 3 — ${groupLabel}</div>
    <div class="mcq-timer" id="mcq-timer">${MCQ_SECONDS}s</div>
    <div class="question-text">${question.text}</div>
    <div class="mcq-options">${question.options.map((o,i)=>`
      <button class="mcq-btn" id="opt-${i}" onclick="submitAnswer(${i})">
        <span class="opt-letter">${LETTERS[i]}</span>${o}
      </button>`).join('')}
    </div>
  </div>`;
}

function answeredHTML(myAns) {
  const {answers,captains,question,group_index,current_group}=gameState;
  const sorted=[...(answers||[])].sort((a,b)=>a.answered_at_ms-b.answered_at_ms);
  const correct=sorted.filter(a=>a.is_correct);
  const myRank=myAns.is_correct?correct.findIndex(a=>a.captain_id===me.sub)+1:null;
  return `<div class="mcq-container fade-in">
    <div class="round-label">Round ${group_index+1} of 3 — ${GROUP_LABELS[current_group]||''}</div>
    <div style="font-size:1.3rem;font-weight:700;margin:20px 0 8px;color:${myAns.is_correct?'var(--lime)':'var(--danger)'}">
      ${myAns.is_correct?`✓ Correct! You ranked #${myRank}`:'✗ Wrong — no pick slot this round'}
    </div>
    ${question?`<div style="font-size:.88rem;color:var(--muted);margin-bottom:20px">
      You answered: <strong style="color:var(--white)">${myAns.chosen_index!==null?LETTERS[myAns.chosen_index]:'—'}</strong>
      ${!myAns.is_correct&&question.correct_index!==undefined?` · Correct: <strong style="color:var(--lime)">${LETTERS[question.correct_index]}</strong>`:''}
    </div>`:''}
    <div class="card-title">Leaderboard</div>
    ${sorted.map(a=>{
      const cap=(captains||[]).find(c=>c.id===a.captain_id);
      const rank=a.is_correct?correct.findIndex(c=>c.captain_id===a.captain_id)+1:null;
      return `<div style="display:flex;align-items:center;gap:10px;padding:10px;border-bottom:1px solid var(--border)">
        <div style="font-family:'Bebas Neue',sans-serif;font-size:1.3rem;width:26px;color:${a.is_correct?'var(--lime)':'var(--danger)'}">
          ${rank||'✗'}
        </div>
        <div style="font-weight:700">${cap?.name||a.captain_id} ${a.captain_id===me.sub?'<span style="color:var(--lime)">(you)</span>':''}</div>
        <div style="margin-left:auto;font-size:.75rem;color:var(--muted)">${new Date(a.answered_at_ms).toLocaleTimeString()}</div>
      </div>`;
    }).join('')}
    <div style="margin-top:16px;font-size:.83rem;color:var(--muted);text-align:center">
      <span class="pulse-dot">●</span> Waiting…
    </div>
  </div>`;
}

function draftHTML() {
  const {draft_order,current_picker_index,players,captains,current_group,group_index}=gameState;
  const order=draft_order||[]; const pidx=current_picker_index??0;
  const currentPicker=order[pidx]; const isMyTurn=currentPicker===me.sub;
  const groupLabel=GROUP_LABELS[current_group]||'';
  const gPlayers=(players||[]).filter(p=>getGroup(p.batch_year)===current_group).sort((a,b)=>a.name.localeCompare(b.name));
  const allPicked=gPlayers.every(p=>p.taken_by);

  let html=`<div class="fade-in">
    <div class="draft-header"><span>Draft · <span class="dh-group">${groupLabel}</span></span>
      <span class="mono" style="font-size:.8rem;color:var(--muted)">Round ${(group_index||0)+1}/3</span></div>
    <div class="order-chips">${order.map((cid,i)=>{
      const cap=(captains||[]).find(c=>c.id===cid);
      return `<div class="order-chip ${i===pidx&&!allPicked?'active-pick':''}"><span class="chip-rank">${i+1}</span>${cap?.name||cid}</div>`;
    }).join('')}</div>`;

  if(allPicked) html+=`<div class="waiting-banner">All players in this group picked ✓ — waiting for admin to start next round.</div>`;
  else if(isMyTurn) html+=`<div class="my-turn-banner">🟢 YOUR PICK — Select a player below</div>`;
  else {
    const pn=(captains||[]).find(c=>c.id===currentPicker)?.name||currentPicker;
    html+=`<div class="waiting-banner"><span class="pulse-dot">●</span> Waiting for <strong>${pn}</strong> to pick…</div>`;
  }

  html+=`<div class="player-grid">${gPlayers.map(p=>{
    const taken=!!p.taken_by;
    return `<div class="player-card ${taken?'taken':isMyTurn?'pickable':''}" ${!taken&&isMyTurn?`onclick="pickPlayer('${p.id}')"`:''}> 
      <div class="pc-name">${p.name}</div>
      <div class="pc-meta">${p.position} · ${p.batch_year}</div>
      ${taken?`<div class="pc-owner">→ ${p.captain_name||p.taken_by}</div>`:''}
    </div>`;
  }).join('')}</div>
  <div class="divider"></div>
  <div class="card-title">Live Teams</div>${teamsHTML()}
  </div>`;
  return html;
}

function doneHTML(){
  return `<div class="fade-in">
    <div class="results-header">Draft Complete 🏆</div>
    <p style="text-align:center;color:var(--muted);margin-bottom:28px">Final squads.</p>
    ${teamsHTML()}
    <div style="text-align:center;margin-top:28px">
      <button class="btn btn-secondary" onclick="logout()">Exit</button>
    </div>
  </div>`;
}

// ── SORT + TEAMS ───────────────────────────────────────────────────────────
function posClass(pos){return ({GK:'pos-gk',DEF:'pos-def',MID:'pos-mid',ATK:'pos-atk'})[pos?.toUpperCase()]||'pos-unknown';}
function getGroup(y){return y<=2004?'g1':y<=2018?'g2':'g3';}

function sortPlayers(pls) {
  const s=[...pls];
  if(currentSort==='position'){const o={GK:0,DEF:1,MID:2,ATK:3};s.sort((a,b)=>{const pa=o[a.position?.toUpperCase()]??99,pb=o[b.position?.toUpperCase()]??99;return pa!==pb?pa-pb:a.name.localeCompare(b.name);});}
  else if(currentSort==='name') s.sort((a,b)=>a.name.localeCompare(b.name));
  else s.sort((a,b)=>a.batch_year-b.batch_year);
  return s;
}

function sortBar(){
  const labels={year:'Batch Year',position:'Position',name:'Name'};
  return '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:16px;background:var(--card);border:1px solid var(--border);border-radius:5px;padding:10px 14px">'
    +'<span style="font-size:.72rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Sort:</span>'
    +Object.keys(labels).map(s=>`<button onclick="reSort('${s}')" style="font-size:.78rem;font-weight:600;padding:5px 12px;border-radius:3px;cursor:pointer;text-transform:uppercase;letter-spacing:.4px;border:1.5px solid ${currentSort===s?'var(--lime)':'var(--border)'};background:${currentSort===s?'rgba(184,255,63,0.07)':'transparent'};color:${currentSort===s?'var(--lime)':'var(--muted)'}">${labels[s]}</button>`).join('')
    +'</div>';
}

function reSort(s){currentSort=s;const w=document.getElementById('teams-sort-wrap');if(w)w.innerHTML=sortBar()+teamsInnerHTML();}

function teamsHTML(){return '<div id="teams-sort-wrap">'+sortBar()+teamsInnerHTML()+'</div>';}

function teamsInnerHTML(){
  const {captains,players}=gameState||{};
  if(!captains||!captains.length) return '';
  return '<div class="teams-grid">'+captains.map(c=>{
    const mine=sortPlayers((players||[]).filter(p=>p.taken_by===c.id));
    return '<div class="team-col">'
      +'<div class="team-header">'+c.name+'<span class="mono" style="font-size:.78rem;color:var(--muted);font-weight:normal">'+mine.length+'</span></div>'
      +(mine.length?mine.map(p=>'<div class="team-player"><span>'+p.name+'</span>'
        +'<div style="display:flex;gap:6px;align-items:center">'
        +'<span class="pos-badge '+posClass(p.position)+'">'+( p.position||'?')+'</span>'
        +'<span class="year-tag">'+p.batch_year+'</span>'
        +'</div></div>').join('')
        :'<div style="font-size:.8rem;color:var(--muted);padding:6px 0">No picks</div>')
      +'</div>';
  }).join('')+'</div>';
}

// ── ACTIONS ────────────────────────────────────────────────────────────────
async function submitAnswer(chosenIndex) {
  if(hasAnswered) return; hasAnswered=true;
  if(!soundCtx) initSound();
  document.querySelectorAll('.mcq-btn').forEach(b=>b.disabled=true);
  const res=await fetch(`/api/events/${currentEventId}/game/answer`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chosen_index:chosenIndex})});
  if(res.ok){
    const d=await res.json();
    const btn=document.getElementById(`opt-${chosenIndex}`);
    if(btn) btn.classList.add(d.is_correct?'correct':'wrong');
    if(d.is_correct) soundCorrect(); else soundWrong();
    await loadGame();
  } else { hasAnswered=false; document.querySelectorAll('.mcq-btn').forEach(b=>b.disabled=false); }
}

async function pickPlayer(pid) {
  soundPick();
  const res=await fetch(`/api/events/${currentEventId}/game/pick`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({player_id:pid})});
  if(!res.ok){const d=await res.json();toast(d.detail||'Pick failed',true);}
}

async function logout(){await fetch('/api/auth/logout',{method:'POST'});window.location.href='/';}

function toast(msg,isError=false){
  const el=document.getElementById('toast');el.textContent=msg;
  el.className='show'+(isError?' error':'');
  clearTimeout(toast._t);toast._t=setTimeout(()=>el.className='',2800);
}

init();
