/**
 * OCR for Drive: Google Drive add-on that makes scanned PDFs searchable.
 *
 * How it works: the card callback never carries the PDF (card callbacks stop at 30 s). It sends the file id to the OCR
 * server and returns at once. The credential is the user's own access token (scope drive.file) in the Authorization
 * header: the server checks that the token was issued for this add-on, identifies the user, downloads the PDF with
 * that token, runs the OCR and writes "<name> (OCR).pdf" into the original's folder. "View OCR status" polls the
 * server and only returns the user's own jobs.
 *
 * Privacy: the add-on keeps only the last 10 job ids and file names in the user's own script properties (needed for
 * the status card). It never stores document content. The server URL below is a placeholder replaced locally when the
 * code is pasted into the editor; the repository does not hold the real value.
 */
var OCR_URL = '__OCR_URL__';
var PROP_JOBS = 'OCR_JOBS';
var MAX_JOBS = 10;
var PDF = 'application/pdf';

var OCR_ERRORS = {
  already_searchable: 'the PDF already has text on every page',
  not_pdf: 'not a PDF',
  no_parents: 'could not confirm the file\'s folder, nothing was created',
  too_large: 'file is over the size limit',
  too_many_pages: 'too many pages',
  invalid_pdf: 'invalid or password-protected PDF',
  drive_not_found: 'no access to the file: grant access and try again',
  drive_auth: 'authorization expired, try again',
  drive_unreachable: 'Drive did not respond, try again',
  drive_error: 'Drive error, try again',
  created_elsewhere: 'Drive created the result in a different folder, please check',
  internal: 'internal error'
};

var OCR_STATUS = {
  queued: 'waiting in the queue',
  running: 'processing',
  done: 'done',
  error: 'failed'
};

function onHomepage() {
  return [CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('OCR for Drive'))
    .addSection(CardService.newCardSection()
      .addWidget(CardService.newTextParagraph().setText('Select a scanned PDF in Drive to make it searchable. A copy named "<name> (OCR).pdf" is saved next to the original.'))
      .addWidget(CardService.newTextButton().setText('View OCR status')
        .setOnClickAction(CardService.newAction().setFunctionName('onOcrStatus'))))
    .build()];
}

/** Contextual card for the selected Drive items. */
function onDriveItemsSelected(e) {
  var items = (e.drive && e.drive.selectedItems) || [];
  var card = CardService.newCardBuilder().setHeader(CardService.newCardHeader().setTitle('OCR for Drive'));
  var section = CardService.newCardSection();
  var needAccess = [];
  var ready = 0;
  items.forEach(function (it) {
    var note;
    if (it.mimeType !== PDF) note = 'not a PDF';
    else if (!it.addonHasFileScopePermission) { note = 'needs your permission'; needAccess.push(it); }
    else { note = 'ready'; ready++; }
    section.addWidget(CardService.newTextParagraph().setText(it.title + ': ' + note));
  });
  if (needAccess.length) {
    // One file per click: several requestFileScope calls in one response open a single dialog, for the last id only.
    var first = needAccess[0];
    section.addWidget(CardService.newTextButton().setText('Grant access to "' + first.title + '"')
      .setOnClickAction(CardService.newAction().setFunctionName('onRequestAccess').setParameters({ id: first.id })));
  }
  if (ready) {
    section.addWidget(CardService.newTextButton().setText('Start OCR')
      .setOnClickAction(CardService.newAction().setFunctionName('onOcrStart')));
  }
  section.addWidget(CardService.newTextButton().setText('View OCR status')
    .setOnClickAction(CardService.newAction().setFunctionName('onOcrStatus')));
  card.addSection(section);
  return card.build();
}

/** Asks Drive for access to one file (the per-file drive.file grant). */
function onRequestAccess(e) {
  return CardService.newDriveItemsSelectedActionResponseBuilder().requestFileScope(e.parameters.id).build();
}

function notify_(text) {
  return CardService.newActionResponseBuilder().setNotification(CardService.newNotification().setText(text)).build();
}

function onOcrStart(e) {
  var items = (e.drive && e.drive.selectedItems) || [];
  var props = PropertiesService.getUserProperties();
  var jobs = JSON.parse(props.getProperty(PROP_JOBS) || '[]');
  var messages = [];
  items.forEach(function (it) {
    try {
      if (it.mimeType !== PDF) throw new Error('"' + it.title + '" is not a PDF');
      if (it.addonHasFileScopePermission !== true) throw new Error('grant access to "' + it.title + '" first');
      var res = UrlFetchApp.fetch(OCR_URL + '/jobs', {
        method: 'post', contentType: 'application/json', muteHttpExceptions: true,
        headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
        payload: JSON.stringify({ fileId: it.id, lang: 'por+eng' })
      });
      var code = res.getResponseCode();
      if (code === 429) throw new Error('usage limit reached, try again later');
      if (code === 503) throw new Error('the server queue is full, try again in a minute');
      if (code !== 202) throw new Error('server error (HTTP ' + code + ')');
      jobs.push({ jobId: JSON.parse(res.getContentText()).jobId, title: it.title, startedAt: Date.now() });
      messages.push('Started: ' + it.title);
    } catch (err) {
      messages.push(String(err.message || err).slice(0, 140));
    }
  });
  props.setProperty(PROP_JOBS, JSON.stringify(jobs.slice(-MAX_JOBS)));
  return notify_(messages.length ? messages.join('. ') : 'Select a PDF first.');
}

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
    var label = st.note || OCR_STATUS[st.status] || st.status;
    var line = j.title + ': ' + label;
    if (st.error) line += ' (' + (OCR_ERRORS[st.error] || st.error) + ')';
    if (st.pages) line += ', ' + st.pages + (st.pages === 1 ? ' page' : ' pages');
    if (st.run_s !== undefined) line += ', ' + st.run_s + ' s';
    section.addWidget(CardService.newTextParagraph().setText(line));
    if (st.result_id) {
      section.addWidget(CardService.newTextButton().setText('Open result')
        .setOpenLink(CardService.newOpenLink().setUrl('https://drive.google.com/file/d/' + st.result_id + '/view')));
    }
  });
  section.addWidget(CardService.newTextButton().setText('Refresh')
    .setOnClickAction(CardService.newAction().setFunctionName('onOcrStatusRefresh')));
  return CardService.newCardBuilder().setHeader(CardService.newCardHeader().setTitle('OCR status')).addSection(section).build();
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
