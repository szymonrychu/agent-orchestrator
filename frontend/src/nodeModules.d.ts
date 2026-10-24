/** The two Node modules the retirement test uses; the build keeps @types/node out, so declare just these calls. */
declare module 'node:child_process' {
  export function execFileSync(file: string, args: string[], options: { encoding: 'utf8' }): string
}
declare module 'node:fs' {
  export function readFileSync(path: string, encoding: 'utf8'): string
}
