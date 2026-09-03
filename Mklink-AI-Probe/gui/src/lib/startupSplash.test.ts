import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const indexSource = readFileSync(resolve(process.cwd(), 'index.html'), 'utf8')
const mainSource = readFileSync(resolve(process.cwd(), 'src/main.ts'), 'utf8')

describe('desktop startup splash', () => {
  it('renders the probe image and progress UI before loading the application module', () => {
    const splashOffset = indexSource.indexOf('id="startup-splash"')
    const moduleOffset = indexSource.indexOf('src="/src/main.ts"')

    expect(splashOffset).toBeGreaterThan(0)
    expect(moduleOffset).toBeGreaterThan(splashOffset)
    expect(indexSource).toContain('src="/startup-probe.png"')
    expect(indexSource).toContain('role="progressbar"')
    expect(indexSource).toContain('window.__MKLINK_STARTUP__')
  })

  it('keeps the splash visible through endpoint discovery and removes it after mount', () => {
    expect(mainSource.indexOf("updateStartup(22")).toBeLessThan(
      mainSource.indexOf('await initializeRuntimeEndpoint()'),
    )
    expect(mainSource.indexOf("updateStartup(80")).toBeGreaterThan(
      mainSource.indexOf('await initializeRuntimeEndpoint()'),
    )
    expect(mainSource.indexOf('createApp(App).use(router).mount')).toBeLessThan(
      mainSource.indexOf("window.__MKLINK_STARTUP__?.finish()"),
    )
  })
})
