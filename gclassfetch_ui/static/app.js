const $ = id => document.getElementById(id);
const selected = new Set();
let courses = [], snapshot = {}, running = false, starting = false, configReady = false;
let logSequence = 0, runId = '', samples = [], reviewFilter = 'attention', reviewLimit = 30;
let courseRequest = 0, configTimer, reviewSignature = '', loadingCourses = false;

function message(text = '') {
  $('notice').textContent = text;
  $('notice').classList.toggle('hidden', !text);
}
async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok || result.error || (result.status && !['ok', 'cancelled'].includes(result.status))) {
    throw new Error(result.error || result.status || 'The request could not be completed. Please try again.');
  }
  return result;
}
function safeUrl(value) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; }
  catch { return ''; }
}
function bytes(value) {
  let n = Number(value) || 0, i = 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${i ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}
function duration(seconds) {
  const n = Math.max(0, Math.floor(seconds || 0));
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`;
}
function payload() {
  return {dest: $('destination').value, workers: Number($('workers').value),
    options: Object.fromEntries([...document.querySelectorAll('[data-option]')].map(el => [el.dataset.option, el.checked]))};
}
function saveConfig() {
  if (!configReady || running) return;
  clearTimeout(configTimer);
  configTimer = setTimeout(() => api('/config', payload()).catch(error => message(`Settings weren't saved. ${error.message}`)), 350);
}
function updateSelection() {
  const visible = [...document.querySelectorAll('.course-row:not(.hidden)')];
  const checked = visible.filter(row => selected.has(row.dataset.cid)).length;
  $('selectAll').checked = visible.length > 0 && checked === visible.length;
  $('selectAll').indeterminate = checked > 0 && checked < visible.length;
  $('selectAll').disabled = !visible.length || running || starting;
  $('selectionSummary').textContent = selected.size ? `${selected.size} course${selected.size === 1 ? '' : 's'} selected. Materials, assignments, and announcements follow your settings.` : 'Choose the courses you want to keep.';
  $('downloadButton').textContent = running ? 'Download in progress' : starting ? 'Starting download…' : selected.size ? `Download ${selected.size} course${selected.size === 1 ? '' : 's'}` : 'Choose courses to download';
  $('downloadButton').disabled = running || starting || loadingCourses || !selected.size || !configReady;
  document.querySelectorAll('.course-row').forEach(row => {
    row.classList.toggle('selected', selected.has(row.dataset.cid));
    row.querySelector('input').checked = selected.has(row.dataset.cid);
    row.querySelector('input').disabled = running || starting;
  });
}
function filterCourses() {
  const query = $('search').value.trim().toLocaleLowerCase();
  let shown = 0;
  document.querySelectorAll('.course-row').forEach(row => {
    const match = row.dataset.search.includes(query);
    row.classList.toggle('hidden', !match);
    shown += match ? 1 : 0;
  });
  $('shownCount').textContent = `${shown} shown`;
  $('emptySearch').classList.toggle('hidden', shown > 0 || courses.length === 0);
  updateSelection();
}
function externalLink(url, label) {
  const link = document.createElement('a');
  link.href = safeUrl(url); link.target = '_blank'; link.rel = 'noopener noreferrer';
  link.setAttribute('aria-label', label); link.title = label;
  return link;
}
function renderCourses() {
  const list = $('courseList');
  list.replaceChildren();
  $('courseCount').textContent = String(courses.length);
  if (!courses.length) {
    const empty = document.createElement('div'); empty.className = 'empty';
    const title = document.createElement('h3'); title.textContent = 'No courses to show';
    const detail = document.createElement('p'); detail.textContent = 'Try including archived courses, or switch to your college Google account.';
    empty.append(title, detail); list.append(empty);
  }
  for (const course of courses) {
    const row = document.createElement('div'); row.className = 'course-row';
    row.dataset.cid = course.id; row.dataset.search = `${course.name || ''} ${course.section || ''}`.toLocaleLowerCase();
    const label = document.createElement('label'); label.className = 'course-choice';
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox';
    checkbox.setAttribute('aria-label', `Select ${course.name || 'Untitled course'}`);
    checkbox.addEventListener('change', () => { checkbox.checked ? selected.add(course.id) : selected.delete(course.id); updateSelection(); });
    const copy = document.createElement('span'); copy.className = 'course-copy';
    const title = document.createElement('span'); title.className = 'course-title'; title.textContent = course.name || 'Untitled course';
    const section = document.createElement('span'); section.className = 'course-section';
    section.textContent = [course.section, course.courseState === 'ARCHIVED' ? 'Archived' : null].filter(Boolean).join(' · ') || 'Google Classroom';
    const status = document.createElement('span'); status.className = 'course-state hidden';
    copy.append(title, section, status); label.append(checkbox, copy); row.append(label);
    if (safeUrl(course.alternateLink)) {
      const link = externalLink(course.alternateLink, `Open ${course.name} in Classroom`); link.className = 'external';
      link.innerHTML = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M14 4h6v6M20 4 10 14M10 4H4v16h16v-6"/></svg>';
      row.append(link);
    }
    list.append(row);
  }
  filterCourses();
  renderCourseStates();
}
async function loadCourses() {
  const requestId = ++courseRequest; loadingCourses = true; updateSelection();
  $('refreshButton').disabled = true; $('courseList').setAttribute('aria-busy', 'true');
  try {
    const result = await api('/courses?archived=' + $('includeArchived').checked);
    if (requestId !== courseRequest) return;
    if (!Array.isArray(result)) throw new Error('Please sign in with Google to load your courses.');
    courses = result.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    for (const id of selected) if (!courses.some(course => course.id === id)) selected.delete(id);
    renderCourses();
    $('connection').textContent = 'Google connected'; $('connection').classList.remove('offline');
  } catch (error) {
    if (requestId !== courseRequest) return;
    const empty = document.createElement('div'); empty.className = 'empty';
    empty.textContent = `${error.message} Use Refresh to try again.`;
    $('courseList').replaceChildren(empty); courses = []; selected.clear(); $('courseCount').textContent = '0'; $('shownCount').textContent = '0 shown'; updateSelection();
    $('connection').textContent = 'Connection needs attention'; $('connection').classList.add('offline');
  } finally {
    if (requestId === courseRequest) { loadingCourses = false; $('refreshButton').disabled = running; $('courseList').setAttribute('aria-busy', 'false'); updateSelection(); }
  }
}
function renderCourseStates() {
  for (const row of document.querySelectorAll('.course-row')) {
    const course = (snapshot.courses || {})[row.dataset.cid], el = row.querySelector('.course-state');
    el.className = 'course-state' + (course ? '' : ' hidden');
    if (!course) continue;
    const statuses = {queued: 'Waiting', scanning: 'Checking materials…', downloading: 'Downloading', done: 'Up to date', 'done-warnings': 'Needs attention', error: 'Could not finish', stopped: 'Stopped'};
    el.textContent = `${statuses[course.status] || course.status}${course.total ? ` · ${course.done || 0} of ${course.total} files saved` : ''}${course.failed ? ` · ${course.failed} failed` : ''}`;
    if (['done-warnings', 'error'].includes(course.status)) el.classList.add('attention');
    if (course.status === 'done') el.classList.add('success');
  }
}
function renderReview() {
  const signature = JSON.stringify([snapshot.issues || [], reviewFilter, reviewLimit]);
  if (signature === reviewSignature) return;
  reviewSignature = signature;
  const issues = snapshot.issues || [], attention = issues.filter(item => item.kind !== 'link'), links = issues.filter(item => item.kind === 'link');
  $('reviewSection').classList.toggle('hidden', !issues.length);
  $('reviewAttentionCount').textContent = attention.length; $('reviewLinkCount').textContent = links.length;
  const filtered = reviewFilter === 'link' ? links : attention;
  $('reviewList').replaceChildren();
  for (const item of filtered.slice(0, reviewLimit)) {
    const row = document.createElement('li'); row.className = 'review-item';
    const copy = document.createElement('div'), title = document.createElement('strong'), reason = document.createElement('p');
    const courseName = (snapshot.courses || {})[item.cid]?.name;
    title.textContent = item.title; reason.textContent = [courseName, item.reason].filter(Boolean).join(' · ');
    copy.append(title, reason); row.append(copy);
    if (safeUrl(item.url)) { const link = externalLink(item.url, `Open original: ${item.title}`); link.textContent = 'Open original'; row.append(link); }
    $('reviewList').append(row);
  }
  if (!filtered.length) { const empty = document.createElement('li'); empty.className = 'empty'; empty.textContent = reviewFilter === 'attention' ? 'No problems reported. Online resources are listed in the next tab.' : 'No online-only resources reported.'; $('reviewList').append(empty); }
  $('showMore').classList.toggle('hidden', filtered.length <= reviewLimit);
  document.querySelectorAll('[data-review]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.review === reviewFilter)));
}
function renderStatus(s) {
  snapshot = s; running = !!s.running;
  const titles = {scanning: 'Checking your courses', downloading: 'Saving your materials', stopping: 'Finishing current work…', stopped: 'Download stopped', done: 'Your materials are ready', 'done-warnings': 'Finished with items to review', error: 'The download could not finish'};
  $('runPanel').classList.toggle('hidden', s.phase === 'idle');
  $('phaseTitle').textContent = titles[s.phase] || 'Ready to download';
  const processed = (s.done_files || 0) + (s.failed_files || 0);
  const percent = s.total_files ? Math.min(100, Math.round(processed / s.total_files * 100)) : 0;
  $('phaseDesc').textContent = s.phase === 'scanning' ? `${s.scan_done || 0} of ${s.scan_total || 0} courses checked. Looking through posts and shared folders.` : s.phase === 'done' ? (s.total_files ? 'Downloadable files are saved. Browse your folder or open the offline library.' : 'No downloadable attachments were found in the included sources. Check the saved post text and online links.') : s.phase === 'done-warnings' ? 'Some materials or course sections could not be saved. Review the list below.' : s.phase === 'stopped' ? 'Saved files are safe. Download these courses again to continue.' : s.phase === 'error' ? 'Check the review below, then try again.' : `${processed} of ${s.total_files || 0} files processed.`;
  if (s.phase === 'scanning' || s.phase === 'stopping') { $('progress').removeAttribute('value'); $('pctText').textContent = ''; }
  else { $('progress').value = percent; $('pctText').textContent = s.total_files ? `${percent}%` : ''; }
  const issues = s.issues || [], attention = issues.filter(item => item.kind !== 'link').length;
  $('savedCount').textContent = Math.max(0, (s.done_files || 0) - (s.skipped_files || 0));
  $('existingCount').textContent = s.skipped_files || 0; $('linkCount').textContent = s.linked_files || 0;
  $('attentionCount').textContent = attention; $('attentionStat').classList.toggle('has-attention', attention > 0);
  $('currentFile').textContent = [s.current_course, s.current_file].filter(Boolean).join(' / ');
  $('currentFile').classList.toggle('hidden', !running || !s.current_file);
  $('stopButton').classList.toggle('hidden', !running); $('stopButton').disabled = s.phase === 'stopping';
  $('retryButton').classList.toggle('hidden', running || !attention || !issues.some(item => item.cid));
  $('reportLink').classList.toggle('hidden', running || !s.report_ready);
  for (const id of ['destination', 'workers', 'browseButton', 'includeArchived', 'refreshButton']) $(id).disabled = running || starting || (id === 'refreshButton' && loadingCourses);
  document.querySelectorAll('[data-option]').forEach(el => el.disabled = running || starting);
  const now = Date.now() / 1000; samples.push({time: now, bytes: s.done_bytes || 0}); samples = samples.filter(sample => now - sample.time < 5);
  const speed = running && samples.length > 1 ? Math.max(0, ((s.done_bytes || 0) - samples[0].bytes) / Math.max(.1, now - samples[0].time)) : 0;
  $('activityMeta').textContent = s.start_time ? `${bytes(s.done_bytes)} saved · ${duration((s.end_time || now) - s.start_time)}` : 'Download details appear here';
  $('transferDetails').textContent = `${bytes(s.done_bytes)} transferred${running ? ` · ${bytes(speed)}/s` : ''} · ${s.failed_files || 0} failed downloads`;
  updateSelection(); renderCourseStates(); renderReview();
}
async function poll() {
  try {
    const status = await api('/status?since=' + logSequence);
    if ($('connection').textContent.startsWith('App disconnected')) {
      $('connection').textContent = document.body.dataset.connected === 'true' ? 'Google connected' : 'Not connected';
      $('connection').classList.remove('offline');
    }
    if (runId !== status.run_id || status.reset) { logSequence = 0; samples = []; $('log').replaceChildren(); runId = status.run_id; }
    for (const entry of status.log || []) {
      if (entry.seq <= logSequence) continue;
      logSequence = entry.seq; const line = document.createElement('div'); line.className = entry.kind; line.textContent = entry.msg; $('log').append(line);
    }
    while ($('log').childNodes.length > 400) $('log').firstChild.remove();
    renderStatus(status);
  } catch { $('connection').textContent = 'App disconnected · reconnecting'; $('connection').classList.add('offline'); }
  finally { setTimeout(poll, 1200); }
}
async function startDownload(ids) {
  if (starting || running || !ids.length) return;
  starting = true; message(); updateSelection();
  clearTimeout(configTimer);
  try {
    const result = await api('/start/' + ids.map(encodeURIComponent).join(','), payload());
    if (result.status !== 'ok') throw new Error(result.status);
    const status = await api('/status'); logSequence = 0; $('log').replaceChildren(); renderStatus(status);
    $('runPanel').scrollIntoView({block: 'nearest', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
  } catch (error) { message(error.message); }
  finally { starting = false; updateSelection(); }
}
async function openFolder(destination) {
  try { await api('/open-folder', {path: destination || $('destination').value}); }
  catch (error) { message(error.message); }
}
$('downloadButton').addEventListener('click', () => startDownload([...selected]));
$('retryButton').addEventListener('click', () => startDownload([...new Set((snapshot.issues || []).filter(item => item.kind !== 'link' && item.cid).map(item => item.cid))]));
$('stopButton').addEventListener('click', async () => { try { await api('/stop', {}); $('stopButton').disabled = true; } catch (error) { message(error.message); } });
$('refreshButton').addEventListener('click', () => { message(); loadCourses(); });
$('includeArchived').addEventListener('change', loadCourses);
$('search').addEventListener('input', filterCourses);
$('clearSearch').addEventListener('click', () => { $('search').value = ''; filterCourses(); $('search').focus(); });
$('selectAll').addEventListener('change', event => { document.querySelectorAll('.course-row:not(.hidden)').forEach(row => event.target.checked ? selected.add(row.dataset.cid) : selected.delete(row.dataset.cid)); updateSelection(); });
$('destination').addEventListener('change', saveConfig); $('workers').addEventListener('change', saveConfig);
document.querySelectorAll('[data-option]').forEach(el => el.addEventListener('change', saveConfig));
$('browseButton').addEventListener('click', async () => {
  $('browseButton').disabled = true;
  try { const result = await api('/select-folder', {current: $('destination').value}); if (result.path) { $('destination').value = result.path; saveConfig(); } }
  catch (error) { message(error.message); }
  finally { $('browseButton').disabled = running; }
});
$('openResults').addEventListener('click', () => openFolder(snapshot.destination));
$('footerFolder').addEventListener('click', () => openFolder());
document.querySelectorAll('[data-review]').forEach(button => button.addEventListener('click', () => { reviewFilter = button.dataset.review; reviewLimit = 30; renderReview(); }));
$('showMore').addEventListener('click', () => { reviewLimit += 30; renderReview(); });
async function init() {
  try {
    const config = await api('/config'); $('destination').value = config.dest || ''; $('workers').value = String(config.workers || 8);
    if (!$('workers').value) { const option = new Option(`${config.workers} downloads`, config.workers, true, true); $('workers').add(option); }
    document.querySelectorAll('[data-option]').forEach(el => { if (typeof config.options?.[el.dataset.option] === 'boolean') el.checked = config.options[el.dataset.option]; });
    configReady = true;
  } catch (error) { message(`Could not load your settings. Refresh this page to try again. ${error.message}`); }
  updateSelection();
  if (document.body.dataset.connected === 'true') await loadCourses();
  else { $('courseList').innerHTML = '<div class="empty"><h3>Your courses will appear here</h3><p>Connect Google Classroom to get started.</p></div>'; $('courseList').setAttribute('aria-busy', 'false'); }
}
init(); poll();
