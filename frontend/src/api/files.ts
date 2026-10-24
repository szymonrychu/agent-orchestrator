/** Typed access to the cluster workdir file API (read-only). */
import { query, request } from './client'
import type { components } from './schema'

export type FileListing = components['schemas']['FileListing']
export type FileEntry = components['schemas']['FileEntry']
export type FileContent = components['schemas']['FileContent']

const id = encodeURIComponent

/** One directory of the workdir; `path` is relative to the workdir root ("" is the root), `offset` the previous page's `next_offset`. */
export const listFiles = (clusterId: string, path: string, offset?: number) =>
  request<FileListing>('GET', `/clusters/${id(clusterId)}/files${query({ path, offset })}`)

/** One file of the workdir, with its content when it is text. */
export const readFile = (clusterId: string, path: string) =>
  request<FileContent>('GET', `/clusters/${id(clusterId)}/files/content${query({ path })}`)
