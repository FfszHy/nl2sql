import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { buildChartOption } from '../src/chart.ts';

const columns = ['city', 'cinema', 'revenue', 'share'];
const rows = [['北京', '影院 A', '123.45', 0.7], ['上海', '影院 B', 0, 0.3]];
const config = (changes = {}) => ({
  version: 1, type: 'bar', category: ['city'], series: [{ field: 'revenue' }], ...changes
});

test('bar binds composite labels and dual axes from query rows without mutating inputs', () => {
  const input = config({ category: ['city', 'cinema'], series: [
    { field: 'revenue', name: '收入' }, { field: 'share', name: '占比', axis: 'secondary' }
  ] });
  const before = JSON.stringify({ input, rows, columns });
  const option = buildChartOption(input, rows, columns);
  assert.deepEqual(option.xAxis.data, ['北京 / 影院 A', '上海 / 影院 B']);
  assert.deepEqual(option.series[0].data, [123.45, 0]);
  assert.deepEqual(option.series[1].data, [0.7, 0.3]);
  assert.equal(option.series[1].yAxisIndex, 1);
  assert.equal(option.yAxis[1].position, 'right');
  assert.equal(option.tooltip.renderMode, 'richText');
  assert.equal(JSON.stringify({ input, rows, columns }), before);
});

test('horizontal bar binds numeric axes horizontally', () => {
  const option = buildChartOption(config({ orientation: 'horizontal', series: [{ field: 'revenue', axis: 'secondary' }] }), rows, columns);
  assert.deepEqual(option.yAxis.data, ['北京', '上海']);
  assert.equal(option.xAxis[1].position, 'top');
  assert.equal(option.series[0].xAxisIndex, 1);
});

test('line preserves negative values and gaps instead of coercing missing data to zero', () => {
  const values = [0, null, '', false, ' 12.5 ', '-3', '1e2', 'Infinity', '1e309', '0x10', {}, NaN];
  const option = buildChartOption(config({ type: 'line' }), values.map((value, i) => [`row${i}`, null, value]), columns);
  assert.deepEqual(option.series[0].data, [0, null, null, null, 12.5, -3, 100, null, null, null, null, null]);
  assert.equal(option.series[0].type, 'line');
});

test('pie binds labels and values; refuses negative shares and does not show fake equal slices for zeros', () => {
  const option = buildChartOption(config({ type: 'pie' }), rows, columns);
  assert.deepEqual(option.series[0].data, [{ name: '北京', value: 123.45 }, { name: '上海', value: 0 }]);
  assert.equal(option.series[0].stillShowZeroSum, false);
  assert.equal(buildChartOption(config({ type: 'pie' }), [['北京', null, -1]], columns), null);
});

test('scatter skips missing x/y pairs and binds decimal strings', () => {
  const input = config({ type: 'scatter', category: ['share'] });
  const option = buildChartOption(input, [...rows, ['广州', 'C', null, 0.4]], columns);
  assert.deepEqual(option.series[0].data, [[0.7, 123.45], [0.3, 0]]);
  assert.equal(buildChartOption(input, [['北京', 'A', 1, null]], columns), null);
});

test('plain database text is kept as canvas text, never HTML options', () => {
  const label = '<img src=x onerror="globalThis.chartAttack=true">';
  const option = buildChartOption(config({ title: label, series: [{ field: 'revenue', name: label }] }), [[label, 'A', 1]], columns);
  assert.equal(option.title.text, label);
  assert.equal(option.xAxis.data[0], label);
  assert.equal(option.tooltip.renderMode, 'richText');
  assert.equal(option.series[0].name, label);
  assert.equal(option.tooltip.formatter, undefined);
  assert.equal(globalThis.chartAttack, undefined);
});

test('Unicode title/name limits count codepoints consistently with the backend', () => {
  assert.notEqual(buildChartOption(config({ title: '😀'.repeat(200), series: [{ field: 'revenue', name: '😀'.repeat(100) }] }), rows, columns), null);
  assert.equal(buildChartOption(config({ title: '😀'.repeat(201) }), rows, columns), null);
  assert.equal(buildChartOption(config({ title: '\uD800' }), rows, columns), null);
  assert.equal(buildChartOption(config({ series: [{ field: 'revenue', name: '\uDC00' }] }), rows, columns), null);
});

