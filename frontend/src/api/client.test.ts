import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, copyPreset, createCluster, createPreset, deleteCluster, deletePreset, getPreset, listEvents, listPresets, retryMessage, updatePreset } from './client';

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

describe('preset client', () => {
  it('calls the documented preset routes', async () => {
    const f = vi.fn(async () => new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', f);
    await listPresets();
    await getPreset('p 1');
    await createPreset({ name: 'n', spec: { env: {}, max_attempts: 5, peers: true, replicas: 1, static: false, workdir: 'w' } });
    await updatePreset('p1', { name: 'n2', spec: { env: {}, max_attempts: 5, peers: true, replicas: 2, static: false, workdir: 'w' } });
    await deletePreset('p1');
    await copyPreset('p1');
    await copyPreset('p1', { name: 'dup' });
    await createCluster({ preset_id: 'p1', slug: 's' });
    const calls = f.mock.calls as unknown as Array<[string, RequestInit]>;
    expect(calls.map(([url, init]) => `${init.method} ${url}`)).toEqual([
      'GET /api/v1/presets',
      'GET /api/v1/presets/p%201',
      'POST /api/v1/presets',
      'PUT /api/v1/presets/p1',
      'DELETE /api/v1/presets/p1',
      'POST /api/v1/presets/p1/copy',
      'POST /api/v1/presets/p1/copy',
      'POST /api/v1/clusters',
    ]);
    expect(calls[5][1].body).toBeUndefined();
    expect(calls[6][1].body).toBe('{"name":"dup"}');
    expect(calls[7][1].body).toBe('{"preset_id":"p1","slug":"s"}');
  });
});
