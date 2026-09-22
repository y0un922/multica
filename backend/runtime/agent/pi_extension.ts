import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

/** Loaded explicitly by PiAgentExecutor; no background resources. */
export default function (pi: ExtensionAPI) {
  const url = process.env.TBM_BRIDGE_URL;
  const token = process.env.TBM_BRIDGE_TOKEN;
  const capabilities: string[] = JSON.parse(process.env.TBM_CAPABILITIES ?? "[]");
  const specs = JSON.parse(process.env.TBM_CAPABILITY_SPECS ?? "{}");
  const outputSchema = JSON.parse(process.env.TBM_OUTPUT_SCHEMA ?? "null");
  if (!url || !token) throw new Error("Missing execution bridge configuration");

  async function post(path: string, body: unknown, signal?: AbortSignal) {
    const response = await fetch(`${url}${path}`, {
      method: "POST",
      headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    if (!response.ok) throw new Error(`Bridge rejected request (${response.status})`);
    const result = await response.json() as { ok: boolean; value?: unknown; error?: string };
    if (!result.ok) throw new Error(result.error ?? "Capability failed");
    return result;
  }

  if (capabilities.length) {
    pi.registerTool({
      name: "invoke_capability",
      label: "Workflow Capability",
      description: `Call a workflow capability. Allowed identifiers: ${capabilities.join(", ")}. ` +
        "Use the arguments required by the workflow goal; never invent missing required information. " +
        `Capability contracts: ${JSON.stringify(specs)}`,
      parameters: Type.Object({
        capability: Type.String(),
        args: Type.Record(Type.String(), Type.Unknown()),
      }),
      async execute(callId, params, signal) {
        const result = await post("/invoke", {
          call_id: callId, capability: params.capability, args: params.args,
        }, signal);
        const text = JSON.stringify(result.value);
        // Do not quietly truncate structured evidence: report oversized results.
        if (Buffer.byteLength(text, "utf8") > 50 * 1024) {
          throw new Error("Capability result exceeds 50KB; request a smaller result");
        }
        return { content: [{ type: "text", text }], details: {} };
      },
    });
  }
  const resultParameters = Type.Object({ value: Type.Record(Type.String(), Type.Unknown()) });
  // Workflow uses a bounded JSON Schema dialect; Python revalidates before accepting.
  if (outputSchema) resultParameters.properties.value = outputSchema;
  pi.registerTool({
    name: "submit_result",
    label: "Workflow Result",
    description: "Submit the final JSON object for this workflow node. Call once, after all other tools finish. " +
      "Do not call alongside capability tools. This is the final action.",
    parameters: resultParameters,
    async execute(_callId, params, signal) {
      await post("/result", { value: params.value }, signal);
      return {
        content: [{ type: "text", text: "Workflow result submitted" }],
        details: {}, terminate: true,
      };
    },
  });
}
