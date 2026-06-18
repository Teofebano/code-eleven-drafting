const LETTERS=['A','B','C','D'];
const GROUP_LABELS={g1:'≤ 2004',g2:'2005 – 2018',g3:'> 2018'};
const POS_CLASS={GK:'pos-gk',DEF:'pos-def',MID:'pos-mid',ATK:'pos-atk'};
const MCQ_SECONDS=15;

let me=null,gameState=null,ws=null,eid=null;
let timerInterval=null,timerEnd=null,hasAnswered=false,soundCtx=null,currentSort='year';

function initSound(){soundCtx=new(window.AudioContext||window.webkitAudioContext)();}
function playTone(f,t,d,v=.3){if(!soundCtx)return;const o=soundCtx.createOscillator(),g=soundCtx.createGain();o.connect(g);g.connect(soundCtx.destination);o.type=t;o.frequency.value=f;g.gain.setValueAtTime(v,soundCtx.currentTime);g.gain.exponentialRampToValueAtTime(.001,soundCtx.currentTime+d);o.start();o.stop(soundCtx.currentTime+d);}
function soundReveal(){[0,80,160].forEach(d=>setTimeout(()=>playTone(180,'sawtooth',.12,.4),d));setTimeout(()=>playTone(440,'square',.3,.5),200);setTimeout(()=>playTone(660,'square',.25,.4),350);}
function soundCorrect(){playTone(523,'sine',.15,.4);setTimeout(()=>playTone(659,'sine',.15,.15),120);setTimeout(()=>playTone(784,'sine',.2,.35),230);setTimeout(()=>playTone(1047,'sine',.25,.4),330);}
function soundWrong(){playTone(220,'sawtooth',.2,.4);setTimeout(()=>playTone(180,'sawtooth',.2,.3),150);}
function soundPick(){playTone(440,'sine',.1,.3);setTimeout(()=>playTone(660,'sine',.12,.25),100);}
function soundUrgent(){playTone(880,'square',.05,.15);}

async function init(){
  document.addEventListener('click',()=>{if(!soundCtx)initSound();},{once:true});
  me=await fetch('/api/auth/me').then(r=>r.json());
  if(!me.role||!['captain','admin'].includes(me.role)){window.location.href='/';return;}
  eid=me.event_id||new URLSearchParams(location.search).get('event');
  if(!eid){window.location.href='/';return;}
  document.getElementById('cap-name-h').textContent=me.name;
  connectWS(); loadGame();
}

function connectWS(){
  const proto=location.protocol==='https:'?'wss':'ws';
  ws=new WebSocket(`${proto}://${location.host}/ws/draft?room=event:${eid}`);
  ws.onmessage=e=>{
    const msg=JSON.parse(e.data);
    if(msg.type==='round_started'){hasAnswered=false;loadGame();startTimer(MCQ_SECONDS);soundReveal();}
    else if(msg.type==='captain_answered') loadGame();
    else if(msg.type==='phase_change'){loadGame();if(msg.phase==='draft')soundPick();}
    else if(msg.type==='player_picked'){loadGame();if(msg.captain_id!==me.sub)soundPick();}
    else if(['reset','players_updated'].includes(msg.type)){hasAnswered=false;loadGame();}
  };
  ws.onclose=()=>setTimeout(connectWS,2000);
  setInterval(()=>{if(ws&&ws.readyState===1)ws.send('ping');},20000);
}

async function loadGame(){
  gameState=await fetch(`/api/events/${eid}/game`).then(r=>r.json());
  render();
}

function startTimer(s){
  clearInterval(timerInterval);timerEnd=Date.now()+s*1000;let lastU=false;
  timerInterval=setInterval(()=>{
    const left=Math.max(0,Math.ceil((timerEnd-Date.now())/1000));
    const el=document.getElementById('mcq-timer');
    if(el){el.textContent=left+'s';const u=left<=5;el.className='mcq-timer'+(u?' urgent':'');if(u&&!lastU){soundUrgent();lastU=true;}}
    if(left<=0)clearInterval(timerInterval);
  },250);
}

