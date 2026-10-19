import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, apiGet, apiSend, isAuthError } from '../api';
import { useI18n } from '../i18n';
import { useToast } from './useToast';
import { emitRefresh } from '../utils/events';

/**
 * What a manual run covers.
 *
 * A reader's run is always intersected with their own subscriptions; `siteWide`
 * switches to the console's route, which is not narrowed to one caller.
 */
export interface SyncRunScope {
  source?: string;
  groupId?: string | number;
  bizList?: string[];
  siteWide?: boolean;
}

/** The slice of the queued job the page follows and reports on. */
export interface SyncRunTask {
  task_id: string;
  status: string;
  error?: string | null;
}

const ACTIVE_STATUSES = ['queued', 'pending', 'running', 'cancelling'];
const POLL_INTERVAL_MS = 2000;

export const isSyncRunActive = (status: string | null | undefined): boolean =>
  ACTIVE_STATUSES.includes(String(status || ''));

const buildRunRequest = (
  scope: SyncRunScope,
): { path: string; body: Record<string, unknown> } => {
  if (scope.siteWide) {
    // The console route takes a source and nothing else: it covers the whole
    // instance, so a group or a biz list would be meaningless there.
    return {
      path: '/api/settings/admin/run',
      body: scope.source ? { source: scope.source } : {},
    };
  }
  // Every filter the page shows goes out together; the backend intersects them
  // with the caller's own subscriptions. An explicit empty `bizList` is a filter
  // too, so it goes out as `[]` instead of widening into every subscription.
  const body: Record<string, unknown> = {};
  if (scope.source) body.source = scope.source;
  if (scope.groupId !== undefined && scope.groupId !== '') {
    body.group_id = Number(scope.groupId);
  }
  if (scope.bizList !== undefined) body.biz_list = scope.bizList;
  return { path: '/api/settings/run', body };
};

export const wait = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    // An already-aborted signal never fires again: resolving now avoids a timer
    // that would hang until it elapsed on its own.
    if (signal.aborted) return resolve();
    let timer = 0;
    const onAbort = () => {
      window.clearTimeout(timer);
      resolve();
    };
    timer = window.setTimeout(() => {
      // A long job polls for minutes: reclaim the listener on a normal timeout
      // or they pile up one per tick and all fire on the next abort.
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal.addEventListener('abort', onAbort, { once: true });
  });

/**
 * Trigger one manual sync and follow it to its result.
 *
 * One implementation for every entry point — the article page, the group and
 * batch toolbars, the console — because they all need the same three things: a
 * scope, a status to show, and a refresh once the articles have landed.
 */
export function useSyncRun() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [task, setTask] = useState<SyncRunTask | null>(null);
  const [running, setRunning] = useState(false);
  // Every run takes a generation: a late answer from an older one is dropped
  // instead of overwriting the run the page is actually waiting for.
  const generationRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);

  // Leaving the page ends the polling and cancels the requests it started.
  useEffect(() => () => {
    generationRef.current += 1;
    controllerRef.current?.abort();
  }, []);

  const runSync = useCallback(async (scope: SyncRunScope = {}): Promise<string> => {
    generationRef.current += 1;
    const generation = generationRef.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setTask(null);
    setRunning(true);

    const { path, body } = buildRunRequest(scope);
    let taskId = '';
    try {
      const started = await apiSend(path, 'POST', body, { signal: controller.signal });
      if (generation !== generationRef.current) return taskId;
      taskId = String(started.task_id || '');
      const status = String(started.status || '');
      if (!taskId) {
        showToast(t('sync.runQueued', 'Sync task queued'));
        emitRefresh();
        return taskId;
      }
      showToast(t('sync.runQueued', 'Sync task queued'));

      let current: SyncRunTask = { task_id: taskId, status };
      setTask(current);
      // Only an active job is worth asking about: a terminal one ends the loop.
      while (isSyncRunActive(current.status)) {
        await wait(POLL_INTERVAL_MS, controller.signal);
        if (generation !== generationRef.current) return taskId;
        const payload = await apiGet(`/api/settings/tasks/${taskId}`, { signal: controller.signal });
        if (generation !== generationRef.current) return taskId;
        current = {
          task_id: taskId,
          status: String(payload.status || ''),
          error: (payload.error as string | null) || null,
        };
        setTask(current);
      }

      emitRefresh();
      if (current.status === 'success' || current.status === 'finished') {
        showToast(t('sync.status.success', 'Success'));
      } else if (current.status === 'cancelled') {
        showToast(t('sync.cancelled', 'Sync cancelled.'));
      } else {
        showToast(current.error || t('sync.failed', 'Sync failed. Please check login.'));
      }
      return taskId;
    } catch (err) {
      if (generation !== generationRef.current || isAuthError(err)) return taskId;
      const conflicting = err instanceof ApiError
        && (err.code === 'sync.task_conflict' || err.status === 409);
      if (conflicting) {
        // The queue holds one scope at a time; a refusal is a plain answer, and
        // saying "queued" here would be a lie.
        showToast(
          t('sync.taskConflict', 'Another sync with a different scope is running. Try again when it ends.'),
        );
        return taskId;
      }
      showToast((err as Error)?.message || t('sync.runFailed', 'Failed to start a sync task.'));
      return taskId;
    } finally {
      if (generation === generationRef.current) setRunning(false);
    }
  }, [showToast, t]);

  return { runSync, task, running };
}
