import { describe, expect, it } from 'vitest';
import {
  applyEvents,
  emptyChat,
  filterByAgents,
  prependHistory,
  trimWindow,
  type EventRow,
} from './chatModel';

const ords = { a1: 1, a2: 2 };
const row = (id: number, type: string, data: Record<string, unknown> = {}, agent = 'a1'): EventRow => ({
  id,
  agent_id: agent,
  type,
  ts: `2026-01-01T00:00:${String(id % 60).padStart(2, '0')}Z`,
  turn_id: 't1',
  data,
});
const fresh = () => emptyChat(ords);

describe('chatModel', () => {
  it('maps event types to item kinds', () => {
    const rows = [
      row(1, 'user_message', { text: 'hi', origin: 'user' }),
      row(2, 'user_message', { text: 'h', origin: 'hook' }),
      row(3, 'user_message', { text: 'r', origin: 'restart' }),
      row(4, 'user_message', { text: 'c', origin: 'command' }),
      row(5, 'assistant_text', { text: 'yo' }),
      row(6, 'thinking', { text: 'hmm' }),
      row(7, 'tool_call', { tool_use_id: 'x', tool_name: 'Bash', tool_input: {} }),
      row(8, 'tool_denied', { name: 'Bash', reason: 'no' }),
      row(9, 'turn_completed', { record: { usage: { input_tokens: 3 } } }),
      row(10, 'turn_failed', { error: 'boom' }),
      row(11, 'error', { message: 'bad' }),
      row(12, 'peer_message_received', { from: 'a2', message: 'psst' }),
      row(13, 'mystery', {}),
    ];
    const s = applyEvents(fresh(), rows);
    expect(s.items.map((i) => i.kind)).toEqual([
      'user_message', 'user_message', 'user_message', 'user_message',
      'assistant', 'thinking', 'tool', 'denied', 'turn_end', 'error', 'error', 'peer',
    ]);
    for (const i of s.items) {
      expect(i.agentId).toBe('a1');
      expect(i.agentOrdinal).toBe(1);
      expect(i.eventId).toBeGreaterThan(0);
      expect(i.ts).toBeTruthy();
    }
    expect(s.items[8]).toMatchObject({ usage: { input_tokens: 3 } });
  });

  it('pairs tool calls with results', () => {
    let s = applyEvents(fresh(), [row(1, 'tool_call', { tool_use_id: 'x', tool_name: 'Bash', tool_input: { c: 1 } })]);
    expect(s.items[0]).toMatchObject({ kind: 'tool', status: 'running' });
    s = applyEvents(s, [row(2, 'tool_result', { tool_use_id: 'x', response: 'done', is_error: false })]);
    expect(s.items).toHaveLength(1);
    expect(s.items[0]).toMatchObject({ status: 'ok', output: 'done' });
    s = applyEvents(s, [
      row(3, 'tool_call', { tool_use_id: 'y', tool_name: 'Bash', tool_input: {} }),
      row(4, 'tool_result', { tool_use_id: 'y', response: 'e', is_error: true }),
    ]);
    expect(s.items[1]).toMatchObject({ status: 'error', output: 'e' });
    // across the page boundary: result newer, call arrives via prepend
    let p = applyEvents(fresh(), [row(12, 'tool_result', { tool_use_id: 'z', response: 'late' })]);
    expect(p.items).toHaveLength(0);
    p = prependHistory(p, [row(11, 'tool_call', { tool_use_id: 'z', tool_name: 'Read', tool_input: {} })]);
    expect(p.items[0]).toMatchObject({ kind: 'tool', status: 'ok', output: 'late' });
  });

  it('thinking starts collapsed', () => {
    const s = applyEvents(fresh(), [row(1, 'thinking', { text: 'hmm' })]);
    expect(s.items[0]).toMatchObject({ kind: 'thinking', collapsed: true, text: 'hmm' });
  });

  it('peer origin becomes peer item', () => {
    const s = applyEvents(fresh(), [row(1, 'user_message', { text: 'yo', origin: 'peer', from: 'a2' })]);
    expect(s.items[0]).toMatchObject({ kind: 'peer', text: 'yo' });
  });

  it('dedupes and orders on prepend and append', () => {
    let s = applyEvents(fresh(), [row(5, 'assistant_text', { text: 'e' }), row(6, 'assistant_text', { text: 'f' })]);
    s = applyEvents(s, [row(6, 'assistant_text', { text: 'f' }), row(7, 'assistant_text', { text: 'g' })]);
    s = prependHistory(s, [row(3, 'assistant_text', { text: 'c' }), row(5, 'assistant_text', { text: 'e' }), row(4, 'assistant_text', { text: 'd' })]);
    expect(s.items.map((i) => i.eventId)).toEqual([3, 4, 5, 6, 7]);
  });

  it('trims to newest', () => {
    const rows = Array.from({ length: 10 }, (_, i) => row(i + 1, 'assistant_text', { text: String(i) }));
    const s = trimWindow(applyEvents(fresh(), rows), 4);
    expect(s.items.map((i) => i.eventId)).toEqual([7, 8, 9, 10]);
    expect(trimWindow(applyEvents(fresh(), rows)).items).toHaveLength(10);
  });

  it('gap rows become gap items', () => {
    const s = applyEvents(fresh(), [row(1, 'gap', { from_seq: 3, to_seq: 9 })]);
    expect(s.items[0]).toMatchObject({ kind: 'gap' });
  });

  it('filters by agents', () => {
    const s = applyEvents(fresh(), [row(1, 'assistant_text', {}, 'a1'), row(2, 'assistant_text', {}, 'a2')]);
    expect(filterByAgents(s.items, ['a2']).map((i) => i.eventId)).toEqual([2]);
    expect(filterByAgents(s.items, [])).toHaveLength(2);
  });
});
