'use strict';
let overview = null;
let selectedId = null;
let token = '';
let busy = false;
const $ = (id) => document.getElementById(id);
const labels = {awaiting_approval: 'Awaiting approval', approved: 'Approved', executing: 'Recovering', recovered: 'Recovered', diagnosed: 'Diagnosed', verification_required: 'Needs verification', failed: 'Failed'};

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function badge(status) {
  return element('span', labels[status] || status, 'pill ' + (status === 'recovered' ? '' : status === 'verification_required' ? 'failure' : 'warning'));
}
function date(timestamp) { return new Date(timestamp * 1000).toLocaleString(); }
async function api(path, body) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api' + path, {method: body === undefined ? 'GET' : 'POST', headers, body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) $('auth').classList.remove('hidden');
    throw new Error(typeof data.detail === 'string' ? data.detail : 'The request could not be validated.');
  }
  return data;
}
async function run(message, task) {
  if (busy) return;
  busy = true;
  $('error').classList.add('hidden');
  $('activity').textContent = message;
  document.querySelectorAll('button').forEach(button => { button.disabled = true; });
  try {
    await task();
    overview = await api('/overview');
    $('auth').classList.add('hidden');
    render();
  } catch (error) {
    $('error').textContent = error.message;
    $('error').classList.remove('hidden');
  } finally {
    busy = false;
    $('activity').textContent = '';
    updateButtons();
  }
}
function updateButtons() {
  document.querySelectorAll('button').forEach(button => { button.disabled = false; });
  if (!overview) {
    ['save-checkpoint', 'inject', 'diagnose'].forEach(id => { $(id).disabled = true; });
    return;
  }
  const healthy = overview.diagnosis.code === 'healthy';
  $('save-checkpoint').disabled = !healthy;
  $('inject').disabled = !overview.checkpoint || !healthy;
  const incident = overview.incidents.find(i => i.id === selectedId);
  if ($('approve')) $('approve').disabled = incident.status !== 'awaiting_approval' || Date.now() >= incident.expires_at * 1000;
  if ($('execute')) $('execute').disabled = incident.status !== 'approved' || Date.now() >= incident.expires_at * 1000;
}
function render() {
  const demo = overview.mode === 'demo';
  $('mode').textContent = demo ? 'SIMULATED ENVIRONMENT' : 'LIVE LOCAL CLUSTER';
  $('mode-note').textContent = demo ? 'Demo mode · Workloads and recovery are simulated. No Kubernetes cluster is modified. Follow the five steps below to try the complete workflow.' : `Live mode · Connected to ${overview.snapshot.context}. Actions are restricted to sample-app in kuberecovery-demo. Inject failures using the lab CLI.`;
  $('failure-controls').classList.toggle('hidden', !demo);
  $('health').textContent = overview.diagnosis.code === 'healthy' ? 'Healthy' : 'Degraded';
  $('health').style.color = overview.diagnosis.code === 'healthy' ? '#147d64' : '#b67d29';
  $('replicas').textContent = `${overview.snapshot.ready} / ${overview.snapshot.desired} replicas ready · generation ${overview.snapshot.generation}`;
  $('checkpoint').textContent = overview.checkpoint ? `Revision ${overview.checkpoint.revision}` : 'Not saved';
  $('checkpoint-note').textContent = overview.checkpoint ? 'Verified healthy when saved' : 'Save before testing a failure';
  $('incident-count').textContent = overview.incidents.length;
  $('queue-count').textContent = overview.incidents.length;
  const recovered = overview.incidents.filter(i => i.status === 'recovered').length;
  $('incident-note').textContent = recovered ? `${recovered} verified ${demo ? 'simulated ' : ''}recoveries` : 'A clear record of every recovery';
  if (!selectedId && overview.incidents.length) selectedId = overview.incidents[0].id;
  renderIncidents();
  renderDetail();
  renderAudit();
  let step = !overview.checkpoint ? 0 : overview.diagnosis.code === 'healthy' ? 1 : 2;
  const selected = overview.incidents.find(i => i.id === selectedId);
  if (selected && selected.status === 'awaiting_approval') step = 3;
  if (selected && ['approved','recovered','verification_required'].includes(selected.status)) step = 4;
  document.querySelectorAll('.steps li').forEach((li, index) => li.classList.toggle('active', index === step));
}
function renderIncidents() {
  if (!overview.incidents.length) return;
  $('incidents').replaceChildren();
  for (const incident of overview.incidents) {
    const button = element('button', undefined, 'incident-item' + (incident.id === selectedId ? ' selected' : ''));
    button.append(element('strong', incident.diagnosis.title), badge(incident.status), element('small', date(incident.created_at)));
    button.addEventListener('click', () => { selectedId = incident.id; render(); updateButtons(); });
    $('incidents').append(button);
  }
}
function renderDetail() {
  const incident = overview.incidents.find(i => i.id === selectedId);
  if (!incident) return;
  $('detail-subtitle').textContent = `Incident ${incident.id.slice(0, 8)} · sample-app`;
  $('incident-status').replaceWith(Object.assign(badge(incident.status), {id: 'incident-status'}));
  const container = $('detail');
  container.className = 'detail-content';
  container.replaceChildren();
  const heading = element('div', undefined, 'diagnosis-heading');
  heading.append(element('span', '◈', 'diagnosis-icon'), element('h3', incident.diagnosis.title));
  container.append(heading, element('p', incident.diagnosis.explanation), element('div', 'COLLECTED EVIDENCE', 'section-label'));
  const list = element('ul', undefined, 'evidence');
  for (const evidence of incident.diagnosis.evidence) {
    const li = element('li');
    li.append(element('small', evidence.source), element('span', evidence.message));
    list.append(li);
  }
  if (!list.children.length) list.append(element('li', 'No matching failure events in this snapshot.'));
  container.append(list);
  for (const [pod, logs] of Object.entries(incident.snapshot.logs)) {
    container.append(element('div', `PREVIOUS LOGS · ${pod}`, 'section-label'), element('pre', logs));
  }
  if (incident.plan) {
    container.append(element('div', 'RECOVERY PREVIEW', 'section-label'));
    const preview = element('div', undefined, 'preview');
    preview.append(element('p', incident.plan.summary));
    const grid = element('div', undefined, 'preview-grid');
    for (const [title, template] of [['Current pod configuration', incident.plan.source_template], ['Healthy checkpoint configuration', incident.plan.target_template]]) {
      const column = element('div');
      column.append(element('label', title), element('pre', JSON.stringify(template, null, 2)));
      grid.append(column);
    }
    preview.append(grid);
    container.append(preview);
    if (['awaiting_approval', 'approved'].includes(incident.status)) {
      const actions = element('div', undefined, 'recovery-actions');
      const approve = Object.assign(element('button', 'Approve this plan', 'secondary'), {id: 'approve'});
      const execute = Object.assign(element('button', 'Execute recovery'), {id: 'execute'});
      approve.addEventListener('click', () => run('Recording approval for this exact plan…', () => api(`/incidents/${incident.id}/approve`, {plan_digest: incident.plan_digest})));
      execute.addEventListener('click', () => run('Restoring the checkpoint and checking rollout health. Verification allows up to 60 seconds after the patch…', () => api(`/incidents/${incident.id}/execute`, {})));
      actions.append(approve, execute);
      container.append(actions, element('div', `Plan expires ${date(incident.expires_at)}. A changed deployment requires a new preview.`, 'expiry'));
    }
  } else {
    container.append(element('div', incident.blocked_reason, 'result warning'));
  }
  if (incident.status === 'recovered') {
    const seconds = (incident.completed_at - incident.execution_started_at).toFixed(2);
    container.append(element('div', `${overview.mode === 'demo' ? 'Simulated recovery' : 'Recovery'} verified. All desired replicas are ready, available, and running the checkpoint template. Execution and verification: ${seconds}s.`, 'result'));
  }
  if (incident.error) container.append(element('div', incident.error, 'result warning'));
}
function renderAudit() {
  if (!overview.audit.length) return;
  $('audit-rows').replaceChildren();
  for (const event of overview.audit) {
    const row = element('tr');
    for (const value of [date(event.timestamp), event.action.replaceAll('_', ' '), event.actor, event.incident_id ? event.incident_id.slice(0, 8) : '—']) row.append(element('td', value));
    $('audit-rows').append(row);
  }
}
$('refresh').addEventListener('click', () => run('Collecting deployment status…', async () => {}));
$('save-checkpoint').addEventListener('click', () => run('Verifying and saving the healthy configuration…', () => api('/checkpoints', {})));
$('inject').addEventListener('click', () => run('Introducing a simulated failure…', () => api('/demo/failures', {scenario: $('scenario').value})));
$('diagnose').addEventListener('click', () => run('Collecting evidence and preparing a diagnosis…', async () => {
  const incident = await api('/incidents', {request_key: crypto.randomUUID()});
  selectedId = incident.id;
}));
$('auth').addEventListener('submit', event => {
  event.preventDefault();
  token = $('token').value;
  $('token').value = '';
  run('Connecting to the live local cluster…', async () => {});
});
run('Connecting to the recovery service…', async () => {});
