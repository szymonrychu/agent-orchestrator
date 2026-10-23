import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, deleteCluster, listEvents, retryMessage } from './client';

afterEach(() => vi.unstubAllGlobals());

describe('client', () => {
  it('throws ApiError parsed from the error shape', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ error: { code: 'not_found', message: 'cluster' } }), { status: 404 }),
      ),
    );
    await expect(deleteCluster('x')).rejects.toMatchObject({
      status: 404,
      code: 'not_found',
      message: 'cluster',
    });
    await expect(deleteCluster('x')).rejects.toBeInstanceOf(ApiError);
  });

  it('builds query strings and sends discard without parameters', async () => {
    const f = vi.fn(async () => new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', f);
    await listEvents('c1', { after_id: 4, limit: 10 });
    await retryMessage('m1');
    await deleteCluster('c1');
    const calls = f.mock.calls as unknown as Array<[string, RequestInit]>;
    expect(calls[0][0]).toBe('/api/v1/clusters/c1/events?after_id=4&limit=10');
    expect(calls[1][0]).toBe('/api/v1/messages/m1/retry');
    expect(calls[2][0]).toBe('/api/v1/clusters/c1');
    expect(calls[2][1].method).toBe('DELETE');
  });
});
