import { z } from "zod";

export default {
  description:
    "Get the full status of a pull request: state, mergeability, review decision, checks, and basic metadata. " +
    "Use this instead of calling `gh pr view` directly — it uses the correct field names.",
  args: {
    pr: z
      .union([z.string(), z.number()])
      .describe("PR number or URL"),
  },
  async execute(
    args: { pr: string | number },
    context: { directory: string; worktree: string },
  ) {
    const prRef = String(args.pr);
    const cwd = context.directory;

    // Get PR metadata with confirmed-valid fields
    const viewProc = Bun.spawnSync(
      [
        "gh", "pr", "view", prRef,
        "--json",
        "number,title,state,mergeable,mergeStateStatus,reviewDecision,mergedAt,mergedBy,headRefName,baseRefName,url",
      ],
      { cwd, env: { ...process.env } },
    );

    if (viewProc.exitCode !== 0) {
      return `Error fetching PR: ${viewProc.stderr.toString().trim()}`;
    }

    let pr: Record<string, unknown> = {};
    try {
      pr = JSON.parse(viewProc.stdout.toString());
    } catch {
      return viewProc.stdout.toString();
    }

    // Get check run results
    const checksProc = Bun.spawnSync(
      ["gh", "pr", "checks", prRef, "--json", "name,state,completedAt,description"],
      { cwd, env: { ...process.env } },
    );

    let checks: unknown[] = [];
    if (checksProc.exitCode === 0) {
      try {
        checks = JSON.parse(checksProc.stdout.toString());
      } catch {
        // ignore parse failure
      }
    }

    const allChecksPassing =
      checks.length === 0 ||
      checks.every((c: Record<string, unknown>) => c.state === "SUCCESS" || c.state === "SKIPPED");

    const lines = [
      `PR #${pr.number}: ${pr.title}`,
      `State:          ${pr.state}`,
      `Mergeable:      ${pr.mergeable}`,
      `Merge status:   ${pr.mergeStateStatus}`,
      `Review:         ${pr.reviewDecision || "(none)"}`,
      `Merged at:      ${pr.mergedAt || "(not merged)"}`,
      `Branch:         ${pr.headRefName} → ${pr.baseRefName}`,
      `URL:            ${pr.url}`,
      "",
      `Checks (${checks.length}): ${allChecksPassing ? "ALL PASSING" : "FAILURES PRESENT"}`,
    ];

    for (const c of checks as Record<string, string>[]) {
      lines.push(`  ${c.state.padEnd(8)} ${c.name}`);
    }

    return lines.join("\n");
  },
};
