import { z } from "zod";

export default {
  description:
    "Update a ticket in the orchestrator database. Can change state, link a PR, or update the assignee. Returns confirmation or error message.",
  args: {
    ticket_id: z.string().describe("The ticket ID (e.g. ORCH-001)"),
    state: z
      .string()
      .optional()
      .describe("New state (e.g. 'In Progress', 'Code Review')"),
    linked_pr: z.string().optional().describe("PR URL to link to the ticket"),
    assignee: z.string().optional().describe("Assignee name"),
  },
  async execute(
    args: {
      ticket_id: string;
      state?: string;
      linked_pr?: string;
      assignee?: string;
    },
    context: { directory: string; worktree: string },
  ) {
    const cmd = ["uv", "run", "orch", "tickets", "update", args.ticket_id];
    if (args.state) {
      cmd.push("--state", args.state);
    }
    if (args.linked_pr) {
      cmd.push("--linked-pr", args.linked_pr);
    }
    if (args.assignee) {
      cmd.push("--assignee", args.assignee);
    }
    if (cmd.length === 6) {
      return "No updates requested. Provide at least one of: state, linked_pr, assignee.";
    }
    const proc = Bun.spawnSync(cmd, {
      cwd: context.directory,
    });
    if (proc.exitCode !== 0) {
      const stderr = proc.stderr.toString().trim();
      return `Error updating ticket ${args.ticket_id}: ${stderr}`;
    }
    return proc.stdout.toString().trim();
  },
};
