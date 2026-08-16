import z from "@deepseek-ai/schemastery";
import { installModelSelection } from "@deepseek-ai/dsh-agent";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { SessionId } from "@deepseek-ai/dsh-session";

const name = "headless-runner";
const inject = ["agentDefaultModel", "agents", "sessions"];
const Config = z.object({ task: z.string().required() });
const internals = {
	stdout: process.stdout,
	stderr: process.stderr,
};

function summarize(events, firstSeq) {
	let started = false;
	let text = "";
	let reason;
	for (const event of events) {
		if (event.seq < firstSeq) continue;
		if (event.type === "turn/start") {
			started = true;
			continue;
		}
		if (!started) continue;
		if (event.type === "assistant/message") {
			const joined = event.data.message.content
				.filter((block) => block.type === "text")
				.map((block) => block.text)
				.join("");
			if (joined !== "") text = joined;
		}
		if (event.type === "turn/end") reason = event.data.reason;
	}
	return { text, reason };
}

function fail(io, error) {
	io.stderr.write(`dsh: ${error instanceof Error ? error.message : String(error)}\n`);
	io.exit(1);
}

async function run(ctx, task, io) {
	await ctx.get("loader")?.await();
	const agents = ctx.get("agents");
	const defaultModel = ctx.get("agentDefaultModel");
	const sessions = ctx.get("sessions");
	if (agents === undefined || defaultModel === undefined || sessions === undefined) return;

	const resumeSessionId = process.env.MEMORYOS_RESUME_SESSION_ID;
	if (resumeSessionId === undefined || resumeSessionId === "") {
		throw new Error("MEMORYOS_RESUME_SESSION_ID is required by the continuation driver");
	}
	const selection = defaultModel.currentSelection();
	const { agent } = await agents.resume({
		resumeSessionId: SessionId(resumeSessionId),
		agentOptions: {
			provider: selection.provider,
			model: selection.model,
		},
		setup: (agentCtx) => {
			installModelSelection(agentCtx, {
				current: selection,
				assembled: undefined,
			});
		},
	});
	await agent.whenIdle();
	const firstSeq = agent.session.seq;
	agent.followup(
		createUserMessage({
			content: [{ type: "text", text: task }],
			source: { kind: "user" },
		}),
	);
	await agent.whenIdle();
	await sessions.flush(agent.session);
	const outcome = summarize(agent.session.events, firstSeq);
	io.stdout.write(outcome.text + "\n");
	if (outcome.reason?.kind === "error") {
		io.stderr.write(
			`dsh: ${outcome.reason.error.code}: ${outcome.reason.error.message}\n`,
		);
	}
	io.exit(outcome.reason?.kind === "completed" ? 0 : 1);
}

function apply(ctx, config) {
	const exit = ctx.get("appExit");
	if (exit === undefined) {
		throw new Error(
			"headless-runner: the launcher must provide ctx.appExit before the tree mounts",
		);
	}
	const io = { stdout: internals.stdout, stderr: internals.stderr, exit };
	run(ctx, config.task, io).catch((error) => {
		fail(io, error);
	});
}

export { Config, apply, inject, internals, name };
