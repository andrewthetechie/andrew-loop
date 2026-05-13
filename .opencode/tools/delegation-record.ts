import { z } from "zod";

export default {
  description:
    "Record a hidden helper delegation event and output summary for parent-ticket observability.",
  args: {
    ticket_id: z.string().describe("The parent ticket ID (e.g. ORCH-001)"),
    helper_role: z
      .string()
      .describe("Hidden helper role used (codebase-scout, leaf-coder, or patch-reviewer)"),
    output: z.string().describe("Helper output or concise summary to persist"),
  },
  async execute(
    args: { ticket_id: string; helper_role: string; output: string },
    context: { directory: string; worktree: string },
  ) {
    const proc = Bun.spawnSync(
      [
        "uv",
        "run",
        "orch",
        "tickets",
        "delegation-record",
        args.ticket_id,
        args.helper_role,
        args.output,
      ],
      {
        cwd: context.directory,
      },
    );
    if (proc.exitCode !== 0) {
      const stderr = proc.stderr.toString().trim();
      return `Error recording delegation output for ${args.ticket_id}: ${stderr}`;
    }
    return proc.stdout.toString().trim();
  },
};
