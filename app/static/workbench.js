import {request} from './client.js';
import {snapshot, history} from './view.js';
import {visualize} from './plots.js';

const form = document.querySelector('#source');
const file = document.querySelector('#file');
const profile = document.querySelector('#profile');
const status = document.querySelector('#notice');
const detail = document.querySelector('#review');
const evidence = document.querySelector('#evidence');
const stages = [...document.querySelectorAll('[data-stage]')];
let stage = 'balance';
let report, selected, batch, running = false;

function source() {
  const data = new FormData();
  if (file.files.length) data.append('file',file.files[0]);
  else data.append('sample',profile.value);
  return data;
}

function announce(text, failure = false) {
  status.textContent = text;
  status.dataset.failure = String(failure);
}

function draw() {
  if (!report) return;
  snapshot(report, selected, stage);
  visualize(report, selected);
  for (const button of stages) button.setAttribute('aria-pressed',String(button.dataset.stage===stage));
  document.querySelector('#panel-title').textContent = stages.find(button => button.dataset.stage===stage)?.textContent || '运行对账';
  detail.hidden = !batch;
  if (batch) {
    document.querySelector('#run-status').textContent = `${batch.id.slice(0,10)} · ${batch.status} · r${batch.revision}`;
    detail.querySelector('[value="APPROVED"]').disabled = !report.balance.releasable || batch.status==='APPROVED';
    detail.querySelector('[value="HELD"]').disabled = batch.status==='APPROVED';
    evidence.href = `/api/runs/${batch.id}/evidence`;
  }
}

function show(payload, committed = false) {
  batch = committed ? payload : null;
  report = committed ? payload.report : payload;
  detail.elements.note.value = batch?.note || '';
  selected = report.cases?.[0]?.case;
  draw();
}

async function refresh() {
  const data = await request('/api/runs');
  history(data.items, batch?.id);
}

async function perform(action) {
  if (running) return;
  running = true;
  document.body.setAttribute('aria-busy','true');
  announce('处理中…');
  try { await action(); }
  catch (error) { announce(error.message,true); }
  finally { running = false; document.body.removeAttribute('aria-busy'); }
}

function preview(next) {
  return perform(async () => {
    const payload = await request(`/api/stages/${next}`,{method:'POST',body:source()});
    stage = next;
    show(payload);
    announce('阶段预览已更新');
    await refresh();
  });
}

stages.forEach(button => button.addEventListener('click',()=>preview(button.dataset.stage)));
form.addEventListener('submit', event => {
  event.preventDefault();
  perform(async () => {
    const result = await request('/api/runs',{method:'POST',body:source()});
    stage = 'balance'; show(result,true);
    announce('批次已保存'); await refresh();
  });
});
const filename = document.querySelector('#file-name');
profile.addEventListener('change',()=>{file.value=''; filename.textContent=''; preview(stage);});
file.addEventListener('change',()=>{filename.textContent=file.files[0]?.name || ''; preview(stage);});
document.querySelector('#rows').addEventListener('click',event=>{
  const button = event.target.closest('[data-case]');
  if (button) { selected = button.dataset.case; draw(); document.querySelector(`[data-case="${CSS.escape(selected)}"]`).focus(); }
});
document.querySelector('#history').addEventListener('click',event=>{
  const button = event.target.closest('[data-run]');
  if (button) perform(async()=>{
    const result = await request(`/api/runs/${button.dataset.run}`);
    stage='balance'; show(result,true); announce('已载入历史批次'); await refresh();
  });
});
detail.addEventListener('submit',event=>{
  event.preventDefault();
  perform(async()=>{
    const payload={status:event.submitter.value,revision:batch.revision,note:detail.elements.note.value};
    const result=await request(`/api/runs/${batch.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    show(result,true); announce(result.status==='APPROVED'?'批次已签发':'批次已暂缓'); await refresh();
    if (typeof loadCredits === 'function') await loadCredits();
  });
});
preview(stage);

// Post-issuance withdrawals, corrections and current credit positions.
const creditTable = document.querySelector('#credit-rows');
const creditForm = document.querySelector('#credit-event');
const creditDetail = document.querySelector('#credit-detail');

function creditBadge(status, valid) {
  const label = valid === false ? '抵扣失效' : ({READY:'达标', ACTIVE:'有效', CREDIT_SHORTFALL:'低于最低学分'}[status] || status);
  return `<span class="badge" data-result="${valid === false ? 'CREDIT_SHORTFALL' : 'READY'}">${label}</span>`;
}

async function loadCredits() {
  if (!creditTable) return;
  try {
    const data = await request('/api/workbench/credits');
    const items = data.items || [];
    const head = element('tr');
    head.append(...['申请', '当前可用', '复算/最低', '状态', '原签发凭证', '办理'].map(title => element('th', title, {scope:'col'})));
    creditTable.replaceChildren();
    creditTable.createTHead().append(head);
    const body = creditTable.createTBody();
    for (const item of items) {
      const tr = body.insertRow();
      tr.insertCell().textContent = item.application_id;
      tr.insertCell().textContent = String(item.available_credits ?? 0);
      tr.insertCell().textContent = `${item.recomputed_credits ?? 0}/${item.minimum_credits ?? 0}`;
      const statusCell = tr.insertCell();
      statusCell.innerHTML = creditBadge(item.status, item.deduction_valid);
      const certCell = tr.insertCell();
      if (item.issue_no) {
        const link = element('a', '下载当时凭证', {href:`/api/certificates/${encodeURIComponent(item.issue_no)}/download`});
        certCell.append(link);
      } else certCell.textContent = '—';
      const actionCell = tr.insertCell();
      const button = element('button', '办理变动', {type:'button', 'data-credit-app':item.application_id});
      actionCell.append(button);
    }
    if (!items.length) body.insertRow().insertCell().textContent = '签发批次后显示申请学分';
  } catch (error) {
    if (creditDetail) creditDetail.textContent = error.message;
  }
}

creditTable?.addEventListener('click', event => {
  const button = event.target.closest('[data-credit-app]');
  if (!button || !creditForm) return;
  creditForm.elements.application_id.value = button.dataset.creditApp;
  document.querySelector('#credit-target').textContent = `当前办理申请 ${button.dataset.creditApp}`;
  creditForm.scrollIntoView({block:'nearest'});
});

document.querySelector('#refresh-credits')?.addEventListener('click', () => perform(loadCredits));

creditForm?.addEventListener('submit', event => {
  event.preventDefault();
  perform(async () => {
    const form = new FormData(creditForm);
    const app = form.get('application_id');
    const kind = form.get('event_type');
    const body = {
      external_course: form.get('external_course') || undefined,
      local_course: form.get('local_course') || undefined,
    };
    if (form.get('credits') !== '') body.credits = Number(form.get('credits'));
    let path = `/api/applications/${encodeURIComponent(app)}`;
    if (kind === 'withdrawal') path += '/withdrawals';
    else {
      path += '/corrections';
      if (kind === 'replacement') body.new_external_course = form.get('new_external_course') || undefined;
    }
    const result = await request(path, {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body),
    });
    creditDetail.textContent = JSON.stringify({
      application_id: result.application_id,
      status: result.status,
      available_credits: result.available_credits,
      recomputed_credits: result.recomputed_credits,
      deduction_valid: result.deduction_valid,
      layer: result.layer,
      stale: result.stale,
    }, null, 2);
    announce('学分状态已复算');
    await loadCredits();
  });
});

loadCredits();
