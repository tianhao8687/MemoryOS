import crypto from "node:crypto";
import fs from "node:fs";
import {
	createZstdFrameDecoder,
	scanZstdFrames,
} from "/opt/deepseek-harness/packages/session/session-persistence-jsonl/lib/types/zstd.js";

const [inputPath, outputPath, turnText] = process.argv.slice(2);
const cutoffTurn = Number(turnText);
if (!inputPath || !outputPath || !Number.isInteger(cutoffTurn) || cutoffTurn < 1) {
	throw new Error("usage: node rollback_deepseek_session.js INPUT OUTPUT CUTOFF_TURN");
}
if (fs.existsSync(outputPath)) throw new Error(`refusing to overwrite ${outputPath}`);

function collectTurns(value, turns = new Set()) {
	if (Array.isArray(value)) {
		for (const item of value) collectTurns(item, turns);
		return turns;
	}
	if (value === null || typeof value !== "object") return turns;
	for (const [key, item] of Object.entries(value)) {
		if (key === "turn" && Number.isInteger(item)) turns.add(item);
		collectTurns(item, turns);
	}
	return turns;
}

const source = fs.readFileSync(inputPath);
const scanned = scanZstdFrames(source);
if (scanned.tornStart !== undefined) throw new Error("source session has a torn final frame");
let boundary;
let framesKept = 0;
let rowsKept = 0;
const retainedTurns = new Set();
for (const [index, frame] of scanned.frames.entries()) {
	const decoder = createZstdFrameDecoder();
	const chunks = [];
	for (const chunk of decoder.decode(source, [frame])) chunks.push(Buffer.from(chunk));
	// Each decoder owns a single lifecycle; allocate the next one after this frame.
	const text = Buffer.concat(chunks).toString("utf8");
	const rows = text
		.split("\n")
		.filter(Boolean)
		.map((line) => JSON.parse(line));
	const frameTurns = new Set();
	for (const row of rows) collectTurns(row, frameTurns);
	const before = [...frameTurns].some((turn) => turn < cutoffTurn);
	const atOrAfter = [...frameTurns].some((turn) => turn >= cutoffTurn);
	if (before && atOrAfter) {
		throw new Error(`frame ${index} mixes retained and rejected turns`);
	}
	if (atOrAfter) {
		boundary = frame.start;
		break;
	}
	framesKept += 1;
	rowsKept += rows.length;
	for (const turn of frameTurns) retainedTurns.add(turn);
}
if (boundary === undefined) throw new Error(`turn ${cutoffTurn} was not found`);
if (!retainedTurns.has(cutoffTurn - 1)) {
	throw new Error(`retained session does not contain turn ${cutoffTurn - 1}`);
}
const output = source.subarray(0, boundary);
const verification = scanZstdFrames(output);
if (verification.tornStart !== undefined || verification.frames.length !== framesKept) {
	throw new Error("rollback output failed frame validation");
}
fs.writeFileSync(outputPath, output, { flag: "wx" });
const sha256 = (value) => crypto.createHash("sha256").update(value).digest("hex");
process.stdout.write(
	JSON.stringify({
		input_bytes: source.length,
		input_sha256: sha256(source),
		output_bytes: output.length,
		output_sha256: sha256(output),
		frames_kept: framesKept,
		rows_kept: rowsKept,
		retained_turns: [...retainedTurns].sort((left, right) => left - right),
		cutoff_turn: cutoffTurn,
	}) + "\n",
);
