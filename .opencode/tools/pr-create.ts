import { z } from "zod";

export default {
  description:
    "Create a pull request for a ticket. Calls orch pr create and returns the PR URL on success. Base branch is auto-resolved from the ticket's issue_id.",
  args: {
    ticket_id: z.string().describe("The ticket ID (e.g. ORCH-001)"),
  },
  async execute(
    args: { ticket_id: string },
    context: { directory: string; worktree: string },
  ) {
    const cmd = ["uv", "run", "orch", "pr", "create", args.ticket_id];
    const proc = Bun.spawnSync(cmd, {
      cwd: context.directory,
    });
    if (proc.exitCode !== 0) {
      const stderr = proc.stderr.toString().trim();
      return `Error creating PR for ${args.ticket_id}: ${stderr}`;
    }
    return proc.stdout.toString().trim();
  },
};
