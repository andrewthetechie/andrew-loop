import { z } from "zod";

export default {
  description:
    "Add a comment to a ticket. Records who said what for audit trail and agent coordination.",
  args: {
    ticket_id: z.string().describe("The ticket ID (e.g. ORCH-001)"),
    body: z.string().describe("The comment text"),
    author: z
      .string()
      .describe("Comment author (e.g. 'coder', 'reviewer', 'human')"),
  },
  async execute(
    args: { ticket_id: string; body: string; author: string },
    context: { directory: string; worktree: string },
  ) {
    const proc = Bun.spawnSync(
      [
        "uv",
        "run",
        "orch",
        "tickets",
        "comment",
        args.ticket_id,
        args.body,
        "--author",
        args.author,
      ],
      {
        cwd: context.directory,
      },
    );
    if (proc.exitCode !== 0) {
      const stderr = proc.stderr.toString().trim();
      return `Error adding comment to ${args.ticket_id}: ${stderr}`;
    }
    return proc.stdout.toString().trim();
  },
};
