'use strict';
const state = {split:'overall',metric:'score',domain:'overall',descending:true};
const domainIndex = {coding:0,cowork:1,reasoning:2,overall:3};
const splitNames = {overall:'Overall',development:'Development',sealed:'Sealed'};
let data;
function orderedRows(kind) {
  return data.rows.filter(row=>row.kind===kind).sort((a,b)=>{
    const diff = b[state.metric][state.split][domainIndex[state.domain]] - a[state.metric][state.split][domainIndex[state.domain]];
    return (state.descending?diff:-diff)||a.name.localeCompare(b.name);
  });
}
function renderRows(id, rows, baseline=false) {
  const body=document.getElementById(id); body.replaceChildren();
  rows.forEach((row,index)=>{
    const tr=document.createElement('tr');
    const rank=document.createElement('td');rank.className='rank';rank.textContent=baseline?'—':index+1;tr.append(rank);
    const name=document.createElement('td');name.className='name';if(!baseline){const a=document.createElement('a');a.href='traces/run.html?id='+encodeURIComponent(row.id);a.textContent=row.name;name.append(a)}else{name.textContent=row.name}
    if(baseline){const label=document.createElement('span');label.className='baseline-label';label.textContent='baseline';name.append(label)}tr.append(name);
    for(const domain of ['overall','coding','cowork','reasoning']){const cell=document.createElement('td');cell.textContent=row[state.metric][state.split][domainIndex[domain]].toFixed(2);if(domain===state.domain)cell.className='primary';tr.append(cell)}
    body.append(tr);
  });
}
function render(){
  renderRows('results',orderedRows('agent'));renderRows('baseline',orderedRows('baseline'),true);
  document.querySelectorAll('[data-split]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.split===state.split)));
  document.querySelectorAll('[data-metric]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.metric===state.metric)));
  document.querySelectorAll('[data-domain]').forEach(b=>{const active=b.dataset.domain===state.domain;b.parentElement.setAttribute('aria-sort',active?(state.descending?'descending':'ascending'):'none');b.querySelector('span').textContent=active?(state.descending?'↓':'↑'):'↕'});
  const metric=state.metric==='score'?'Score = 100 × CCC':'Pairwise agreement (%)';
  document.getElementById('table-description').textContent=`${metric}. Higher is better. Target-equal average across ${data.groups[state.split]} benchmarks.`;
  document.getElementById('result-status').textContent=`${splitNames[state.split]} · ${orderedRows('agent').length} researchers · sorted by ${state.domain==='cowork'?'co-work':state.domain}`;
}
async function init(){
  try{
    const response=await fetch('data.json');if(!response.ok)throw new Error('Results unavailable');data=await response.json();
    for(const row of data.rows)for(const metric of ['score','pa'])for(const split of Object.keys(splitNames))if(!Array.isArray(row[metric]?.[split])||row[metric][split].length!==4||!row[metric][split].every(Number.isFinite))throw new Error('Invalid results');
    document.querySelectorAll('[data-split]').forEach(b=>b.addEventListener('click',()=>{state.split=b.dataset.split;render()}));
    document.querySelectorAll('[data-metric]').forEach(b=>b.addEventListener('click',()=>{state.metric=b.dataset.metric;render()}));
    document.querySelectorAll('[data-domain]').forEach(b=>b.addEventListener('click',()=>{state.descending=state.domain===b.dataset.domain?!state.descending:true;state.domain=b.dataset.domain;render()}));
    if(data.paperUrl){const u=new URL(data.paperUrl);if(u.protocol==='https:'){const a=document.getElementById('paper-link');a.href=u.href;a.hidden=false}}
    document.getElementById('download').addEventListener('click',()=>{
      const escape=s=>'"'+String(s).replaceAll('"','""')+'"';
      const rows=[['Researcher','Kind','Split','Metric','Overall','Coding','Co-work','Reasoning'],...[...orderedRows('agent'),...orderedRows('baseline')].map(r=>[r.name,r.kind,state.split,state.metric,...['overall','coding','cowork','reasoning'].map(d=>r[state.metric][state.split][domainIndex[d]])])];
      const url=URL.createObjectURL(new Blob([rows.map(r=>r.map(escape).join(',')).join('\n')],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=`erb-${state.split}-${state.metric}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    });render();
  }catch(e){const body=document.getElementById('results');body.replaceChildren();const tr=document.createElement('tr');const td=document.createElement('td');td.colSpan=6;td.textContent='Results could not be loaded. Please refresh or download the source data below.';tr.append(td);body.append(tr);document.getElementById('download').disabled=true;document.getElementById('result-status').textContent='Results unavailable';}
}
init();
