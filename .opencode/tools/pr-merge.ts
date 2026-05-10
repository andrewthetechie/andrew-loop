import { z } from "zod";

export default {
  description:
    "Merge a pull request using the repository's configured merge method. " +
    "Returns the merge result. Use `pr-status` first to confirm the PR is mergeable.",
  args: {
    pr: z
      .union([z.string(), z.number()])
      .describe("PR number or URL"),
    method: z
      .enum(["merge", "squash", "rebase"])
      .default("squash")
      .describe("Merge method. Defaults to squash."),
  },
  async execute(
    args: { pr: string | number; method?: "merge" | "squash" | "rebase" },
    context: { directory: string; worktree: string },
  ) {
    const prRef = String(args.pr);
    const method = args.method ?? "squash";
    const cwd = context.directory;

    const flag = method === "squash" ? "--squash" : method === "rebase" ? "--rebase" : "--merge";

    const proc = Bun.spawnSync(
      ["gh", "pr", "merge", prRef, flag, "--auto"],
      { cwd, env: { ...process.env } },
    );

    const stdout = proc.stdout.toString().trim();
    const stderr = proc.stderr.toString().trim();

    if (proc.exitCode !== 0) {
      return `Merge failed (exit ${proc.exitCode}):\n${stderr || stdout}`;
    }

    // Confirm merge state
    const viewProc = Bun.spawnSync(
      ["gh", "pr", "view", prRef, "--json", "state,mergedAt,mergedBy"],
      { cwd, env: { ...process.env } },
    );

    if (viewProc.exitCode === 0) {
      try {
        const result = JSON.parse(viewProc.stdout.toString());
        if (result.state === "MERGED") {
          return `Merged successfully at ${result.mergedAt} by ${result.mergedBy?.login ?? "unknown"}.`;
        }
      } catch {
        // fall through
      }
    }

    return stdout || "Merge command succeeded.";
  },
};
