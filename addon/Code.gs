/**
 * OCR for Drive: Google Drive add-on that makes scanned PDFs searchable.
 *
 * One button does everything: "Start OCR" straightens sideways pages, splits double-page scans down the middle,
 * runs OCR and REPLACES the file with the searchable version. If the add-on has no access to the file yet, the same
 * button opens Drive's permission dialog and starts the OCR as soon as the user allows it. After an OCR, "Revert OCR"
 * takes the place of "Start OCR" and restores the original content. "Flatten and re-OCR" writes a separate
 * "<name> (flatten).pdf" so an existing OCR can be compared with ours.
 *
 * How it works: the card callback never carries the PDF (card callbacks stop at 30 s). It sends the file id to the OCR
 * server and returns at once. The credential is the user's own access token (scope drive.file) in the Authorization
 * header: the server checks that the token was issued for this add-on, identifies the user, downloads the PDF with that
 * token, does the work and writes the result back. "View OCR status" polls the server (own jobs only).
 *
 * Reverting needs no server state: when the server replaces a file it stores the id of the original revision, the md5
 * of its result and the date in the file's appProperties (private to this add-on). The file is "ours" while its md5
 * still equals that stored md5; if someone edits it afterwards, the add-on offers Start OCR again and the server
 * refuses to revert, so an edit is never thrown away.
 *
 * Privacy: the add-on keeps only the last 10 job ids and file names, plus at most one pending permission request, in
 * the user's own script properties. It never stores document content. The server URL below is a placeholder replaced
 * locally when the code is pasted into the editor; the repository does not hold the real value.
 */
var OCR_URL = '__OCR_URL__';
var OCR_VERSION = '0.1.4';
var PROP_JOBS = 'OCR_JOBS';
var PROP_PENDING = 'OCR_PENDING';
var MAX_JOBS = 10;
var PENDING_TTL_MS = 5 * 60 * 1000;
var PDF = 'application/pdf';

var OCR_ERRORS = {
  already_searchable: 'the PDF already has text on every page (use Flatten to redo it as a copy)',
  not_pdf: 'not a PDF',
  no_parents: 'could not confirm the file\'s folder, nothing was created',
  too_large: 'file is over the size limit',
  too_many_pages: 'too many pages',
  invalid_pdf: 'invalid or password-protected PDF',
  overlay_unavailable: 'the text layer cannot be added without re-encoding the pages, so the file was left alone (try Flatten)',
  file_changed: 'the file was edited in the meantime, so nothing was changed',
  nothing_to_revert: 'nothing to revert: this file was not OCR\'d by this add-on',
  revision_unavailable: 'the original version is not available, so nothing was changed',
  replace_failed: 'Drive did not accept the new content',
  drive_not_found: 'no access to the file: grant access and try again',
  drive_forbidden: 'no permission to edit this file',
  drive_auth: 'authorization expired, try again',
  drive_unreachable: 'Drive did not respond, try again',
  drive_error: 'Drive error, try again',
  created_elsewhere: 'Drive created the result in a different folder, please check',
  internal: 'internal error'
};

var OCR_STATUS = { queued: 'waiting in the queue', running: 'processing', done: 'done', error: 'failed' };
var OCR_DONE = { replace: 'done, file replaced', flatten: 'done, new copy created', revert: 'done, original restored' };

function onHomepage() {
  return [CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('OCR for Drive').setSubtitle('Version ' + OCR_VERSION))
    .addSection(CardService.newCardSection()
      .addWidget(CardService.newTextParagraph().setText('Select a scanned PDF in Drive and click Start OCR. The file is straightened if needed, double-page scans are split in half, and the file is replaced with a searchable version. You can revert it afterwards.'))
      .addWidget(CardService.newTextButton().setText('View OCR status')
        .setOnClickAction(CardService.newAction().setFunctionName('onOcrStatus'))))
    .build()];
}

/** True when the file is the result of an OCR by this add-on and has not been edited since. */
function isOurResult_(id) {
  try {
    var f = Drive.Files.get(id, { fields: 'md5Checksum,appProperties' });
    var p = f.appProperties || {};
    return !!(p.ocrPrevRev && p.ocrMd5 && p.ocrMd5 === f.md5Checksum);
  } catch (err) { return false; }
}

