import { z } from "zod";

export default {
  description:
    "Push updates for an existing pull request linked to a ticket. Calls orch pr update and returns success or error.",
  args: {
    ticket_id: z.string().describe("The ticket ID (e.g. ORCH-001)"),
  },
  async execute(
    args: { ticket_id: string },
    context: { directory: string; worktree: string },
  ) {
    const proc = Bun.spawnSync(
      ["uv", "run", "orch", "pr", "update", args.ticket_id],
      {
        env: {
          ...process.env,
          ORCH_DB_PATH: process.env.ORCH_DB_PATH ?? ".orchestra/state.db",
        },
        cwd: context.directory,
      },
    );
    if (proc.exitCode !== 0) {
      const stderr = proc.stderr.toString().trim();
      return `Error updating PR for ${args.ticket_id}: ${stderr}`;
    }
    return `PR updated for ${args.ticket_id}`;
  },
};
