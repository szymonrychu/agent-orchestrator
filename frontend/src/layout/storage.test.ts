// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { loadJson, saveJson } from './storage'

const isNumberArray = (v: unknown): v is number[] => Array.isArray(v) && v.every((n) => typeof n === 'number')

beforeEach(() => localStorage.clear())
afterEach(() => vi.unstubAllGlobals())

describe('storage', () => {
  test('loadJson_round_trip_and_fallbacks', () => {
    saveJson('layout', { a: [1, 2] })
    expect(localStorage.getItem('orch:layout')).toBe('{"a":[1,2]}')
    expect(loadJson('layout', null)).toEqual({ a: [1, 2] })

    expect(loadJson('missing', 'fb')).toBe('fb')

    localStorage.setItem('orch:bad', '{not json')
    expect(loadJson('bad', 'fb')).toBe('fb')

    saveJson('nums', [1, 2, 3])
    expect(loadJson('nums', [] as number[], isNumberArray)).toEqual([1, 2, 3])
    saveJson('nums', ['x'])
    expect(loadJson('nums', [9], isNumberArray)).toEqual([9])
  })

  test('storage_never_throws', () => {
    vi.stubGlobal('localStorage', undefined)
    expect(() => saveJson('k', 1)).not.toThrow()
    expect(loadJson('k', 'fb')).toBe('fb')

    const throwing = {
      getItem: () => {
        throw new Error('denied')
      },
      setItem: () => {
        throw new Error('quota')
      },
    }
    vi.stubGlobal('localStorage', throwing)
    expect(() => saveJson('k', 1)).not.toThrow()
    expect(loadJson('k', 'fb')).toBe('fb')

    vi.unstubAllGlobals()
    const circular: Record<string, unknown> = {}
    circular.self = circular
    expect(() => saveJson('k', circular)).not.toThrow()
    expect(() => saveJson('k', undefined)).not.toThrow()
    expect(loadJson('k', 'fb')).toBe('fb')
  })
})
