// Blocks hidden-helper Task launches on orch's cross-process helper slots.
//
// OpenCode's native Task tool is still used for delegation. The plugin changes
// only permission timing: patch-reviewer and codebase-scout task calls are set
// to "ask" in the generated agent config, then this hook acquires a configured
// orch slot and allows the call. The slot is held until the Task tool finishes.

const HIDDEN_HELPERS = new Set(["patch-reviewer", "codebase-scout"]);

function asArray(value) {
  if (Array.isArray(value)) return value;
  if (typeof value === "string") return [value];
  return [];
}

function helperFrom(input) {
  const candidates = [
    ...asArray(input.pattern),
    ...asArray(input.patterns),
    input.metadata?.agent,
    input.metadata?.subagent_type,
    input.metadata?.helper,
    input.metadata?.role,
    input.title,
  ]
    .filter(Boolean)
    .map((value) => String(value));

  for (const candidate of candidates) {
    for (const helper of HIDDEN_HELPERS) {
      if (candidate.includes(helper)) return helper;
    }
  }
  return undefined;
}

function permissionName(input) {
  return String(input.type ?? input.permission ?? "");
}

async function waitForAcquire(proc) {
  const decoder = new TextDecoder();
  let stdout = "";
  let stderr = "";

  const stdoutPromise = (async () => {
    if (!proc.stdout) return;
    for await (const chunk of proc.stdout) {
      stdout += decoder.decode(chunk);
      if (stdout.includes("\n")) return;
    }
  })();

  const stderrPromise = (async () => {
    if (!proc.stderr) return;
    for await (const chunk of proc.stderr) {
      stderr += decoder.decode(chunk);
    }
  })();

  await Promise.race([stdoutPromise, proc.exited]);
  if (stdout.startsWith("ACQUIRED ")) return;

  try {
    proc.kill();
  } catch {}
  await stderrPromise.catch(() => {});
  throw new Error((stderr || stdout || "hidden-helper slot acquisition failed").trim());
}

export const HiddenHelperSemaphorePlugin = async () => {
  const held = new Map();

  function release(callID) {
    const proc = held.get(callID);
    if (!proc) return;
    held.delete(callID);
    try {
      proc.kill();
    } catch {}
  }

  return {
    "permission.ask": async (input, output) => {
      if (permissionName(input) !== "task") return;
      const helper = helperFrom(input);
      if (!helper) return;

      const callID = input.callID ?? input.tool?.callID ?? `${input.sessionID}:${Date.now()}`;
      const proc = Bun.spawn(["uv", "run", "orch", "hidden-helper-slot", "hold", helper], {
        cwd: process.cwd(),
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
        env: process.env,
      });

      try {
        await waitForAcquire(proc);
      } catch (err) {
        output.status = "deny";
        throw err;
      }

      held.set(callID, proc);
      output.status = "allow";
    },

    "tool.execute.after": async (input) => {
      if (String(input.tool).toLowerCase() !== "task") return;
      release(input.callID);
    },
  };
};
