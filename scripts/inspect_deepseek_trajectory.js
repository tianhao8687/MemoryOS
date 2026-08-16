import fs from 'node:fs'
import {
  createZstdFrameDecoder,
  scanZstdFrames,
} from '/opt/deepseek-harness/packages/session/session-persistence-jsonl/lib/types/zstd.js'

const path = process.argv[2]
if (path === undefined) throw new Error('usage: node inspect_deepseek_trajectory.js SESSION [TAIL]')
const tail = Number.parseInt(process.argv[3] ?? '0', 10)
if (!Number.isInteger(tail) || tail < 0) throw new Error('TAIL must be a non-negative integer')
const typeFilter = new Set((process.argv[4] ?? '').split(',').filter(Boolean))
const source = fs.readFileSync(path)
const { frames } = scanZstdFrames(source)
const decoder = createZstdFrameDecoder()
const chunks = []
for (const chunk of decoder.decode(source, frames)) chunks.push(Buffer.from(chunk))
const rows = Buffer.concat(chunks).toString('utf8')
  .split('\n')
  .filter(Boolean)
  .map(line => JSON.parse(line))

const trajectory = []
for (const row of rows) {
  if (row.type === 'user/message') {
    trajectory.push({
      seq: row.seq,
      step: row.data?.step,
      type: row.type,
      message: truncate(row.data?.message, 2_000),
    })
  } else if (row.type === 'turn/start' || row.type === 'turn/end') {
    trajectory.push({
      seq: row.seq,
      step: row.data?.step,
      type: row.type,
      data: truncate(row.data, 2_000),
    })
  } else if (row.type === 'assistant/message') {
    trajectory.push({
      seq: row.seq,
      step: row.data?.step,
      type: row.type,
      message: truncate(row.data?.message, 2_000),
    })
  } else if (row.type === 'tool/call') {
    trajectory.push({
      seq: row.seq,
      step: row.data?.step,
      type: row.type,
      name: row.data?.name,
      arguments: truncate(row.data?.arguments, 1_500),
    })
  } else if (row.type === 'tool/result') {
    trajectory.push({
      seq: row.seq,
      step: row.data?.step,
      type: row.type,
      message: truncate(row.data?.message, 4_000),
    })
  } else if (row.type === 'reasoning-chunks') {
    trajectory.push({
      step: row.data?.step,
      type: row.type,
      texts: truncate(row.data?.texts, 4_000),
    })
  }
}

const filtered = typeFilter.size === 0
  ? trajectory
  : trajectory.filter(item => typeFilter.has(item.type))
const visible = tail === 0 ? filtered : filtered.slice(-tail)
process.stdout.write(`${JSON.stringify(visible, null, 2)}\n`)

function truncate(value, limit) {
  const serialized = JSON.stringify(value)
  if (serialized === undefined || serialized.length <= limit) return value
  return `${serialized.slice(0, limit)}…`
}
