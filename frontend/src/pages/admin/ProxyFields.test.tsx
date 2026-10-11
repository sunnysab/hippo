import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ProxyFields } from './ProxyFields';

const renderFields = (values: Record<string, string>) => {
  const onSave = vi.fn().mockResolvedValue(undefined);
  const { container } = render(<ProxyFields values={values} onSave={onSave} />);
  const field = (id: string) => container.querySelector(`#${id}`) as HTMLInputElement | HTMLSelectElement;
  return { onSave, field };
};

const BASE = {
  proxy_default: '',
  proxy_bilibili: 'direct',
  proxy_youtube: 'http://proxy.example:8082',
  proxy_podcast: 'default',
};

describe('ProxyFields', () => {
  it('derives each source mode from the stored value', () => {
    const { field } = renderFields(BASE);

    expect(field('proxy_bilibili').value).toBe('direct');
    expect(field('proxy_youtube').value).toBe('custom');
    expect(field('proxy_podcast').value).toBe('default');
    // 只有「自定义」才需要填地址。
    expect(field('proxy_youtube-url').value).toBe('http://proxy.example:8082');
    expect(field('proxy_bilibili-url')).toBeNull();
    expect(field('proxy_podcast-url')).toBeNull();
  });

  it('writes the mode alone when a source is pointed at the default or at a direct link', async () => {
    const { onSave, field } = renderFields(BASE);

    fireEvent.change(field('proxy_podcast'), { target: { value: 'default' } });
    fireEvent.change(field('proxy_youtube'), { target: { value: 'direct' } });

    expect(onSave).toHaveBeenCalledWith({ proxy_podcast: 'default' });
    expect(onSave).toHaveBeenCalledWith({ proxy_youtube: 'direct' });
  });

  it('waits for the url before saving a custom proxy', async () => {
    const { onSave, field } = renderFields({ ...BASE, proxy_podcast: 'default' });

    fireEvent.change(field('proxy_podcast'), { target: { value: 'custom' } });

    // 只切到「自定义」还不能写库：此刻输入框还是空的。
    expect(onSave).not.toHaveBeenCalled();
    const url = field('proxy_podcast-url');
    expect(url).not.toBeNull();

    fireEvent.change(url, { target: { value: 'http://127.0.0.1:7890' } });
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.blur(url);

    expect(onSave).toHaveBeenCalledWith({ proxy_podcast: 'http://127.0.0.1:7890' });
  });

  it('saves the instance default on blur', () => {
    const { onSave, field } = renderFields(BASE);

    fireEvent.change(field('proxy_default'), { target: { value: 'http://default.example:8082' } });
    fireEvent.blur(field('proxy_default'));

    expect(onSave).toHaveBeenCalledWith({ proxy_default: 'http://default.example:8082' });
  });

  it('does not write a field that was left alone', () => {
    const { onSave, field } = renderFields(BASE);

    fireEvent.blur(field('proxy_default'));
    fireEvent.focus(field('proxy_default'));
    fireEvent.blur(field('proxy_default'));

    expect(onSave).not.toHaveBeenCalled();
  });

  it('names the field after its source', () => {
    renderFields(BASE);

    expect(screen.getByText('哔哩哔哩 · 代理')).toBeTruthy();
    expect(screen.getByText('YouTube · 代理')).toBeTruthy();
    expect(screen.getByText('播客 · 代理')).toBeTruthy();
  });
});
