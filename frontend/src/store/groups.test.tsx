import { act, render } from '@testing-library/react';
import { createRef, useEffect } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { GroupsProvider, useGroupsActions, useGroupsState } from './groups';

interface HarnessApi {
  loadGroups: () => Promise<unknown>;
  selectGroup: (groupId: number | null) => void;
  getSelectedGroupId: () => number | null;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

function Harness({ apiRef }: { apiRef: React.RefObject<HarnessApi | null> }) {
  const { state, dispatch } = useGroupsState();
  const { loadGroups } = useGroupsActions();

  useEffect(() => {
    apiRef.current = {
      loadGroups,
      selectGroup: (groupId) => dispatch({ type: 'SELECT_GROUP', groupId }),
      getSelectedGroupId: () => state.selectedGroupId,
    };
  }, [apiRef, dispatch, loadGroups, state.selectedGroupId]);

  return null;
}

describe('useGroupsActions', () => {
  it('keeps the latest selected group when loadGroups resolves late', async () => {
    const pending = deferred<Response>();
    vi.stubGlobal('fetch', vi.fn(() => pending.promise));

    const apiRef = createRef<HarnessApi | null>();
    render(
      <GroupsProvider>
        <Harness apiRef={apiRef} />
      </GroupsProvider>,
    );

    await act(async () => {
      apiRef.current!.selectGroup(1);
    });

    const loadPromise = apiRef.current!.loadGroups();

    await act(async () => {
      apiRef.current!.selectGroup(2);
    });

    pending.resolve(new Response(JSON.stringify({
      groups: [{ id: 2, name: 'Second', account_count: 0, article_count: 0 }],
      default_group_id: null,
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));

    await act(async () => {
      await loadPromise;
    });

    expect(apiRef.current!.getSelectedGroupId()).toBe(2);
  });
});
