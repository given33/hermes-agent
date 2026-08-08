/**
 * In-process bridge for the official oh-my-pi Agent Hub controls.
 *
 * This is loaded with Pi's public --extension flag.  It lives outside the
 * pinned Pi checkout and imports Pi's own registry/lifecycle modules from the
 * configured checkout at runtime, so the upstream source remains untouched.
 * The Python gateway sends one local slash command and waits for the result
 * file; the actual chat/kill/revive operation runs in Pi's own process and
 * registry, exactly where the official collab host performs it.
 */

import { mkdir, rename, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

type AgentControlCommand = "chat" | "kill" | "revive";

interface ExtensionApi {
	registerCommand(
		name: string,
		options: {
			description?: string;
			handler: (args: string) => Promise<void> | void;
		},
	): void;
}

interface AgentControlRequest {
	requestId: string;
	cmd: AgentControlCommand;
	agentId: string;
	text?: string;
}

interface AgentControlResult {
	ok: boolean;
	requestId: string;
	cmd: AgentControlCommand;
	agentId: string;
	error?: string;
}

let internalModulesPromise:
	| Promise<{
		AgentLifecycleManager: { global(): { ensureLive(id: string): Promise<any>; release(id: string, expected?: any, options?: { tombstone?: boolean }): Promise<boolean> } };
		AgentRegistry: { global(): { get(id: string): any } };
		USER_INTERRUPT_LABEL: string;
	}>
	| undefined;

async function internalModules() {
	if (!internalModulesPromise) {
		const root = process.env.CODING_PI_ROOT?.trim();
		if (!root) throw new Error("CODING_PI_ROOT is not set for the Pi Agent Hub bridge");
		internalModulesPromise = Promise.all([
			import(pathToFileURL(join(root, "packages", "coding-agent", "src", "registry", "agent-lifecycle.ts")).href),
			import(pathToFileURL(join(root, "packages", "coding-agent", "src", "registry", "agent-registry.ts")).href),
			import(pathToFileURL(join(root, "packages", "coding-agent", "src", "session", "messages.ts")).href),
		]).then(([lifecycle, registry, messages]) => ({
			AgentLifecycleManager: lifecycle.AgentLifecycleManager,
			AgentRegistry: registry.AgentRegistry,
			USER_INTERRUPT_LABEL: messages.USER_INTERRUPT_LABEL,
		}));
	}
	return internalModulesPromise;
}

function decodeRequest(args: string): AgentControlRequest {
	const encoded = args.trim();
	if (!encoded) throw new Error("missing Agent Hub request");
	const request = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8")) as Partial<AgentControlRequest>;
	if (!request.requestId || !/^[A-Za-z0-9_-]{8,96}$/.test(request.requestId)) throw new Error("invalid request id");
	if (request.cmd !== "chat" && request.cmd !== "kill" && request.cmd !== "revive") throw new Error("invalid Agent Hub command");
	if (!request.agentId || request.agentId === "main") throw new Error("the main Pi agent is not controlled through Agent Hub");
	return request as AgentControlRequest;
}

async function writeResult(result: AgentControlResult): Promise<void> {
	const directory = process.env.CODING_PI_AGENT_CONTROL_DIR?.trim();
	if (!directory) return;
	await mkdir(directory, { recursive: true });
	const target = join(directory, `${result.requestId}.json`);
	const temporary = `${target}.${Date.now()}.tmp`;
	await writeFile(temporary, JSON.stringify(result), { encoding: "utf8", mode: 0o600 });
	await rename(temporary, target);
}

async function executeRequest(request: AgentControlRequest): Promise<void> {
	const { AgentLifecycleManager, AgentRegistry, USER_INTERRUPT_LABEL } = await internalModules();
	const registry = AgentRegistry.global();
	const lifecycle = AgentLifecycleManager.global();
	const ref = registry.get(request.agentId);
	if (!ref) throw new Error(`unknown agent ${request.agentId}`);
	if (ref.kind === "advisor") throw new Error(`agent ${request.agentId}: advisor transcripts are read-only`);

	switch (request.cmd) {
		case "chat": {
			const text = request.text?.trim();
			if (!text) throw new Error(`agent ${request.agentId}: empty chat message`);
			const session = await lifecycle.ensureLive(request.agentId);
			// The upstream CollabHost returns after scheduling the steer.  Keep the
			// same behavior so a long subagent turn never blocks the guest socket.
			void session.prompt(text, { streamingBehavior: "steer" }).catch(error => {
				console.warn(`Pi Agent Hub chat failed for ${request.agentId}: ${String(error)}`);
			});
			break;
		}
		case "kill": {
			if (ref.status === "running" && ref.session) {
				await ref.session.abort({ reason: USER_INTERRUPT_LABEL });
			}
			await lifecycle.release(request.agentId, ref, { tombstone: true });
			break;
		}
		case "revive":
			await lifecycle.ensureLive(request.agentId);
			break;
	}
}

export default async function piAgentControlExtension(pi: ExtensionApi): Promise<void> {
	// Importing the internal modules during extension startup confirms that the
	// extension is attached to the same unmodified Pi checkout as the session.
	await internalModules();
	pi.registerCommand("hermes-agent-control", {
		description: "Internal Hermes bridge for official Pi Agent Hub controls",
		handler: async args => {
			let request: AgentControlRequest | undefined;
			try {
				request = decodeRequest(args);
				await executeRequest(request);
				await writeResult({
					ok: true,
					requestId: request.requestId,
					cmd: request.cmd,
					agentId: request.agentId,
				});
			} catch (error) {
				if (request) {
					await writeResult({
						ok: false,
						requestId: request.requestId,
						cmd: request.cmd,
						agentId: request.agentId,
						error: String(error),
					});
				}
				throw error;
			}
		},
	});
}

