import { z } from "zod";

export default {
  description:
    "Run all configured validation commands for this project (tests, lint, etc.) and return structured pass/fail results. " +
    "Always use this tool to validate your changes — do not call pytest, ruff, or other validators directly.",
  args: {
    dir: z
      .string()
      .optional()
      .describe(
        "Directory to run validators in. Defaults to the current working directory.",
      ),
  },
  async execute(
    args: { dir?: string },
    context: { directory: string; worktree: string },
  ) {
    const cwd = args.dir ?? context.directory;
    const cmd = [
      "uv",
      "run",
      "orch",
      "validate",
      "--json",
      "--dir",
      cwd,
    ];

    const proc = Bun.spawnSync(cmd, {
      cwd: context.directory,
    });

    if (proc.exitCode !== 0 && proc.stdout.toString().trim() === "") {
      const stderr = proc.stderr.toString().trim();
      return `Error running validators: ${stderr}`;
    }

    const raw = proc.stdout.toString().trim();
    try {
      const data = JSON.parse(raw);
      const lines: string[] = [];

      lines.push(
        `Overall: ${data.all_passed ? "ALL PASS" : "FAILURES PRESENT"}`,
      );
      lines.push("");

      for (const r of data.results ?? []) {
        const status = r.passed ? "PASS" : "FAIL";
        lines.push(`${status}: \`${r.command}\`  (exit ${r.exit_code})`);
        if (!r.passed) {
          const output = [r.stdout, r.stderr]
            .filter(Boolean)
            .join("\n")
            .trim();
          if (output) {
            // Last 30 lines is enough context
            const tail = output.split("\n").slice(-30).join("\n");
            lines.push(tail);
          }
        }
        lines.push("");
      }

      return lines.join("\n");
    } catch {
      return raw;
    }
  },
};
