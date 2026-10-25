import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { parseWechatUrl, renderInlineNodes, type InlineLinkRenderer } from './markdown';

function Probe({ text }: { text: string }) {
  return <div>{renderInlineNodes(text)}</div>;
}

describe('renderInlineNodes', () => {
  it('does not render unsafe javascript links as anchors', () => {
    render(<Probe text={'Click [me](javascript:alert(1))'} />);

    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.getByText('Click [me](javascript:alert(1))')).toBeTruthy();
  });

  it('renders safe links and inline emphasis as React nodes', () => {
    render(<Probe text={'Read **this** [post](https://example.com)'} />);

    const link = screen.getByRole('link', { name: 'post' });
    expect(link.getAttribute('href')).toBe('https://example.com/');
    expect(screen.getByText('this').tagName).toBe('STRONG');
  });

  it('renders links inside nested emphasis without losing their labels', () => {
    const { container } = render(<Probe text={'**Read *[title](https://example.com/a_b)* now**'} />);

    expect(container.querySelector('strong em a')?.textContent).toBe('title');
    expect(screen.getByRole('link').getAttribute('href')).toBe('https://example.com/a_b');
    expect(container.textContent).toBe('Read title now');
  });

  it('renders bold links and combined emphasis', () => {
    const { container } = render(<Probe text={'**[title](https://example.com)** ***both***'} />);

    expect(container.querySelector('strong a')?.textContent).toBe('title');
    expect(container.querySelector('strong em')?.textContent).toBe('both');
  });

  it('autolinks bare and angle URLs without consuming punctuation or wrappers', () => {
    const { container } = render(<Probe text={'**https://example.com/a_b*c_d**，https://mp.weixin.qq.com/s/Ab_c-12。 <https://example.com/a_(b)> (https://example.com/end).'} />);

    expect(screen.getAllByRole('link').map(link => link.getAttribute('href'))).toEqual([
      'https://example.com/a_b*c_d',
      'https://mp.weixin.qq.com/s/Ab_c-12',
      'https://example.com/a_(b)',
      'https://example.com/end',
    ]);
    expect(container.querySelector('strong a')?.textContent).toBe('https://example.com/a_b*c_d');
    expect(container.textContent).toContain('，https://mp.weixin.qq.com/s/Ab_c-12。');
    expect(container.textContent).toContain('(https://example.com/end).');
  });

  it('preserves literal trailing URL stars outside emphasis', () => {
    render(<Probe text={'https://example.com/a_b*'} />);

    expect(screen.getByRole('link').getAttribute('href')).toBe('https://example.com/a_b*');
  });

  it('protects URLs and emphasis in code spans including longer backtick fences', () => {
    const { container } = render(<Probe text={'`https://example.com/**code**` ``[title](https://example.com) `literal` ``'} />);

    expect(screen.queryByRole('link')).toBeNull();
    expect(container.querySelectorAll('code')).toHaveLength(2);
    expect(container.querySelector('strong')).toBeNull();
    expect(container.querySelectorAll('.cjk-spacer')).toHaveLength(0);
  });

  it('formats link labels without creating nested anchors', () => {
    const { container } = render(<Probe text={'[**中文English** https://inner.example [inner](https://inner.example)](https://outer.example)'} />);

    expect(screen.getAllByRole('link')).toHaveLength(1);
    expect(container.querySelector('a strong')?.textContent).toBe('中文English');
    expect(container.querySelectorAll('a .cjk-spacer')).toHaveLength(1);
    expect(screen.getByRole('link').getAttribute('href')).toBe('https://outer.example/');
  });

  it('passes only validated links and React labels to the optional renderer', () => {
    const calls: { href: string; key: string }[] = [];
    const renderLink: InlineLinkRenderer = (href, label, key) => {
      calls.push({ href, key });
      return <a key={key} href={href} data-custom="true">{label}</a>;
    };
    const { container } = render(<div>{renderInlineNodes(
      '**[中文English](//example.com)** https://example.org [bad](javascript:alert(1)) [bad](//) `https://code.example`',
      renderLink,
    )}</div>);

    expect(calls.map(call => call.href)).toEqual(['https://example.com/', 'https://example.org/']);
    expect(new Set(calls.map(call => call.key)).size).toBe(2);
    expect(container.querySelectorAll('[data-custom="true"]')).toHaveLength(2);
    expect(container.querySelector('strong a')?.textContent).toBe('中文English');
    expect(container.querySelectorAll('a .cjk-spacer')).toHaveLength(1);
  });

  it('keeps default external-link safety and WeChat metadata', () => {
    render(<Probe text={'https://mp.weixin.qq.com/s?__biz=b&mid=m&idx=1'} />);

    const link = screen.getByRole('link');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
    expect(link.getAttribute('class')).toBe('js-article-link');
    expect(link.getAttribute('data-hippo-biz')).toBe('b');
    expect(link.getAttribute('data-hippo-mid')).toBe('m');
    expect(link.getAttribute('data-hippo-idx')).toBe('1');
  });

  it.each(['javascript:alert(1)', 'data:text/html,test', '//', 'https://'])('rejects unsafe or malformed destinations: %s', destination => {
    render(<Probe text={`[title](${destination})`} />);

    expect(screen.queryByRole('link')).toBeNull();
  });

  it('splits CJK and Latin boundaries with spacer elements', () => {
    const { container } = render(<Probe text={'中文English布局12月'} />);

    expect(container.querySelectorAll('.cjk-spacer')).toHaveLength(4);
    expect(container.textContent).toBe('中文English布局12月');
  });

  it('keeps inline code free of spacer elements', () => {
    const { container } = render(<Probe text={'运行 `git commit` 成功'} />);

    expect(container.querySelectorAll('.cjk-spacer')).toHaveLength(0);
    expect(container.querySelector('code')?.textContent).toBe('git commit');
  });
});

describe('parseWechatUrl', () => {
  it.each([
    'https://mp.weixin.qq.com/s/Ab_c-123',
    '//mp.weixin.qq.com/s/Ab_c-123?scene=1',
    'https://mp.weixin.qq.com/s?__biz=b&mid=m&idx=1',
    'https://mp.weixin.qq.com/s?__biz=b&sn=abc',
    'https://mp.weixin.qq.com/mp/appmsg/show?__biz=b&appmsgid=m&itemidx=1',
  ])('recognizes article URLs: %s', url => {
    expect(parseWechatUrl(url)).not.toBeNull();
  });

  it('preserves identifiers for legacy article URLs', () => {
    expect(parseWechatUrl('https://mp.weixin.qq.com/mp/appmsg/show?__biz=b&appmsgid=m&itemidx=1'))
      .toEqual({ biz: 'b', mid: 'm', idx: '1' });
  });

  it.each([
    'https://mp.weixin.qq.com/',
    'https://mp.weixin.qq.com/s',
    'https://mp.weixin.qq.com/other?__biz=b&mid=m&idx=1',
    'https://mp.weixin.qq.com/s/one/two',
    'https://mp.weixin.qq.com.evil.example/s/slug',
    'https://user@mp.weixin.qq.com/s/slug',
    'https://mp.weixin.qq.com:9090/s/slug',
    'javascript:alert(1)',
  ])('rejects non-article URLs: %s', url => {
    expect(parseWechatUrl(url)).toBeNull();
  });
});