function render(){
  if(!gameState)return;
  const el=document.getElementById('main-content');
  const{phase}=gameState;
  if(!phase||phase==='lobby') el.innerHTML=lobbyHTML();
  else if(phase==='mcq')     el.innerHTML=mcqHTML();
  else if(phase==='draft')   el.innerHTML=draftHTML();
  else if(phase==='done')    el.innerHTML=doneHTML();
}

function lobbyHTML(){
  return `<div class="waiting fade-in">
    <div style="font-size:2rem;margin-bottom:12px">⚽</div>
    <div class="waiting-title">${gameState.event?.name||'Draft Room'}</div>
    <div class="waiting-sub"><span class="pulse-dot">●</span> Waiting for admin to start the first round…</div>
    <div style="margin-top:32px">${teamsHTML()}</div>
  </div>`;
}

function mcqHTML(){
  const{question,answers,group_index,current_group}=gameState;
  const myAns=(answers||[]).find(a=>a.captain_id===me.sub);
  if(myAns) return answeredHTML(myAns);
  if(!question) return '<div class="waiting"><div class="waiting-title">Loading…</div></div>';
  return `<div class="mcq-container fade-in">
    <div class="round-label">Round ${group_index+1} of 3 — ${GROUP_LABELS[current_group]||''}</div>
    <div class="mcq-timer" id="mcq-timer">${MCQ_SECONDS}s</div>
    <div class="question-text">${question.text}</div>
    <div class="mcq-options">${question.options.map((o,i)=>`
      <button class="mcq-btn" id="opt-${i}" onclick="submitAnswer(${i})">
        <span class="opt-letter">${LETTERS[i]}</span>${o}
      </button>`).join('')}</div>
  </div>`;
}