function loadPending_() {
  var raw = PropertiesService.getUserProperties().getProperty(PROP_PENDING);
  if (!raw) return null;
  var p = JSON.parse(raw);
  return (Date.now() - p.t < PENDING_TTL_MS) ? p : null;
}

function clearPending_() { PropertiesService.getUserProperties().deleteProperty(PROP_PENDING); }

/** Sends one job to the server. Returns a short message for the user. */
function startJob_(item, action) {
  var res = UrlFetchApp.fetch(OCR_URL + '/jobs', {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
    payload: JSON.stringify({ fileId: item.id, action: action, lang: 'por+eng' })
  });
  var code = res.getResponseCode();
  if (code === 429) throw new Error('usage limit reached, try again later');
  if (code === 503) throw new Error('the server queue is full, try again in a minute');
  if (code !== 202) throw new Error('server error (HTTP ' + code + ')');
  var props = PropertiesService.getUserProperties();
  var jobs = JSON.parse(props.getProperty(PROP_JOBS) || '[]');
  jobs.push({ jobId: JSON.parse(res.getContentText()).jobId, title: item.title, action: action, startedAt: Date.now() });
  props.setProperty(PROP_JOBS, JSON.stringify(jobs.slice(-MAX_JOBS)));
  return { replace: 'OCR started: ', flatten: 'Flatten started: ', revert: 'Revert started: ' }[action] + item.title;
}

/**
 * Contextual card. If a permission request was pending for a file and Drive now says we have access, the OCR starts
 * here, right after the user clicked Allow.
 */
function onDriveItemsSelected(e) {
  var items = ((e.drive && e.drive.selectedItems) || []).slice(0, 10);
  var pending = loadPending_();
  var started = [];
  if (pending) {
    items.forEach(function (it) {
      if (it.id === pending.id && it.addonHasFileScopePermission === true && it.mimeType === PDF) {
        clearPending_();
        try { started.push(startJob_(it, pending.action)); } catch (err) { started.push(String(err.message || err)); }
      }
    });
  }
  var card = CardService.newCardBuilder().setHeader(CardService.newCardHeader().setTitle('OCR for Drive').setSubtitle('Version ' + OCR_VERSION));
  var section = CardService.newCardSection();
  started.forEach(function (m) { section.addWidget(CardService.newTextParagraph().setText(m)); });
  var toStart = 0, toRevert = 0, pdfs = 0;
  items.forEach(function (it) {
    var note;
    if (it.mimeType !== PDF) { note = 'not a PDF'; }
    else {
      pdfs++;
      if (it.addonHasFileScopePermission !== true) { note = 'ready (will ask for permission)'; toStart++; }
      else if (isOurResult_(it.id)) { note = 'OCR done, can be reverted'; toRevert++; }
      else { note = 'ready'; toStart++; }
    }
    section.addWidget(CardService.newTextParagraph().setText(it.title + ': ' + note));
  });
  if (toStart) {
    section.addWidget(CardService.newTextButton().setText('Start OCR')
      .setOnClickAction(CardService.newAction().setFunctionName('onOcrStart')));
  }
  if (toRevert) {
    section.addWidget(CardService.newTextButton().setText('Revert OCR')
      .setOnClickAction(CardService.newAction().setFunctionName('onOcrRevert')));
  }
  if (pdfs) {
    section.addWidget(CardService.newTextButton().setText('Flatten and re-OCR (new copy)')
      .setOnClickAction(CardService.newAction().setFunctionName('onOcrFlatten')));
  }
  section.addWidget(CardService.newTextButton().setText('View OCR status')
    .setOnClickAction(CardService.newAction().setFunctionName('onOcrStatus')));
  card.addSection(section);
  return card.build();
}

function notify_(text) {
  return CardService.newActionResponseBuilder().setNotification(CardService.newNotification().setText(text)).build();
}

/**
 * Runs `action` on every selected PDF the add-on can already touch. For the first PDF without access it remembers
 * the intent and opens Drive's permission dialog (one file per click: several requestFileScope calls in one response
 * open a single dialog, for the last id only); the OCR then starts by itself once the user allows it.
 */
