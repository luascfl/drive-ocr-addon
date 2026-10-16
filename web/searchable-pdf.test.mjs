// Run with: bun test web/
// Covers only the pure logic of wordsFromBlocks (per-word direction and font size). addTextLayer depends on
// pdf-lib and on PDF extractors; its evidence is in .context/docs/us004-evidence.md and us005-evidence.md.
import { describe, expect, test } from 'bun:test';
import { wordsFromBlocks } from './searchable-pdf.mjs';

const W = (text, x0, x1) => ({ text, bbox: { x0, y0: 0, x1, y1: 20 } });
const L = (words, base = null) => ({ words, baseline: base });
const blocks = (...lines) => [{ paragraphs: [{ lines }] }];
const dirs = (b) => wordsFromBlocks(b).map((w) => `${w.text}:${w.rtl ? 'R' : 'L'}`).join(' ');

describe('per-word direction', () => {
  test('LTR line: no word is RTL', () => {
    expect(dirs(blocks(L([W('a', 0, 10), W('b', 20, 30)])))).toBe('a:L b:L');
  });

  test('RTL line in logical order (decreasing x): all RTL', () => {
    expect(dirs(blocks(L([W('שלום', 200, 290), W('עולם', 100, 190)])))).toBe('שלום:R עולם:R');
  });

  test('Latin inside an RTL line is LTR, and a number between RTL words inherits the line direction', () => {
    const line = L([W('שלום', 900, 990), W('Maria', 700, 780), W('Silva', 790, 860), W('2024', 600, 680), W('עולם', 500, 590)]);
    expect(dirs(blocks(line))).toBe('שלום:R Maria:L Silva:L 2024:R עולם:R');
  });

  test('Hebrew word in an LTR line is RTL', () => {
    expect(dirs(blocks(L([W('hello', 0, 80), W('שלום', 100, 180), W('world', 200, 280)])))).toBe('hello:L שלום:R world:L');
  });

  test('a lone number on a one-word line is LTR; a lone RTL word is RTL', () => {
    expect(dirs(blocks(L([W('2024', 0, 90)])))).toBe('2024:L');
    expect(dirs(blocks(L([W('שלום', 0, 90)])))).toBe('שלום:R');
  });

  test('LRM and RLM direction marks around Latin do not change the classification', () => {
    expect(dirs(blocks(L([W('\u200eexample.com\u200f', 0, 80)])))).toBe('\u200eexample.com\u200f:L');
  });
});

describe('font size from line pitch', () => {
  const base = (y) => ({ x0: 0, y0: y, x1: 10, y1: y, has_baseline: true });
  const size = (b) => wordsFromBlocks(b).map((w) => w.fontSizePx);

  test('pitch within range becomes the size', () => {
    expect(size(blocks(L([W('a', 0, 10)], base(16)), L([W('b', 0, 10)], base(46)), L([W('c', 0, 10)], base(76))))).toEqual([30, 30, 30]);
  });

  test('large pitch is capped at 2 times the line height', () => {
    expect(size(blocks(L([W('a', 0, 10)], base(16)), L([W('b', 0, 10)], base(116))))).toEqual([40, 40]);
  });

  test('with no line pair in the same paragraph, uses the line height', () => {
    expect(size(blocks(L([W('a', 0, 10)], base(16))))).toEqual([20]);
  });

  test('empty inputs do not break', () => {
    expect(wordsFromBlocks([])).toEqual([]);
    expect(wordsFromBlocks(undefined)).toEqual([]);
    expect(wordsFromBlocks(blocks({ words: [] }))).toEqual([]);
  });
});
