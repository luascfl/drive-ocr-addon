// Makes a scanned PDF searchable by writing invisible text (render mode 3) over the ORIGINAL PDF.
// The original images are not re-encoded: the file grows only by the text (the approach used by OCRmyPDF and Scribe.js).
//
// Font: "GlyphLessFont", the same one Tesseract 5.5.0 writes in its PDFs (the 572 bytes of TTF below were
// extracted from a PDF it generated on this machine; Tesseract is Apache-2.0). It has 2 empty glyphs and a
// fixed advance of 0.5 em. Because it is Type0/Identity-H with a CIDToGIDMap sending every code to the same glyph
// and a ToUnicode that maps the code to itself, the 2-byte character code IS the Unicode code point (UTF-16): any
// Basic Multilingual Plane character passes without a glyph table and without Helvetica's WinAnsi limitation.
//
// Structure that text extractors group well (copied from Tesseract's renderer):
// - one text object (BT...ET) per PARAGRAPH, with lines chained by Td;
// - each word ends with an explicit space and gets a Tz such that "word + space" spans exactly the
//   distance to the start of the next word (no gaps or overlaps between glyphs);
// - one font size per line, equal to the line spacing (Poppler groups lines by comparing the two).
//
// PDFLib is injected (ESM module of pdf-lib or of the @cantoo/pdf-lib fork, via CDN or npm), so the file does not
// depend on a bundler.
// Known limits:
// - Characters outside the basic plane (UTF-16 surrogate pairs) have not been verified in all extractors.
// - Slanted text is positioned horizontally; baseline drift goes into each Td.
// - Pages that already have visible or invisible text receive a second layer; decide beforehand whether to skip them.

const ROTATIONS = [0, 90, 180, 270];

// Few decimal places suffice: the text is invisible and 0.01 pt is far less than one pixel at 300 DPI (0.24 pt).
// Long floating-point numbers bloat the content stream.
const r2 = (x) => Math.round(x * 100) / 100;
const r4 = (x) => Math.round(x * 10000) / 10000;

// Letters of right-to-left scripts: Hebrew, Arabic, Syriac, Thaana and presentation forms.
const RTL_LETTER = /[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]/;

