import { z } from "zod";

export default {
  description:
    "Create a pull request for a ticket. Calls orch pr create and returns the PR URL on success.",
  args: {
    ticket_id: z.string().describe("The ticket ID (e.g. ORCH-001)"),
    base_branch: z
      .string()
      .optional()
      .describe("Base branch for the PR (default: main)"),
  },
  async execute(
    args: { ticket_id: string; base_branch?: string },
    context: { directory: string; worktree: string },
  ) {
    const cmd = [
      "uv",
      "run",
      "orch",
      "pr",
      "create",
      args.ticket_id,
      "--base",
      args.base_branch ?? "main",
    ];
    const proc = Bun.spawnSync(cmd, {
      env: {
        ...process.env,
        ORCH_DB_PATH: process.env.ORCH_DB_PATH ?? ".orchestra/state.db",
      },
      cwd: context.directory,
    });
    if (proc.exitCode !== 0) {
      const stderr = proc.stderr.toString().trim();
      return `Error creating PR for ${args.ticket_id}: ${stderr}`;
    }
    return proc.stdout.toString().trim();
  },
};
