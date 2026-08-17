import fs from "node:fs";
import {
	createZstdFrameDecoder,
	scanZstdFrames,
} from "/opt/deepseek-harness/packages/session/session-persistence-jsonl/lib/types/zstd.js";

const path = process.argv[2];
if (path === undefined) throw new Error("usage: node summarize_deepseek_session.js SESSION");
const source = fs.readFileSync(path);
const { frames, tornStart } = scanZstdFrames(source);
const decoder = createZstdFrameDecoder();
const chunks = [];
for (const chunk of decoder.decode(source, frames)) chunks.push(Buffer.from(chunk));
const decoded = Buffer.concat(chunks).toString("utf8");
const rows = decoded
	.split("\n")
	.filter(Boolean)
	.map((line) => JSON.parse(line));
const typeCounts = new Map();
const toolCounts = new Map();
const callsByTurn = new Map();
const argumentProbeCounts = new Map([
	["benchmark_hidden_test", 0],
	["cold-score", 0],
	["score-r", 0],
	["continuation-r", 0],
	["/trial-", 0],
]);
const resultProbeCounts = new Map([...argumentProbeCounts.keys()].map((key) => [key, 0]));
const benchmarkArgumentOperations = new Map([
	["find", 0],
	["copy", 0],
	["read", 0],
	["write", 0],
	["run", 0],
]);
const benchmarkArgumentPaths = [];
const probesByTurn = new Map();

function incrementTurnProbe(turn, surface, probe) {
	const key = String(turn ?? "unknown");
	const row = probesByTurn.get(key) ?? {};
	const field = `${surface}:${probe}`;
	row[field] = (row[field] ?? 0) + 1;
	probesByTurn.set(key, row);
}

function increment(map, key) {
	map.set(key, (map.get(key) ?? 0) + 1);
}

for (const row of rows) {
	const type = typeof row.type === "string" ? row.type : "<header>";
	increment(typeCounts, type);
	if (row.type === "tool/call" && typeof row.data?.name === "string") {
		increment(toolCounts, row.data.name);
		const turn = String(row.data.turn ?? "unknown");
		const calls = callsByTurn.get(turn) ?? [];
		calls.push(row.data.name);
		callsByTurn.set(turn, calls);
		const serializedArguments = JSON.stringify(row.data.arguments ?? {}).toLowerCase();
		for (const probe of argumentProbeCounts.keys()) {
			if (serializedArguments.includes(probe)) {
				argumentProbeCounts.set(probe, argumentProbeCounts.get(probe) + 1);
				incrementTurnProbe(row.data.turn, "argument", probe);
			}
		}
		if (serializedArguments.includes("benchmark_hidden_test")) {
			for (const match of serializedArguments.matchAll(
				/[a-z0-9_./-]*benchmark_hidden_test\.py/g,
			)) {
				benchmarkArgumentPaths.push(match[0]);
			}
			const operationPatterns = {
				find: /\b(find|grep|rg)\b/,
				copy: /\b(cp|copy)\b/,
				read: /\b(cat|sed|head|tail|read)\b/,
				write: /\b(write|touch|tee)\b|>>?|apply_patch/,
				run: /\b(python|pytest|run)\b/,
			};
			for (const [operation, pattern] of Object.entries(operationPatterns)) {
				if (pattern.test(serializedArguments)) {
					benchmarkArgumentOperations.set(
						operation,
						benchmarkArgumentOperations.get(operation) + 1,
					);
				}
			}
		}
	}
	if (row.type === "tool/result") {
		const serializedResult = JSON.stringify(row.data ?? {}).toLowerCase();
		for (const probe of resultProbeCounts.keys()) {
			if (serializedResult.includes(probe)) {
				resultProbeCounts.set(probe, resultProbeCounts.get(probe) + 1);
				incrementTurnProbe(row.data?.turn, "result", probe);
			}
		}
	}
}

const sorted = (map) =>
	[...map.entries()].sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]));
process.stdout.write(
	JSON.stringify(
		{
			bytes: source.length,
			complete_frames: frames.length,
			torn_frame: tornStart !== undefined,
			rows: rows.length,
			type_counts: sorted(typeCounts),
			tool_counts: sorted(toolCounts),
			calls_by_turn: [...callsByTurn.entries()].map(([turn, calls]) => ({ turn, calls })),
			argument_probe_counts: Object.fromEntries(argumentProbeCounts),
			result_probe_counts: Object.fromEntries(resultProbeCounts),
			benchmark_argument_operations: Object.fromEntries(benchmarkArgumentOperations),
			benchmark_argument_paths: [...new Set(benchmarkArgumentPaths)],
			probes_by_turn: Object.fromEntries(probesByTurn),
			last_rows: rows.slice(-30).map((row) => ({
				seq: row.seq,
				type: row.type ?? "<header>",
				data_keys:
					row.data !== null && typeof row.data === "object"
						? Object.keys(row.data).sort()
						: [],
			})),
		},
		null,
		2,
	) + "\n",
);
