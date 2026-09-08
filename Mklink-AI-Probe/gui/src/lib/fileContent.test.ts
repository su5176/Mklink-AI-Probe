import { describe, expect, it } from 'vitest'
import { fileContentStamp } from './fileContent'

describe('file content identity', () => {
  it('detects changed bytes with identical name, size and timestamp', async () => {
    const first = new File(['old'], 'firmware.hex', { lastModified: 100 })
    const next = new File(['new'], 'firmware.hex', { lastModified: 100 })
    expect(await fileContentStamp(first)).not.toBe(await fileContentStamp(next))
  })
})