test('JavaScript source and option-like JSON are rejected without executing', () => {
  const payload = '({ series: [], value: (globalThis.chartAttack = true) })';
  assert.equal(buildChartOption(payload, rows, columns), null);
  assert.equal(buildChartOption({ series: [{ type: 'bar', data: [1] }], tooltip: { formatter: payload } }, rows, columns), null);
  assert.equal(buildChartOption(config({ formatter: payload }), rows, columns), null);
  assert.equal(globalThis.chartAttack, undefined);
});

const invalidConfigs = [
  null, [], {}, config({ version: true }), config({ version: 2 }), config({ type: ['bar'] }),
  config({ type: 'custom' }), config({ title: { text: 'x', link: 'javascript:alert(1)' } }),
  config({ title: 'x'.repeat(201) }), config({ category: [] }), config({ category: ['unknown'] }),
  config({ category: ['city', 'city'] }), config({ category: ['city', 'cinema', 'revenue', 'share'] }),
  config({ series: [] }), config({ series: Array(9).fill({ field: 'revenue' }) }),
  config({ series: [{ field: 'unknown' }] }), config({ series: [{ field: 'revenue', name: false }] }),
  config({ series: [{ field: 'revenue', name: 'x'.repeat(101) }] }),
  config({ series: [{ field: 'revenue', formatter: 'alert(1)' }] }),
  config({ series: [{ field: 'revenue', axis: 'third' }] }), config({ orientation: 'diagonal' }),
  config({ type: 'line', orientation: 'vertical' }), config({ type: 'pie', category: ['city', 'cinema'] }),
  config({ type: 'pie', series: [{ field: 'revenue' }, { field: 'share' }] }),
  config({ type: 'scatter', series: [{ field: 'revenue', axis: 'secondary' }] }),
  config({ tooltip: { renderMode: 'html' } }), config({ graphic: { type: 'image', style: { image: 'https://example.invalid/x' } } }),
  JSON.parse('{"version":1,"type":"bar","category":["city"],"series":[{"field":"revenue"}],"__proto__":{"polluted":true}}')
];

test('schema allowlist rejects unsupported keys, structures, enums, versions and oversized names', () => {
  for (const [index, input] of invalidConfigs.entries()) {
    assert.equal(buildChartOption(input, rows, columns), null, `invalid schema #${index}`);
  }
  assert.equal({}.polluted, undefined);
});

test('ambiguous referenced column names are rejected; unreferenced duplicates are harmless', () => {
  assert.equal(buildChartOption(config(), [['北京', 1, 2]], ['city', 'revenue', 'revenue']), null);
  assert.notEqual(buildChartOption(config(), [['北京', 1, 2, 3]], ['city', 'revenue', 'unused', 'unused']), null);
});

test('unusual SQL column names bind by array index, without object property assignment', () => {
  const option = buildChartOption(config({ category: ['__proto__'], series: [{ field: 'constructor' }] }), [['标签', 2]], ['__proto__', 'constructor']);
  assert.deepEqual(option.xAxis.data, ['标签']);
  assert.deepEqual(option.series[0].data, [2]);
});

test('malformed rows and nonnumeric series cannot silently produce a misleading chart', () => {
  assert.equal(buildChartOption(config(), [{ city: '北京', revenue: 1 }], columns), null);
  assert.equal(buildChartOption(config(), [['北京', 'A', false]], columns), null);
  assert.equal(buildChartOption(config(), [[{}, 'A', 1]], columns), null);
  assert.equal(buildChartOption(config(), rows, [1, 2]), null);
});

test('model code execution entry points stay absent from frontend source', () => {
  for (const filename of ['App.tsx', 'chart.ts']) {
    const source = readFileSync(new URL(`../src/${filename}`, import.meta.url), 'utf8');
    assert.doesNotMatch(source, /\bnew\s+Function\s*\(|\beval\s*\(|dangerouslySetInnerHTML/);
  }
});
