import { z } from "zod";

export default {
  description:
    "List all tickets with their current states. Returns the full workflow status as structured JSON.",
  args: {
    state_filter: z
      .string()
      .optional()
      .describe("Filter by ticket state (e.g. 'In Progress', 'Code Review')"),
  },
  async execute(
    args: { state_filter?: string },
    context: { directory: string; worktree: string },
  ) {
    const cmd = ["uv", "run", "orch", "status", "--json"];
    const proc = Bun.spawnSync(cmd, {
      env: {
        ...process.env,
        ORCH_DB_PATH:
          process.env.ORCH_DB_PATH ?? ".orchestra/state.db",
      },
      cwd: context.directory,
    });
    if (proc.exitCode !== 0) {
      const stderr = proc.stderr.toString().trim();
      return `Error listing tickets: ${stderr}`;
    }
    const output = proc.stdout.toString().trim();
    if (!args.state_filter) {
      return output;
    }
    try {
      const tickets = JSON.parse(output);
      const filtered = tickets.filter(
        (t: { state: string }) => t.state === args.state_filter,
      );
      return JSON.stringify(filtered, null, 2);
    } catch {
      return output;
    }
  },
};