/** Direction of a word: RTL if it has an RTL letter; LTR if it has a letter of another script; otherwise inherits the line's. */
function wordIsRtl(text, inherit) {
  const t = (text || '').replace(/[\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, '');
  if (RTL_LETTER.test(t)) return true;
  if (/\p{L}/u.test(t)) return false;
  return inherit;
}

const GLYPHLESS_TTF_B64 = 'AAEAAAAKAIAAAwAgT1MvMlbeyJQAAAEoAAAAYGNtYXAACgA0AAABkAAAAB5nbHlmFSJBJAAAAbgAAAAYaGVhZAt48WUAAACsAAAANmhoZWEMAgQCAAAA5AAAACRobXR4BAAAAAAAAYgAAAAIbG9jYQAMAAAAAAGwAAAABm1heHAABAAFAAABCAAAACBuYW1l8usW2gAAAdAAAABLcG9zdAABAAEAAAIcAAAAIAABAAAAAQAAsJRxEF8PPPUEBwgAAAAAAM+a/G4AAAAA1MOn8gAAAAAEAAgAAAAAEAACAAAAAAAAAAEAAAgA//8AAAQAAAAAAAQAAAEAAAAAAAAAAAAAAAAAAAACAAEAAAACAAQAAQAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAwAAAZAABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUAAQABAAAAAAAAAAAAAAAAAAAAAAAAAAAAR09PRwBAAAAAAAAB//8AAAABAAGAAAAAAAAAAAAAAAAAAAABAAAAAAAABAAAAAAAAAIAAQAAAAAAFAADAAAAAAAUAAYACgAAAAAAAAAAAAAAAAAMAAAAAQAAAAAEAAgAAAMAADEhESEEAPwACAAAAAADACoAAAADAAAABQAWAAAAAQAAAAAABQALABYAAwABBAkABQAWAAAAVgBlAHIAcwBpAG8AbgAgADEALgAwVmVyc2lvbiAxLjAAAAEAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAA=';

// Same CMap as Tesseract: the 2-byte code maps to the same code point.
const TOUNICODE_CMAP = `/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo
<<
  /Registry (Adobe)
  /Ordering (UCS)
  /Supplement 0
>> def
/CMapName /Adobe-Identify-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
1 beginbfrange
<0000> <FFFF> <0000>
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end
`;

function base64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Creates the font objects in the document and returns the reference to the Type0 dictionary. */
function embedGlyphlessFont(PDFLib, doc) {
  const ctx = doc.context;
  const ttf = base64ToBytes(GLYPHLESS_TTF_B64);
  const fontFile = ctx.register(ctx.flateStream(ttf, { Length1: ttf.length }));
  const descriptor = ctx.register(ctx.obj({
    Type: 'FontDescriptor', FontName: 'GlyphLessFont', Flags: 5, FontBBox: [0, 0, 500, 1000],
    ItalicAngle: 0, Ascent: 1000, Descent: -1, CapHeight: 1000, StemV: 80, FontFile2: fontFile,
  }));
  // 65,536 two-byte entries, all = glyph 1
  const cidToGid = new Uint8Array(131072);
  for (let i = 1; i < cidToGid.length; i += 2) cidToGid[i] = 1;
  const cidToGidRef = ctx.register(ctx.flateStream(cidToGid));
  const cidFont = ctx.register(ctx.obj({
    Type: 'Font', Subtype: 'CIDFontType2', BaseFont: 'GlyphLessFont',
    CIDSystemInfo: { Registry: PDFLib.PDFString.of('Adobe'), Ordering: PDFLib.PDFString.of('Identity'), Supplement: 0 },
    FontDescriptor: descriptor, CIDToGIDMap: cidToGidRef, DW: 500,
  }));
  const toUnicode = ctx.register(ctx.flateStream(new TextEncoder().encode(TOUNICODE_CMAP)));
  return ctx.register(ctx.obj({
    Type: 'Font', Subtype: 'Type0', BaseFont: 'GlyphLessFont', Encoding: 'Identity-H',
    DescendantFonts: [cidFont], ToUnicode: toUnicode,
  }));
}

/** Normalizes an OCR word: no control characters, collapsed spaces, no leading space. */
function cleanWord(text) {
  return text.replace(/[\u0000-\u001F\u007F]/g, ' ').replace(/\s+/g, ' ').replace(/^ /, '');
}

/** 2-byte code = UTF-16 unit, as a hexadecimal string. */
function toHex(text) {
  let h = '';
  for (let i = 0; i < text.length; i++) h += text.charCodeAt(i).toString(16).padStart(4, '0');
  return h;
}

/**
 * Converts a point of the DISPLAYED page (points, origin at top-left, y down)
 * to the PDF user space, accounting for CropBox and /Rotate.
 */
function toUser(rot, crop, u, v) {
  const rx = rot === 0 ? u : rot === 90 ? v : rot === 180 ? crop.width - u : crop.width - v;
  const ry = rot === 0 ? crop.height - v : rot === 90 ? u : rot === 180 ? v : crop.height - u;
  return [crop.x + rx, crop.y + ry];
}

/**
 * @param {object} PDFLib pdf-lib module
 * @param {object} p
 * @param {Uint8Array} p.pdfBytes original PDF
 * @param {{
 *   pageIndex: number,  page index, 0-based
 *   widthPx: number, heightPx: number,  size of the image used for OCR (the displayed page, already with /Rotate)
 *   words: {text:string, bbox:{x0:number,y0:number,x1:number,y1:number},
 *           baselineY?:number, heightPx?:number, fontSizePx?:number, lineId?:number, paraId?:number, rtl?:boolean}[]  in reading order; see wordsFromBlocks
 * }[]} p.pages pages with OCR
 * @returns {Promise<Uint8Array>}
 */
export async function addTextLayer(PDFLib, { pdfBytes, pages }) {
  if (!(pdfBytes instanceof Uint8Array) || pdfBytes.length === 0) throw new Error('pdfBytes must be a non-empty Uint8Array');
  if (!Array.isArray(pages) || pages.length === 0) throw new Error('pages must have at least one page');
  const { PDFDocument, PDFHexString, beginText, endText, setFontAndSize, setTextMatrix, showText, moveText,
    setTextRenderingMode, TextRenderingMode, setCharacterSqueeze, pushGraphicsState, popGraphicsState } = PDFLib;

  const doc = await PDFDocument.load(pdfBytes);
  const fontRef = embedGlyphlessFont(PDFLib, doc);
  const docPages = doc.getPages();

  for (const p of pages) {
    const page = docPages[p.pageIndex];
    if (!page) throw new Error(`page ${p.pageIndex} does not exist (the PDF has ${docPages.length})`);
    const rot = ((page.getRotation().angle % 360) + 360) % 360;
    if (!ROTATIONS.includes(rot)) throw new Error(`/Rotate ${rot} not supported on page ${p.pageIndex}`);
    const crop = page.getCropBox();
    const dispW = rot % 180 === 0 ? crop.width : crop.height;
    const dispH = rot % 180 === 0 ? crop.height : crop.width;
    const sx = dispW / p.widthPx;
    const sy = dispH / p.heightPx;
    const fontKey = page.node.newFontDictionary('GlyphLessFont', fontRef);
    // text space rotates with the page; Td offsets stay in that frame
    const psi = (rot * Math.PI) / 180;
    const cs = Math.cos(psi);
    const sn = Math.sin(psi);

    const ws = p.words;
    const ops = [pushGraphicsState()];
    let open = null; // BT in progress: { para, lastUx, lastUy, lastSize }
    const close = () => { if (open) { ops.push(endText()); open = null; } };
    for (let i = 0; i < ws.length; i++) {
      const w = ws[i];
      const word = cleanWord(w.text || '');
      if (!word) continue;
      const { x0, y0, x1, y1 } = w.bbox;
      const hPx = w.heightPx !== undefined ? w.heightPx : y1 - y0;
      const wPt = (x1 - x0) * sx;
      if (!(wPt > 0) || !(hPx > 0)) continue;
      // font size: the line's (line pitch, see wordsFromBlocks); without it, the word height
      const size = Math.max(1, (w.fontSizePx !== undefined ? w.fontSizePx : hPx) * sy);
      const lineId = w.lineId !== undefined ? w.lineId : `solo${i}`;
      const para = w.paraId !== undefined ? w.paraId : lineId;
      // the next word on the SAME line defines the pitch; the last one on the line uses its own width
      const next = ws[i + 1];
      const sameLine = next && (next.lineId !== undefined ? next.lineId : `solo${i + 1}`) === lineId;
      const withSpace = word + ' ';
      // fixed advance of 0.5 em per UTF-16 unit
      const natural = 0.5 * size * withSpace.length;
      const naturalWord = 0.5 * size * word.length;
      const rtl = w.rtl === true;
      // anchor = edge where the text STARTS: left in LTR and right in RTL (mirrored text matrix)
      const anchorPx = rtl ? x1 : x0;
      // the pitch only applies up to the next word of the SAME direction; on a direction change the word's own width is used
      const chained = sameLine && (next.rtl === true) === rtl;
      const pitch = chained ? (rtl ? x1 - next.bbox.x1 : next.bbox.x0 - x0) * sx : 0;
      const squeeze = chained && pitch > 0 ? (pitch / natural) * 100 : (wPt / naturalWord) * 100;

      const baseY = w.baselineY !== undefined ? w.baselineY : y1 - (y1 - y0) * 0.2;
      const [ux, uy] = toUser(rot, crop, anchorPx * sx, baseY * sy);
      // direction of the text-space x axis: +1 in LTR, -1 in RTL
      const k = rtl ? -1 : 1;
      if (!open || open.para !== para) {
        close();
        ops.push(beginText(), setTextRenderingMode(TextRenderingMode.Invisible), setFontAndSize(fontKey, size),
          setTextMatrix(r4(k * cs), r4(k * sn), r4(-sn), r4(cs), r2(ux), r2(uy)));
        open = { para, rtl, lastUx: r2(ux), lastUy: r2(uy), lastSize: size };
      } else if (open.rtl !== rtl) {
        // direction change within the paragraph: new absolute Tm in the same BT, like Tesseract
        if (Math.abs(size - open.lastSize) > 1e-6) { ops.push(setFontAndSize(fontKey, size)); open.lastSize = size; }
        ops.push(setTextMatrix(r4(k * cs), r4(k * sn), r4(-sn), r4(cs), r2(ux), r2(uy)));
        open.rtl = rtl;
        open.lastUx = r2(ux);
        open.lastUy = r2(uy);
      } else {
        // Td is relative to the start of the previous word; (ux-lastUx, uy-lastUy) is in user space
        const dux = ux - open.lastUx;
        const duy = uy - open.lastUy;
        if (Math.abs(size - open.lastSize) > 1e-6) { ops.push(setFontAndSize(fontKey, size)); open.lastSize = size; }
        // position actually emitted (rounded), so the error does not accumulate along the line
        const tdx = r2(k * (dux * cs + duy * sn));
        const tdy = r2(-dux * sn + duy * cs);
        ops.push(moveText(tdx, tdy));
        open.lastUx += k * tdx * cs - tdy * sn;
        open.lastUy += k * tdx * sn + tdy * cs;
      }
      ops.push(setCharacterSqueeze(r2(squeeze)), showText(PDFHexString.of(toHex(withSpace))));
    }
    close();
    ops.push(popGraphicsState());
    page.pushOperators(...ops);
  }
  return await doc.save();
}

/**
 * Flattens Tesseract.js (v5) `blocks` into words, in OCR reading order.
 * Per word: `paraId` and `lineId` identify paragraph and line; `heightPx` is the median height of the word boxes
 * on the line; `baselineY` is the baseline at the start of the word, interpolated from the baseline
 * fitted by OCR; `fontSizePx` is the font size of the line.
 *
 * `fontSizePx` does NOT come from the word height: it is the median pitch between consecutive lines of the same
 * paragraph on the page, limited to [1.0 ; 2.0] times the line height (large titles, double-spaced pages).
 * Reason, measured on a two-column scan: Poppler groups lines into blocks by comparing the font size with
 * the line spacing. With 0.8 times the word height (5.2 pt for lines at 9.1 pt) it split the
 * page into 190 flows and reading order dropped to 0.40; with the line pitch (9.1 pt, what native Tesseract
 * does) there were 6 flows and 0.93.
 *
 * `rtl` is the direction of the word, decided as in Tesseract's renderer: a word with a letter of an RTL script
 * (Arabic, Hebrew) is written with the text matrix mirrored, anchored at the right edge; a word with a letter of
 * another script (Latin inside an RTL line) uses the normal matrix, anchored at the left edge; a lone number or
 * punctuation inherits the line direction, which comes from the horizontal order of the words.
 */
export function wordsFromBlocks(blocks) {
  const lines = [];
  let lineId = 0;
  let paraId = 0;
  for (const b of blocks || []) {
    for (const para of b.paragraphs || []) {
      let usedPara = false;
      for (const line of para.lines || []) {
        const words = line.words || [];
        if (words.length === 0) continue;
        if (!usedPara) { paraId += 1; usedPara = true; }
        lineId += 1;
        const hs = words.map((w) => w.bbox.y1 - w.bbox.y0).sort((m, n) => m - n);
        const heightPx = hs[Math.floor(hs.length / 2)];
        const bl = line.baseline;
        const hasBl = bl && bl.has_baseline !== false && bl.x1 !== bl.x0;
        const slope = hasBl ? (bl.y1 - bl.y0) / (bl.x1 - bl.x0) : 0;
        // direction: Tesseract.js returns words in LOGICAL order; on an RTL line each word sits to the left of the previous one
        let dec = 0;
        let inc = 0;
        for (let k = 1; k < words.length; k++) {
          if (words[k].bbox.x0 < words[k - 1].bbox.x0) dec++; else if (words[k].bbox.x0 > words[k - 1].bbox.x0) inc++;
        }
        const lineRtl = dec + inc > 0 ? dec > inc : RTL_LETTER.test(words[0].text || '');
        const first = words[0];
        const ax = wordIsRtl(first.text, lineRtl) ? first.bbox.x1 : first.bbox.x0;
        const firstBase = hasBl ? bl.y0 + (ax - bl.x0) * slope : first.bbox.y1 - 0.2 * (first.bbox.y1 - first.bbox.y0);
        lines.push({ lineId, paraId, heightPx, firstBase, hasBl, bl, slope, words, rtl: lineRtl });
      }
    }
  }
  // pitches between consecutive lines of the same paragraph
  const pitches = [];
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].paraId !== lines[i - 1].paraId) continue;
    const d = lines[i].firstBase - lines[i - 1].firstBase;
    if (d > 0) pitches.push(d);
  }
  pitches.sort((m, n) => m - n);
  const pagePitch = pitches.length ? pitches[Math.floor(pitches.length / 2)] : null;

  const out = [];
  for (const L of lines) {
    const base = pagePitch === null ? L.heightPx : pagePitch;
    const fontSizePx = Math.min(2 * L.heightPx, Math.max(L.heightPx, base));
    for (const w of L.words) {
      const rtl = wordIsRtl(w.text, L.rtl);
      const ax = rtl ? w.bbox.x1 : w.bbox.x0;
      const baselineY = L.hasBl ? L.bl.y0 + (ax - L.bl.x0) * L.slope : undefined;
      out.push({ text: w.text, bbox: w.bbox, baselineY, heightPx: L.heightPx, fontSizePx, lineId: L.lineId, paraId: L.paraId, rtl });
    }
  }
  return out;
}
