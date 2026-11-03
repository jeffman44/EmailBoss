(function(){
  const LS_POS='dpsm_pos', LS_OPEN='dpsm_open', LS_WIN='dpsm_win';
  const pane=document.getElementById('dpsm'), launch=document.getElementById('dpsm-launch'),
        head=document.getElementById('dpsm-head');
  const COLORS=[["#c41f3b","#ff5a6e"],["#a330c9","#d67bff"],["#0070dd","#4aa8ff"],
    ["#00ff96","#7dffc4"],["#ff7d0a","#ffb15e"],["#f58cba","#ffc2df"],
    ["#3fc7eb","#8fe6ff"],["#8788ee","#b3b4ff"],["#c69b6d","#e6c79c"]];
  let DATA=null, win=parseInt(localStorage.getItem(LS_WIN))||7;
  const fmtDay=d=>new Date(d+"T12:00:00").toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'});

  // ── position (restore or default bottom-right-ish) ──
  function place(){
    let p; try{p=JSON.parse(localStorage.getItem(LS_POS));}catch(e){}
    if(p&&typeof p.x==='number'){ pane.style.left=Math.max(0,Math.min(p.x,innerWidth-60))+'px'; pane.style.top=Math.max(0,Math.min(p.y,innerHeight-40))+'px'; pane.style.right='auto'; }
    else { pane.style.right='16px'; pane.style.bottom='62px'; pane.style.left='auto'; pane.style.top='auto'; }
  }
  function open(){ pane.style.display='block'; launch.style.display='none'; place(); localStorage.setItem(LS_OPEN,'true'); load(); }
  function close(){ pane.style.display='none'; launch.style.display='block'; localStorage.setItem(LS_OPEN,'false'); }
  launch.onclick=open;
  document.getElementById('dpsm-close').onclick=close;
  document.getElementById('dpsm-reload').onclick=e=>{ e.stopPropagation(); load(true); };

  // ── drag ──
  let dragging=false,ox=0,oy=0;
  head.addEventListener('pointerdown',e=>{
    if(e.target.classList.contains('dpsm-x'))return;
    dragging=true; head.classList.add('drag');
    const r=pane.getBoundingClientRect(); ox=e.clientX-r.left; oy=e.clientY-r.top;
    pane.style.right='auto'; pane.style.bottom='auto';
    head.setPointerCapture(e.pointerId);
  });
  head.addEventListener('pointermove',e=>{
    if(!dragging)return;
    const x=Math.max(0,Math.min(e.clientX-ox,innerWidth-60)), y=Math.max(0,Math.min(e.clientY-oy,innerHeight-40));
    pane.style.left=x+'px'; pane.style.top=y+'px';
  });
  head.addEventListener('pointerup',e=>{
    if(!dragging)return; dragging=false; head.classList.remove('drag');
    const r=pane.getBoundingClientRect();
    localStorage.setItem(LS_POS,JSON.stringify({x:r.left,y:r.top}));
  });

  // ── tabs ──
  pane.querySelectorAll('.dpsm-tab').forEach(t=>t.onclick=()=>{
    pane.querySelectorAll('.dpsm-tab').forEach(x=>x.classList.remove('on'));
    t.classList.add('on'); win=+t.dataset.win; localStorage.setItem(LS_WIN,win); renderRows();
  });

  // Top 5 only, in EVERY window. A 30-day list of 30 bars is a wall; the meter
  // is for "who's topping the charts", not an archive.
  const TOP_N=5;

  // LIVE PACE: damage per minute over a rolling 60-minute window.
  // Rolling avoids needing to detect "when did the day start": at 6am or 6pm
  // it answers the same question, "how hard am I going right now". Reads the
  // board's own in-memory snapshots, so a forced snapshot from checking a
  // subtask shows up here with no server round-trip.
  const ROLL_MIN=60;
  // Recount abbreviates big parses (1.2M): same idea, scaled to our numbers.
  function fmtDmg(v){
    v=Math.round(Number(v)||0);
    return v>=1e6 ? (v/1e6).toFixed(1).replace(/\.0$/,'')+'M'
         : v>=1e4 ? (v/1e3).toFixed(1).replace(/\.0$/,'')+'k'
         : v.toLocaleString();
  }
  function livePace(){
    const evs = (typeof _damageEvents!=='undefined' && _damageEvents) || [];
    const cutoff = Date.now() - ROLL_MIN*60000;
    const dmg = evs.filter(e=>+new Date(e.time) >= cutoff)
                   .reduce((a,e)=>a+(Number(e.amt)||0),0);
    // Always divide by the FULL window. Idle time is genuinely low pace; if
    // you do nothing for an hour, the meter must read 0, not hide the gap.
    return {dmg:Math.round(dmg), dpm:+(dmg/ROLL_MIN).toFixed(1), mins:ROLL_MIN};
  }
  function renderRows(){
    if(!DATA)return;
    // Ranked by DPM. This is a DPM meter: a 15-hour grind that racked up big
    // raw damage is NOT a better parse than a 6-hour day at double the pace.
    const ranked=DATA.series.slice(-win).filter(x=>(x.dpm??0)>0)
                     .sort((a,b)=>(b.dpm??0)-(a.dpm??0));
    const slice=ranked.slice(0,TOP_N);
    const max=Math.max(...slice.map(x=>x.dpm??0),1);
    const rows=document.getElementById('dpsm-rows'); rows.innerHTML='';
    slice.forEach((x,i)=>{
      const c=COLORS[i%COLORS.length], w=Math.max(7,(x.dpm??0)/max*100);
      const el=document.createElement('div'); el.className='dpsm-row';
      el.innerHTML=`<div class="dpsm-bar" style="background:linear-gradient(90deg,${c[0]},${c[1]})"></div>
        <div class="dpsm-rtxt"><span><span class="r">${i+1}.</span>${fmtDay(x.date)}</span><span>${x.dpm??0}<span class="tot">(${fmtDmg(x.damage??x.n??0)})</span></span></div>`;
      rows.appendChild(el);
      requestAnimationFrame(()=>setTimeout(()=>el.querySelector('.dpsm-bar').style.width=w+'%',35*i));
    });
  }

  function paint(){
    const s=Object.assign({},DATA.stats||{});
    // THE WHOLE TOP BLOCK IS A RATE. Big number, bar, avg and best are ALL
    // damage-per-minute so nothing in the widget mixes units. Cumulative damage
    // totals live in the pace line and the per-day rows underneath.
    // Recompute best/avg/total over the SELECTED window so the 7/14/30 tabs
    // drive the whole pane, not just the bar list. `today` stays today.
    // Busted (flapped) days never reach the client: the server drops them.
    const w=DATA.series.slice(-win), dmg=w.map(x=>x.damage??x.n??0), act=dmg.filter(v=>v>0);
    const rates=w.map(x=>x.dpm??0).filter(v=>v>0);
    if(w.length){
      s.total=dmg.reduce((a,b)=>a+b,0);
      s.best_dmg=Math.max(...dmg,0);
      // Rate benchmarks over the selected window.
      s.best_dpm=rates.length?Math.max(...rates):0;
      s.avg_dpm=rates.length?+(rates.reduce((a,b)=>a+b,0)/rates.length).toFixed(1):0;
      s.mean_active=act.length?Math.round(act.reduce((a,b)=>a+b,0)/act.length):0;
    }
    const lp=livePace();
    const bestD=s.best_dpm??0, avgD=s.avg_dpm??0;
    document.getElementById('dpsm-today').innerHTML=lp.dpm+`<small>DPM · ${lp.mins}m ROLL</small>`;
    const pct=bestD?Math.min(100,Math.round((lp.dpm/bestD)*100)):0;
    const fill=document.getElementById('dpsm-fill');
    fill.className='dpsm-fill'+(pct>=85?' crit':pct>=55?' hot':'');
    setTimeout(()=>fill.style.width=pct+'%',100);
    document.getElementById('dpsm-pct').textContent=pct+'%';
    // Pill removed: it repeated the big number on top of its own bar. The
    // % sits at the right of the track already.
    // How today stacks up: live pace vs the window average and the record.
    const pcOf=(a,b)=>b>0?Math.round(a/b*100):0;
    document.getElementById('dpsm-pace').innerHTML =
      `<span>${win}d avg <b>${avgD}</b> · rec <b class="gold">${bestD}</b></span>`+
      `<span>vs avg <b class="${lp.dpm>=avgD?'up':'dn'}">${pcOf(lp.dpm,avgD)}%</b></span>`;
    // Verdict reads the RATE. Sitting at the keyboard all day is not a parse.
    const v = (lp.dpm>0 && lp.dpm>=bestD) ? 'RAGNAROS DOWN'
            : (lp.dpm>=avgD*1.25) ? 'topping the meters'
            : (lp.dpm>=avgD) ? 'holding rotation'
            : (lp.dpm>0) ? 'standing in fire'
            : 'afk';
    document.getElementById('dpsm-verdict').textContent=v;
    renderRows();
  }

  let loading=false;
  async function load(force){
    if(loading)return; loading=true;
    const rl=document.getElementById('dpsm-reload'); rl.textContent='…';
    try{
      const r=await fetch('/api/boss-dps'); const j=await r.json();
      if(j&&j.series){ DATA=j; paint(); }
    }catch(e){ document.getElementById('dpsm-verdict').textContent='feed offline'; }
    finally{ loading=false; rl.textContent='⟳'; }
  }

  // auto-refresh every 90s while open
  setInterval(()=>{ if(pane.style.display==='block') load(); },90000);
  // Let renderHealthBar poke the meter the instant a hit lands.
  window.dpsmRefresh = ()=>{ if(pane.style.display==='block') load(true); };
  // The launcher shows once the profession says the game layer is on; the
  // open state is restored at the same moment.
  const gate = () => {
    const on = (typeof feat === 'function') ? feat('game_layer') : true;
    launch.style.display = (on && pane.style.display !== 'block') ? 'block' : 'none';
    if (!on) pane.style.display = 'none';
    return on;
  };
  window.dpsmGate = gate;
  setTimeout(() => { if (gate() && localStorage.getItem(LS_OPEN)==='true') open(); }, 1200);
})();
