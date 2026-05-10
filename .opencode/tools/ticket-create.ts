import { z } from "zod";

export default {
  description:
    "Create a new ticket in the orchestrator database. Accepts full ticket details and returns the created ticket ID.",
  args: {
    title: z.string().describe("Ticket title"),
    description: z.string().describe("What needs to be done"),
    acceptance_criteria: z
      .string()
      .describe("Acceptance criteria (markdown checklist)"),
    risk_score: z
      .number()
      .int()
      .min(1)
      .max(5)
      .optional()
      .describe("Risk score 1-5 (1=low risk, 5=high risk/security sensitive)"),
    issue_id: z
      .number()
      .int()
      .optional()
      .describe("GitHub issue ID this ticket should be linked to"),
    file_paths: z
      .string()
      .optional()
      .describe("Relevant file paths, newline-separated"),
    test_expectations: z
      .string()
      .optional()
      .describe("Test expectations or validation commands"),
    depends_on: z
      .array(z.string())
      .optional()
      .describe("Ticket IDs this ticket depends on"),
  },
  async execute(
    args: {
      title: string;
      description: string;
      acceptance_criteria: string;
      risk_score?: number;
      issue_id?: number;
      file_paths?: string;
      test_expectations?: string;
      depends_on?: string[];
    },
    context: { directory: string; worktree: string },
  ) {
    const lines: string[] = [
      `title: ${JSON.stringify(args.title)}`,
      `description: |`,
      ...args.description.split("\n").map((l) => `  ${l}`),
      `acceptance_criteria: |`,
      ...args.acceptance_criteria.split("\n").map((l) => `  ${l}`),
      `risk_score: ${args.risk_score ?? null}`,
      `file_paths: ${args.file_paths ? JSON.stringify(args.file_paths) : null}`,
      `test_expectations: ${args.test_expectations ? JSON.stringify(args.test_expectations) : null}`,
    ];
    const yaml = lines.join("\n") + "\n";

    const tmpFile = `/tmp/orch-ticket-${Date.now()}.yaml`;
    await Bun.write(tmpFile, yaml);

    try {
      const cmd = [
        "uv",
        "run",
        "orch",
        "tickets",
        "create",
      ];
      if (args.issue_id !== undefined) {
        cmd.push("--issue-id", String(args.issue_id));
      }
      cmd.push("--from-file", tmpFile);
      if (args.depends_on?.length) {
        for (const dep of args.depends_on) {
          cmd.push("--depends-on", dep);
        }
      }

      const proc = Bun.spawnSync(cmd, {
        cwd: context.directory,
      });

      if (proc.exitCode !== 0) {
        const stderr = proc.stderr.toString().trim();
        return `Error creating ticket: ${stderr}`;
      }
      return proc.stdout.toString().trim();
    } finally {
      try {
        import("fs").then(({ unlinkSync }) => unlinkSync(tmpFile));
      } catch {
        // best-effort cleanup
      }
    }
  },
};