function answeredHTML(myAns){
  const{answers,captains,question,group_index,current_group}=gameState;
  const sorted=[...(answers||[])].sort((a,b)=>a.answered_at_ms-b.answered_at_ms);
  const correct=sorted.filter(a=>a.is_correct);
  const myRank=myAns.is_correct?correct.findIndex(a=>a.captain_id===me.sub)+1:null;
  return `<div class="mcq-container fade-in">
    <div class="round-label">Round ${group_index+1} of 3 — ${GROUP_LABELS[current_group]||''}</div>
    <div style="font-size:1.3rem;font-weight:700;margin:20px 0 8px;color:${myAns.is_correct?'var(--lime)':'var(--danger)'}">
      ${myAns.is_correct?`✓ Correct! You ranked #${myRank}`:'✗ Wrong — no priority pick this round'}
    </div>
    ${question?`<div style="font-size:.88rem;color:var(--muted);margin-bottom:20px">You answered: <strong style="color:var(--white)">${myAns.chosen_index!==null?LETTERS[myAns.chosen_index]:'—'}</strong>${!myAns.is_correct&&question.correct_index!==undefined?` · Correct: <strong style="color:var(--lime)">${LETTERS[question.correct_index]}</strong>`:''}</div>`:''}
    <div class="card-title">Leaderboard</div>
    ${sorted.map(a=>{
      const cap=(captains||[]).find(c=>c.id===a.captain_id);
      const rank=a.is_correct?correct.findIndex(c=>c.captain_id===a.captain_id)+1:null;
      return `<div style="display:flex;align-items:center;gap:10px;padding:10px;border-bottom:1px solid var(--border)">
        <div style="font-family:'Bebas Neue',sans-serif;font-size:1.3rem;width:26px;color:${a.is_correct?'var(--lime)':'var(--danger)'}">${rank||'✗'}</div>
        <div style="font-weight:700">${cap?.name||a.captain_id} ${a.captain_id===me.sub?'<span style="color:var(--lime)">(you)</span>':''}</div>
        <div style="margin-left:auto;font-size:.75rem;color:var(--muted)">${new Date(a.answered_at_ms).toLocaleTimeString()}</div>
      </div>`;
    }).join('')}
    <div style="margin-top:16px;font-size:.83rem;color:var(--muted);text-align:center"><span class="pulse-dot">●</span> Waiting…</div>
  </div>`;
}

function draftHTML(){
  const{draft_order,current_picker_index,players,captains,current_group,group_index}=gameState;
  const order=draft_order||[];const pidx=current_picker_index??0;
  const currentPicker=order[pidx];const isMyTurn=currentPicker===me.sub;
  const gPlayers=(players||[]).filter(p=>getGroup(p.batch_year)===current_group).sort((a,b)=>a.name.localeCompare(b.name));
  const allPicked=gPlayers.every(p=>p.taken_by);
  let html=`<div class="fade-in">
    <div class="draft-header"><span>Draft · <span class="dh-group">${GROUP_LABELS[current_group]||''}</span></span>
      <span class="mono" style="font-size:.8rem;color:var(--muted)">Round ${(group_index||0)+1}/3</span></div>
    <div class="order-chips">${order.map((cid,i)=>{const cap=(captains||[]).find(c=>c.id===cid);
      return `<div class="order-chip ${i===pidx&&!allPicked?'active-pick':''}"><span class="chip-rank">${i+1}</span>${cap?.name||cid}</div>`;
    }).join('')}</div>`;
  if(allPicked) html+=`<div class="waiting-banner">All players in this group picked ✓ — waiting for admin to start next round.</div>`;
  else if(isMyTurn) html+=`<div class="my-turn-banner">🟢 YOUR PICK — Select a player below</div>`;
  else{const pn=(captains||[]).find(c=>c.id===currentPicker)?.name||currentPicker;html+=`<div class="waiting-banner"><span class="pulse-dot">●</span> Waiting for <strong>${pn}</strong> to pick…</div>`;}
  html+=`<div class="player-grid">${gPlayers.map(p=>{
    const taken=!!p.taken_by;
    return `<div class="player-card ${taken?'taken':isMyTurn?'pickable':''}" ${!taken&&isMyTurn?`onclick="pickPlayer('${p.id}')"`:''}><div class="pc-name">${p.name}</div><div class="pc-meta">${p.position} · ${p.batch_year}</div>${taken?`<div class="pc-owner">→ ${p.captain_name||p.taken_by}</div>`:''}</div>`;
  }).join('')}</div>
  <div class="divider"></div><div class="card-title">Live Teams</div>${teamsHTML()}</div>`;
  return html;
}

function doneHTML(){
  return `<div class="fade-in"><div class="results-header">Draft Complete 🏆</div>
    <p style="text-align:center;color:var(--muted);margin-bottom:28px">Final squads.</p>
    ${teamsHTML()}
    <div style="text-align:center;margin-top:28px"><button class="btn btn-secondary" onclick="logout()">Exit</button></div>
  </div>`;
}

function posClass(pos){return POS_CLASS[pos?.toUpperCase()]||'pos-unknown';}
function getGroup(y){return y<=2004?'g1':y<=2018?'g2':'g3';}

function sortPlayers(pls){
  const s=[...pls],o={GK:0,DEF:1,MID:2,ATK:3};
  if(currentSort==='position') s.sort((a,b)=>{const pa=o[a.position?.toUpperCase()]??99,pb=o[b.position?.toUpperCase()]??99;return pa!==pb?pa-pb:a.name.localeCompare(b.name);});
  else if(currentSort==='name') s.sort((a,b)=>a.name.localeCompare(b.name));
  else s.sort((a,b)=>a.batch_year-b.batch_year);
  return s;
}

function sortBar(){
  const labels={year:'Batch Year',position:'Position',name:'Name'};
  return '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:16px;background:var(--card);border:1px solid var(--border);border-radius:5px;padding:10px 14px">'
    +'<span style="font-size:.72rem;font-weight:700;color:var(--muted);text-transform:uppercase">Sort:</span>'
    +Object.keys(labels).map(s=>`<button onclick="reSort('${s}')" style="font-size:.78rem;font-weight:600;padding:5px 12px;border-radius:3px;cursor:pointer;text-transform:uppercase;border:1.5px solid ${currentSort===s?'var(--lime)':'var(--border)'};background:${currentSort===s?'rgba(184,255,63,0.07)':'transparent'};color:${currentSort===s?'var(--lime)':'var(--muted)'}">${labels[s]}</button>`).join('')+'</div>';
}
function reSort(s){currentSort=s;const w=document.getElementById('teams-sort-wrap');if(w)w.innerHTML=sortBar()+teamsInnerHTML();}
function teamsHTML(){return '<div id="teams-sort-wrap">'+sortBar()+teamsInnerHTML()+'</div>';}
function teamsInnerHTML(){
  const{captains,players}=gameState||{};
  if(!captains||!captains.length) return '';
  return '<div class="teams-grid">'+captains.map(c=>{
    const mine=sortPlayers((players||[]).filter(p=>p.taken_by===c.id));
    const dn=c.team_name||c.name;
    const logo=c.team_logo?`<img src="${c.team_logo}" onclick="zoomLogo('${c.team_logo}')" style="width:24px;height:24px;border-radius:3px;object-fit:cover;cursor:pointer;flex-shrink:0"/>`:""
    return '<div class="team-col"><div class="team-header"><span style="display:inline-flex;align-items:center;gap:6px">'+logo+dn+'</span><span class="mono" style="font-size:.78rem;color:var(--muted);font-weight:normal">'+mine.length+'</span></div>'
      +(mine.length?mine.map(p=>'<div class="team-player"><span>'+p.name+'</span><div style="display:flex;gap:5px;align-items:center"><span class="pos-badge '+posClass(p.position)+'">'+(p.position||'?')+'</span><span class="year-tag">'+p.batch_year+'</span></div></div>').join('')
        :'<div style="font-size:.8rem;color:var(--muted);padding:6px 0">No picks</div>')+'</div>';
  }).join('')+'</div>';
}

async function submitAnswer(i){
  if(hasAnswered)return;hasAnswered=true;if(!soundCtx)initSound();
  document.querySelectorAll('.mcq-btn').forEach(b=>b.disabled=true);
  const res=await fetch(`/api/events/${eid}/game/answer`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chosen_index:i})});
  if(res.ok){const d=await res.json();const btn=document.getElementById(`opt-${i}`);if(btn)btn.classList.add(d.is_correct?'correct':'wrong');if(d.is_correct)soundCorrect();else soundWrong();await loadGame();}
  else{hasAnswered=false;document.querySelectorAll('.mcq-btn').forEach(b=>b.disabled=false);}
}

async function pickPlayer(pid){
  soundPick();
  const res=await fetch(`/api/events/${eid}/game/pick`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({player_id:pid})});
  if(!res.ok){const d=await res.json();toast(d.detail||'Pick failed',true);}
}

function zoomLogo(url){const o=document.createElement('div');o.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.85);display:flex;align-items:center;justify-content:center;z-index:9999;cursor:pointer';o.innerHTML=`<img src="${url}" style="max-width:90vw;max-height:90vh;border-radius:10px"/>`;o.onclick=()=>document.body.removeChild(o);document.body.appendChild(o);}
async function logout(){await fetch('/api/auth/logout',{method:'POST'});window.location.href='/';}
function toast(msg,e=false){const el=document.getElementById('toast');el.textContent=msg;el.className='show'+(e?' error':'');clearTimeout(toast._t);toast._t=setTimeout(()=>el.className='',2800);}
init();
