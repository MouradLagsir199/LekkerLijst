const jsonHeaders = { "Content-Type": "application/json" };
const MAX_BATCH_INPUT_BYTES = 45 * 1024 * 1024;

function json(body: Record<string, unknown>, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: jsonHeaders });
}

function isServiceRoleRequest(request: Request) {
  const authorization = request.headers.get("authorization");
  if (!authorization?.startsWith("Bearer ")) return false;
  const payloadSegment = authorization.slice("Bearer ".length).split(".")[1];
  if (!payloadSegment) return false;
  try {
    const base64 = payloadSegment.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(payloadSegment.length / 4) * 4, "=");
    return JSON.parse(atob(base64)).role === "service_role";
  } catch {
    return false;
  }
}

function openAiHeaders() {
  const apiKey = Deno.env.get("OPENAI_API_KEY");
  if (!apiKey) throw new Error("OPENAI_API_KEY is not configured");
  return { Authorization: `Bearer ${apiKey}` };
}

async function openAiError(response: Response) {
  const message = (await response.text()).slice(0, 4_000);
  return json({ error: "OpenAI request failed", status: response.status, detail: message }, 502);
}

async function openAiJson(path: string, init: RequestInit) {
  return fetch(`https://api.openai.com/v1${path}`, {
    ...init,
    headers: { ...openAiHeaders(), ...(init.headers ?? {}) },
  });
}

Deno.serve(async (request) => {
  if (request.method !== "POST") return json({ error: "Method not allowed" }, 405);
  if (!isServiceRoleRequest(request)) return json({ error: "Service-role authorization required" }, 403);

  let payload: Record<string, unknown>;
  try {
    payload = await request.json();
  } catch {
    return json({ error: "Invalid JSON body" }, 400);
  }

  try {
    const action = payload.action;
    if (action === "submit") {
      const inputJsonl = payload.inputJsonl;
      if (typeof inputJsonl !== "string" || !inputJsonl.trim()) {
        return json({ error: "inputJsonl is required" }, 400);
      }
      if (new TextEncoder().encode(inputJsonl).byteLength > MAX_BATCH_INPUT_BYTES) {
        return json({ error: "Batch input exceeds the bridge size limit" }, 413);
      }

      const filename = typeof payload.filename === "string" ? payload.filename : "catalog-requests.jsonl";
      const form = new FormData();
      form.set("purpose", "batch");
      form.set("file", new File([inputJsonl], filename, { type: "application/jsonl" }));
      const fileResponse = await openAiJson("/files", { method: "POST", body: form });
      if (!fileResponse.ok) return openAiError(fileResponse);
      const inputFile = await fileResponse.json();

      const batchResponse = await openAiJson("/batches", {
        method: "POST",
        headers: jsonHeaders,
        body: JSON.stringify({
          input_file_id: inputFile.id,
          endpoint: "/v1/responses",
          completion_window: "24h",
        }),
      });
      if (!batchResponse.ok) return openAiError(batchResponse);
      return json({ batch: await batchResponse.json(), inputFileId: inputFile.id });
    }

    if (action === "status") {
      const batchId = payload.batchId;
      if (typeof batchId !== "string" || !batchId) return json({ error: "batchId is required" }, 400);
      const batchResponse = await openAiJson(`/batches/${encodeURIComponent(batchId)}`, { method: "GET" });
      if (!batchResponse.ok) return openAiError(batchResponse);
      return json({ batch: await batchResponse.json() });
    }

    if (action === "cancel") {
      const batchId = payload.batchId;
      if (typeof batchId !== "string" || !batchId) return json({ error: "batchId is required" }, 400);
      const batchResponse = await openAiJson(`/batches/${encodeURIComponent(batchId)}/cancel`, { method: "POST" });
      if (!batchResponse.ok) return openAiError(batchResponse);
      return json({ batch: await batchResponse.json() });
    }

    if (action === "download") {
      const batchId = payload.batchId;
      if (typeof batchId !== "string" || !batchId) return json({ error: "batchId is required" }, 400);
      const batchResponse = await openAiJson(`/batches/${encodeURIComponent(batchId)}`, { method: "GET" });
      if (!batchResponse.ok) return openAiError(batchResponse);
      const batch = await batchResponse.json();
      if (typeof batch.output_file_id !== "string" || !batch.output_file_id) {
        return json({ error: "Batch has no output file yet", status: batch.status }, 409);
      }
      const outputResponse = await openAiJson(`/files/${encodeURIComponent(batch.output_file_id)}/content`, { method: "GET" });
      if (!outputResponse.ok) return openAiError(outputResponse);
      return new Response(await outputResponse.arrayBuffer(), {
        status: 200,
        headers: { "Content-Type": "application/jsonl; charset=utf-8" },
      });
    }

    if (action === "response") {
      const requestBody = payload.requestBody;
      if (!requestBody || typeof requestBody !== "object" || Array.isArray(requestBody)) {
        return json({ error: "requestBody must be an object" }, 400);
      }
      const response = await openAiJson("/responses", {
        method: "POST",
        headers: jsonHeaders,
        body: JSON.stringify(requestBody),
      });
      if (!response.ok) return openAiError(response);
      return json({ response: await response.json() });
    }

    return json({ error: "Unsupported action" }, 400);
  } catch (error) {
    console.error("catalog-openai-bridge error", error instanceof Error ? error.message : error);
    return json({ error: "Catalog OpenAI bridge failed" }, 500);
  }
});
