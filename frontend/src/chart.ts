import type { EChartsOption } from 'echarts';

type ChartType = 'bar' | 'line' | 'pie' | 'scatter';
type ChartSeries = { field: string; name: string; axis: 'primary' | 'secondary' };
type ChartConfig = {
  version: 1;
  type: ChartType;
  title?: string;
  category: string[];
  series: ChartSeries[];
  orientation?: 'vertical' | 'horizontal';
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.getPrototypeOf(value) === Object.prototype;

const hasOnlyKeys = (value: Record<string, unknown>, keys: string[]) =>
  Object.keys(value).every((key) => keys.includes(key));

const isShortText = (value: unknown, maxLength: number): value is string => {
  if (typeof value !== 'string') return false;
  const characters = [...value];
  return characters.length <= maxLength && characters.every((character) => {
    const point = character.codePointAt(0)!;
    return point < 0xD800 || point > 0xDFFF;
  });
};

// Treat model output as data. Never forward arbitrary ECharts options or code.
const validateChartConfig = (value: unknown, columns: readonly string[]): ChartConfig | null => {
  if (!isRecord(value) || !hasOnlyKeys(value, ['version', 'type', 'title', 'category', 'series', 'orientation'])) return null;
  if (value.version !== 1 || typeof value.type !== 'string' || !['bar', 'line', 'pie', 'scatter'].includes(value.type)) return null;
  if (value.title !== undefined && !isShortText(value.title, 200)) return null;
  const category = value.category;
  if (!Array.isArray(category) || category.length < 1 || category.length > 3) return null;
  const isColumn = (field: unknown): field is string =>
    isShortText(field, 16_000) && columns.filter((column) => column === field).length === 1;
  if (!category.every(isColumn) || new Set(category).size !== category.length) return null;
  if (!Array.isArray(value.series) || value.series.length < 1 || value.series.length > 8) return null;
  const series: ChartSeries[] = [];
  for (const item of value.series) {
    if (!isRecord(item) || !hasOnlyKeys(item, ['field', 'name', 'axis']) || !isColumn(item.field)) return null;
    if (item.name !== undefined && !isShortText(item.name, 100)) return null;
    if (item.axis !== undefined && item.axis !== 'primary' && item.axis !== 'secondary') return null;
    series.push({ field: item.field, name: item.name === undefined ? item.field : item.name as string, axis: item.axis ?? 'primary' });
  }
  const type = value.type as ChartType;
  if (type !== 'bar' && value.orientation !== undefined) return null;
  if (value.orientation !== undefined && value.orientation !== 'vertical' && value.orientation !== 'horizontal') return null;
  if ((type === 'pie' || type === 'scatter')
    && (category.length !== 1 || series.length !== 1 || series[0].axis !== 'primary')) return null;
  return {
    version: 1, type, category: [...category], series,
    ...(value.title === undefined ? {} : { title: value.title as string }),
    ...(type === 'bar' ? { orientation: value.orientation ?? 'vertical' } : {})
  };
};

const asNumber = (value: unknown): number | null => {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value !== 'string' || !/^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim())) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

const asLabel = (value: unknown): string | null => {
  if (value === null || value === undefined) return 'NULL';
  if (typeof value === 'string' || typeof value === 'boolean') return String(value);
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return null;
};

export const buildChartOption = (
  rawConfig: unknown,
  rows: readonly unknown[][] = [],
  columns: readonly string[] = []
): EChartsOption | null => {
  if (!Array.isArray(columns) || !columns.every((column) => typeof column === 'string')) return null;
  const config = validateChartConfig(rawConfig, columns);
  if (!config || !Array.isArray(rows) || rows.some((row) => !Array.isArray(row))) return null;
  const categoryIndexes = config.category.map((field) => columns.indexOf(field));
  const seriesIndexes = config.series.map((item) => columns.indexOf(item.field));
  const base: EChartsOption = {
    title: { text: config.title ?? '', left: 'left', textStyle: { fontSize: 14 }, width: '55%' },
    // Rich text is drawn on canvas; model/database strings never become HTML.
    tooltip: { trigger: config.type === 'bar' || config.type === 'line' ? 'axis' : 'item', renderMode: 'richText', confine: true },
    legend: { type: 'scroll', right: 0, top: 0, width: '40%' },
    aria: { enabled: true }
  };

  if (config.type === 'scatter') {
    const points: number[][] = [];
    for (const row of rows) {
      const x = asNumber(row[categoryIndexes[0]]);
      const y = asNumber(row[seriesIndexes[0]]);
      if (x !== null && y !== null) points.push([x, y]);
    }
    if (rows.length && !points.length) return null;
    return { ...base, grid: { top: 60, bottom: 35, left: 30, right: 30, containLabel: true },
      xAxis: { type: 'value', name: config.category[0], nameLocation: 'middle', nameGap: 28 },
      yAxis: { type: 'value', name: config.series[0].name, nameLocation: 'middle', nameGap: 45 },
      series: [{ type: 'scatter', name: config.series[0].name, data: points }] };
  }

  const labels: string[] = [];
  for (const row of rows) {
    const parts = categoryIndexes.map((index) => asLabel(row[index]));
    if (parts.some((part) => part === null)) return null;
    labels.push(parts.join(' / '));
  }
  const values = seriesIndexes.map((index) => rows.map((row) => asNumber(row[index])));
  if (rows.length && values.some((items) => items.every((item) => item === null))) return null;

  if (config.type === 'pie') {
    if (values[0].some((value) => value !== null && value < 0)) return null;
    const data = labels.flatMap((name, index) => {
      const value = values[0][index];
      return value === null ? [] : [{ name, value }];
    });
    return { ...base, legend: { type: 'scroll', right: 0, top: 30, orient: 'vertical', width: '25%' },
      series: [{ type: 'pie', name: config.series[0].name, data, stillShowZeroSum: false, radius: '65%', center: ['40%', '55%'],
        label: { show: true, overflow: 'truncate', width: 100 } }] };
  }

  const horizontal = config.orientation === 'horizontal';
  const hasSecondary = config.series.some((item) => item.axis === 'secondary');
  const numericAxes = (hasSecondary ? ['primary', 'secondary'] : ['primary']).map((axis) => ({
    type: 'value' as const,
    name: config.series.filter((item) => item.axis === axis).map((item) => item.name).join(' / '),
    nameLocation: 'middle' as const,
    nameGap: horizontal ? 28 : 45,
    ...(hasSecondary ? { position: horizontal ? (axis === 'primary' ? 'bottom' as const : 'top' as const) : (axis === 'primary' ? 'left' as const : 'right' as const) } : {})
  }));
  const categoryAxis = { type: 'category' as const, data: labels, axisLabel: { hideOverlap: true } };
  return {
    ...base,
    grid: { top: hasSecondary && horizontal ? 90 : 65, bottom: 45, left: 45, right: hasSecondary && !horizontal ? 65 : 30, containLabel: true },
    xAxis: horizontal ? numericAxes : categoryAxis,
    yAxis: horizontal ? categoryAxis : numericAxes,
    series: config.series.map((item, index) => ({
      type: config.type as 'bar' | 'line', name: item.name, data: values[index],
      ...(horizontal ? { xAxisIndex: item.axis === 'secondary' ? 1 : 0 } : { yAxisIndex: item.axis === 'secondary' ? 1 : 0 })
    }))
  };
};
