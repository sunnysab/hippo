import { describe, expect, it } from 'vitest';
import { parseMarkdownTable } from './table';

const archivedTable = '| **方法或资产** | **主要提供什么** | **单独使用时仍需回答什么** | | --- | --- | --- | | 数据库或接口 Schema | 字段, 类型, 数据结构和部分约束 | 字段在业务中代表什么, 与其他系统如何对应 | | CodeGraph | 符号, 调用, 依赖和代码位置 | 调用链承担什么业务职责, 哪些约定不能破坏 | | 知识图谱 | 实体及其关系实例 | 关系是否有明确含义, 适用范围和依据 | | 文档与 RAG | 与当前问题相关的材料 | 多段材料是否在谈同一对象, 是否适用当前场景 | | 本体 | 共享概念, 关系语义和相关公理或约束 | 当前事实是否符合这些定义, 动作是否产生预期结果 |';

describe('Markdown tables', () => {
  it('recovers the flattened table stored in article 8146595', () => {
    const table = parseMarkdownTable(archivedTable)!;
    expect(table.headers).toHaveLength(3);
    expect(table.headers[0]).toBe('**方法或资产**');
    expect(table.rows).toHaveLength(5);
    expect(table.rows[0][0]).toBe('数据库或接口 Schema');
    expect(table.rows[4][2]).toBe('当前事实是否符合这些定义, 动作是否产生预期结果');
  });

  it('keeps empty cells while recovering flattened row boundaries', () => {
    expect(parseMarkdownTable('| A | B | | --- | --- | | | value | | last | |')?.rows)
      .toEqual([['', 'value'], ['last', '']]);
  });

  it('accepts rows with or without outer pipes and retains column alignment', () => {
    const table = parseMarkdownTable('Left | Center | Right\n:--- | :---: | ---:\na | b | c')!;
    expect(table.align).toEqual(['left', 'center', 'right']);
    expect(table.rows).toEqual([['a', 'b', 'c']]);
  });

  it('does not split escaped pipes inside cells', () => {
    const table = parseMarkdownTable('| A | B |\n| --- | --- |\n| a\\|b | `c\\|d` |')!;
    expect(table.rows).toEqual([['a|b', '`c|d`']]);
  });

  it('allows an empty header and a header-only table', () => {
    expect(parseMarkdownTable('| | B |\n| --- | --- |')?.headers).toEqual(['', 'B']);
    expect(parseMarkdownTable('| A | | --- |')?.rows).toEqual([]);
  });

  it.each([
    'ordinary prose with a | separator',
    'a\n---',
    '| A | B | | --- | --- | | not enough |',
    '| A | B |\n| --- | --- |\n| extra | cells | here |',
    '| A | B |\n| --- | nope |\n| a | b |',
    'Prose before | A | B | | --- | --- | | a | b |',
  ])('preserves prose or malformed tables as text: %s', (text) => {
    expect(parseMarkdownTable(text)).toBeNull();
  });
});
