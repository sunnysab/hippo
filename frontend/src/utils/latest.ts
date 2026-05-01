export const createLatestOnly = <T>(apply: (value: T) => void) => {
  let currentRequestId = 0;

  return async (load: () => Promise<T>) => {
    const requestId = currentRequestId + 1;
    currentRequestId = requestId;

    const value = await load();
    if (requestId !== currentRequestId) {
      return false;
    }

    apply(value);
    return true;
  };
};
