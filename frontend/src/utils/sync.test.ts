import { describe, expect, it } from 'vitest';
import { getSyncRunLabel } from './sync';
import zhCN from '../i18n/zh-CN.json';

describe('sync run labels', () => {
  it('distinguishes cancellation in progress from running and cancelled', () => {
    const t = (key: string, fallback?: string) => zhCN[key as keyof typeof zhCN] || fallback || key;
    expect(getSyncRunLabel('cancelling', t)).toBe(zhCN['sync.cancelling']);
    expect(getSyncRunLabel('running', t)).toBe(zhCN['sync.status.running']);
    expect(getSyncRunLabel('cancelled', t)).toBe(zhCN['sync.status.cancelled']);
  });
});
