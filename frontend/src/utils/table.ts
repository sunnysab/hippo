interface MarkdownTable {
  headers: string[];
  rows: string[][];
  align: ('left' | 'center' | 'right' | undefined)[];
}

const delimiter = /^:?-{3,}:?$/;

function splitCells(row: string): string[] {
  const cells: string[] = [];
  let cell = '';
  for (let index = 0; index < row.length; index++) {
    const character = row[index];
    if (character === '\\' && index + 1 < row.length) {
      cell += character + row[++index];
    } else if (character === '|') {
      cells.push(cell.trim());
      cell = '';
    } else {
      cell += character;
    }
  }
  cells.push(cell.trim());
  return cells;
}

export function parseMarkdownTable(text: string): MarkdownTable | null {
  if (!text.includes('|')) return null;
  const lines = text.trim().split(/\r?\n/);
  let rows: string[][];
  if (lines.length === 1) {
    if (!text.trim().startsWith('|') || !text.trim().endsWith('|')) return null;
    const cells = splitCells(lines[0]);
    const separator = cells.findIndex((cell, index) => index > 1 && delimiter.test(cell));
    const width = separator - 2; // Exclude the leading empty token and the row boundary.
    if (width < 1 || (cells.length - 1) % (width + 1) !== 0) return null;
    rows = [];
    // shortcut: legacy recovery requires rectangular rows with outer pipes; reparse stored Markdown for ambiguous tables.
    for (let start = 1; start < cells.length; start += width + 1) {
      if (cells[start + width] !== '') return null;
      rows.push(cells.slice(start, start + width));
    }
  } else {
    rows = lines.map((line) => {
      const cells = splitCells(line.trim());
      if (cells[0] === '') cells.shift();
      if (cells[cells.length - 1] === '') cells.pop();
      return cells;
    });
  }
  const [headers, separators, ...body] = rows;
  if (!headers?.length || !separators || separators.length !== headers.length
    || !separators.every((cell) => delimiter.test(cell))
    || body.some((row) => row.length !== headers.length)) return null;
  const unescape = (cell: string) => cell.replace(/\\\|/g, '|');
  return {
    headers: headers.map(unescape),
    rows: body.map((row) => row.map(unescape)),
    align: separators.map((cell) => {
      if (cell.startsWith(':') && cell.endsWith(':')) return 'center';
      if (cell.endsWith(':')) return 'right';
      return cell.startsWith(':') ? 'left' : undefined;
    }),
  };
}
