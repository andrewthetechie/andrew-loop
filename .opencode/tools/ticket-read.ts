import { z } from "zod";

export default {
  description:
    "Read a ticket from the orchestrator database. Returns full ticket details as structured JSON including title, description, acceptance criteria, state, comments, and all metadata.",
  args: {
    ticket_id: z.string().describe("The ticket ID (e.g. ORCH-001)"),
  },
  async execute(
    args: { ticket_id: string },
    context: { directory: string; worktree: string },
  ) {
    const proc = Bun.spawnSync(
      ["uv", "run", "orch", "tickets", "show", args.ticket_id, "--json"],
      {
        env: { ...process.env, ORCH_DB_PATH: process.env.ORCH_DB_PATH ?? ".orchestra/state.db" },
        cwd: context.directory,
      },
    );
    if (proc.exitCode !== 0) {
      const stderr = proc.stderr.toString().trim();
      return `Error reading ticket ${args.ticket_id}: ${stderr}`;
    }
    return proc.stdout.toString().trim();
  },
};