function runAction_(e, action) {
  var items = (e.drive && e.drive.selectedItems) || [];
  var messages = [], needAccess = null;
  items.forEach(function (it) {
    if (it.mimeType !== PDF) { messages.push('"' + it.title + '" is not a PDF'); return; }
    if (it.addonHasFileScopePermission !== true) { if (!needAccess) needAccess = it; return; }
    if (action === 'revert' && !isOurResult_(it.id)) { messages.push('"' + it.title + '" was not OCR\'d by this add-on'); return; }
    try { messages.push(startJob_(it, action)); } catch (err) { messages.push(String(err.message || err).slice(0, 140)); }
  });
  if (needAccess) {
    PropertiesService.getUserProperties().setProperty(PROP_PENDING, JSON.stringify({ id: needAccess.id, action: action, t: Date.now() }));
    return CardService.newDriveItemsSelectedActionResponseBuilder().requestFileScope(needAccess.id).build();
  }
  return notify_(messages.length ? messages.join('. ') : 'Select a PDF first.');
}

function onOcrStart(e) { return runAction_(e, 'replace'); }
function onOcrFlatten(e) { return runAction_(e, 'flatten'); }
function onOcrRevert(e) { return runAction_(e, 'revert'); }

function buildStatusCard_() {
  var jobs = JSON.parse(PropertiesService.getUserProperties().getProperty(PROP_JOBS) || '[]');
  var section = CardService.newCardSection();
  if (!jobs.length) section.addWidget(CardService.newTextParagraph().setText('No OCR jobs yet.'));
  jobs.slice().reverse().forEach(function (j) {
    var st;
    try {
      var res = UrlFetchApp.fetch(OCR_URL + '/jobs/' + j.jobId,
        { headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() }, muteHttpExceptions: true });
      st = res.getResponseCode() === 200 ? JSON.parse(res.getContentText())
        : { status: 'expired', note: 'no longer on the server' };
    } catch (err) { st = { status: 'unreachable', note: 'could not reach the server' }; }
    var label = st.note || (st.status === 'done' ? (OCR_DONE[st.action || j.action] || 'done') : (OCR_STATUS[st.status] || st.status));
    var line = j.title + ': ' + label;
    if (st.error) line += ' (' + (OCR_ERRORS[st.error] || st.error) + ')';
    if (st.status === 'done' && st.action === 'replace') {
      var extra = [];
      if (st.rotated) extra.push('rotated ' + st.rotated + '\u00b0');
      if (st.split) extra.push(st.split + (st.split === 1 ? ' page split' : ' pages split'));
      if (extra.length) line += ' (' + extra.join(', ') + ')';
    }
    if (st.pages) line += ', ' + st.pages + (st.pages === 1 ? ' page' : ' pages');
    if (st.run_s !== undefined) line += ', ' + st.run_s + ' s';
    section.addWidget(CardService.newTextParagraph().setText(line));
    if (st.result_id && st.status === 'done') {
      section.addWidget(CardService.newTextButton().setText('Open file')
        .setOpenLink(CardService.newOpenLink().setUrl('https://drive.google.com/file/d/' + st.result_id + '/view')));
    }
  });
  section.addWidget(CardService.newTextButton().setText('Refresh')
    .setOnClickAction(CardService.newAction().setFunctionName('onOcrStatusRefresh')));
  return CardService.newCardBuilder().setHeader(CardService.newCardHeader().setTitle('OCR status').setSubtitle('Version ' + OCR_VERSION)).addSection(section).build();
}

function onOcrStatus() {
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().pushCard(buildStatusCard_())).build();
}

function onOcrStatusRefresh() {
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().updateCard(buildStatusCard_())).build();
}

/**
 * Developer helper: run ONCE from the editor after switching the Cloud project or revoking access. It has no
 * try/catch on purpose, so the editor opens the consent screen for ALL manifest scopes (card callbacks catch their
 * errors and never trigger the request).
 */
function authorize() {
  UrlFetchApp.fetch('https://oauth2.googleapis.com/tokeninfo?access_token=' + encodeURIComponent(ScriptApp.getOAuthToken()), { muteHttpExceptions: true });
  var about = Drive.About.get({ fields: 'user(permissionId)' });  // advanced service: needs drive.file
  Logger.log('authorized, drive.file ok: ' + (about && about.user ? 'yes' : 'no'));
}
