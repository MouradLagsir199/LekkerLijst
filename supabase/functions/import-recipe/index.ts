const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS"
};

const MAX_SOCIAL_VIDEO_SECONDS = 5 * 60;
const UNIVERSAL_SOCIAL_TRANSCRIPT_ACTOR = "CVQmx5Se22zxPaWc1";
const PINTEREST_MEDIA_ACTOR = "PQu1LW4FWyPQFFX8F";
const DIRECT_MEDIA_TRANSCRIPT_ACTOR = "VZTENHFJOyJEGIKCv";

const recipeJsonSchema = {
  type: "object",
  additionalProperties: false,
  required: [
    "title",
    "description",
    "servings",
    "prepTimeMinutes",
    "cookTimeMinutes",
    "totalTimeMinutes",
    "ingredients",
    "instructions",
    "tags",
    "sourceUrl",
    "sourcePlatform",
    "confidenceScore",
    "completeness"
  ],
  properties: {
    title: { type: "string" },
    description: { type: ["string", "null"] },
    servings: { type: ["integer", "null"], minimum: 1 },
    prepTimeMinutes: { type: ["integer", "null"], minimum: 0 },
    cookTimeMinutes: { type: ["integer", "null"], minimum: 0 },
    totalTimeMinutes: { type: ["integer", "null"], minimum: 0 },
    ingredients: {
      type: "array",
      minItems: 1,
      items: {
        type: "object",
        additionalProperties: false,
        required: [
          "rawText",
          "quantity",
          "unit",
          "ingredientName",
          "normalizedIngredientName",
          "dutchIngredientName",
          "preparation",
          "optional",
          "ingredientSource",
          "quantitySource"
        ],
        properties: {
          rawText: { type: "string" },
          quantity: { type: ["number", "null"] },
          unit: { type: ["string", "null"] },
          ingredientName: { type: "string" },
          normalizedIngredientName: { type: ["string", "null"] },
          dutchIngredientName: { type: ["string", "null"] },
          preparation: { type: ["string", "null"] },
          optional: { type: "boolean" },
          ingredientSource: { type: "string", enum: ["source", "ai_suggestion"] },
          quantitySource: { type: "string", enum: ["source", "missing", "ai_suggestion"] }
        }
      }
    },
    instructions: {
      type: "array",
      minItems: 1,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["text", "source"],
        properties: {
          text: { type: "string" },
          source: { type: "string", enum: ["source", "ai_suggestion"] }
        }
      }
    },
    tags: {
      type: "array",
      maxItems: 8,
      items: { type: "string", minLength: 1, maxLength: 32 }
    },
    sourceUrl: { type: ["string", "null"] },
    sourcePlatform: {
      anyOf: [
        { type: "string", enum: ["instagram", "tiktok", "youtube", "facebook", "pinterest", "blog", "manual"] },
        { type: "null" }
      ]
    },
    confidenceScore: { type: "number", minimum: 0, maximum: 1 },
    completeness: {
      type: "object",
      additionalProperties: false,
      required: ["status", "missingFields"],
      properties: {
        status: { type: "string", enum: ["complete", "incomplete"] },
        missingFields: {
          type: "array",
          items: { type: "string", enum: ["quantities", "ingredients", "instructions", "servings"] }
        }
      }
    }
  }
};

type ParsedBody = {
  action?: "parse" | "suggest_completion";
  rawText?: string;
  sourceUrl?: string | null;
  recipe?: unknown;
  sourceText?: string;
  servings?: number | null;
};

type SourcePlatform = "instagram" | "tiktok" | "youtube" | "facebook" | "pinterest" | "blog" | "manual";

type LinkContext = {
  url: string;
  canonicalUrl?: string;
  linkedRecipeUrl?: string;
  platform: SourcePlatform;
  title?: string;
  description?: string;
  imageUrl?: string;
  siteName?: string;
  oembed?: Record<string, unknown>;
  transcript?: string;
  recipes: ExtractedRecipe[];
  extractionWarnings: string[];
};

type ExtractedRecipe = {
  name?: string;
  description?: string;
  recipeIngredient?: string[];
  recipeInstructions?: string[];
  recipeYield?: string;
  prepTime?: string;
  cookTime?: string;
  totalTime?: string;
};

type PageMetadata = {
  finalUrl: string;
  canonicalUrl?: string;
  sourceRecipeUrl?: string;
  title?: string;
  description?: string;
  imageUrl?: string;
  siteName?: string;
  recipes: ExtractedRecipe[];
};

type ApifyInstagramPost = Record<string, unknown>;

type PinterestMedia = {
  canonicalUrl?: string;
  linkedRecipeUrl?: string;
  title?: string;
  description?: string;
  imageUrl?: string;
  siteName?: string;
  videoUrl?: string;
  captionsUrl?: string;
  durationSeconds?: number;
};

type PinterestVideo = {
  url: string;
  thumbnailUrl?: string;
  captionsUrl?: string;
  durationSeconds?: number;
};

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  if (request.method !== "POST") {
    return json({ error: "Method not allowed" }, 405);
  }

  const apiKey = Deno.env.get("OPENAI_API_KEY");
  if (!apiKey) {
    return json({ error: "OPENAI_API_KEY is not configured for this function" }, 500);
  }

  let body: ParsedBody;
  try {
    body = await request.json();
  } catch {
    return json({ error: "Request body must be JSON" }, 400);
  }

  if (body.action === "suggest_completion") {
    return await suggestRecipeCompletion(body, apiKey);
  }

  const rawText = body.rawText?.trim() ?? "";
  const sourceUrl = body.sourceUrl?.trim() ?? "";

  if (!rawText && !sourceUrl) {
    return json({ error: "rawText or sourceUrl is required" }, 400);
  }

  if (sourceUrl && !isSupportedHttpUrl(sourceUrl)) {
    return json(
      {
        error: "Ongeldige link",
        details: "Gebruik een volledige http- of https-link."
      },
      400
    );
  }

  if (rawText.length > 12_000) {
    return json({ error: "Recipe text is too long for this import path" }, 413);
  }

  let linkContext: LinkContext | null = null;
  let sourceText = rawText;

  if (sourceUrl) {
    try {
      linkContext = await collectLinkContext(sourceUrl);
    } catch (error) {
      linkContext = fallbackLinkContext(sourceUrl, error);
    }

    if (!rawText && linkContext && !hasUsableRecipeSignal(linkContext)) {
      return json(
        {
          error: "Geen recepttekst gevonden in deze link",
          details: missingRecipeSignalDetails(linkContext)
        },
        422
      );
    }

    if (linkContext) {
      sourceText = [rawText, formatLinkContext(linkContext)].filter(Boolean).join("\n\n");
    }
  }

  if (linkContext?.extractionWarnings.length) {
    console.warn("Import extraction warnings", {
      url: sourceUrl,
      platform: linkContext.platform,
      warnings: linkContext.extractionWarnings
    });
  }

  if (sourceText.length > 20_000) {
    sourceText = sourceText.slice(0, 20_000);
  }

  if (sourceText.trim().length < 20) {
    if (sourceUrl && linkContext) {
      return json(
        {
          error: "Geen recepttekst gevonden in deze link",
          details: missingRecipeSignalDetails(linkContext)
        },
        422
      );
    }

    return json({ error: "Niet genoeg receptinformatie gevonden in deze invoer" }, 422);
  }

  const model = Deno.env.get("OPENAI_RECIPE_MODEL") || "gpt-5.4-mini";
  const openAiResponse = await fetch("https://api.openai.com/v1/responses", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      model,
      input: [
        {
          role: "system",
          content:
            "Faithfully extract a recipe from messy user-provided text, social metadata, or a transcript. Return only the requested JSON schema. All user-visible text must be natural Dutch: translate titles, descriptions, ingredients, preparation notes, and instructions while preserving their meaning. Normalize ingredients for Dutch supermarket shopping. Generate 1 to 8 short lowercase Dutch filter tags that describe the dish, cuisine, main ingredient, method, or diet only when supported by the source. Never invent quantities, ingredients, servings, or missing/cut-off instructions. Mark an ingredient quantity as missing when it is not stated. Mark completeness incomplete and list every missing field when the source is partial. Set all extracted ingredient and instruction provenance to source."
        },
        {
          role: "user",
          content: [
            sourceUrl ? `Source URL: ${sourceUrl}` : "Source URL: none",
            `Detected platform: ${linkContext?.platform ?? (sourceUrl ? detectPlatform(sourceUrl) : "manual")}`,
            "Source content:",
            sourceText
          ].join("\n")
        }
      ],
      text: {
        format: {
          type: "json_schema",
          name: "parsed_recipe",
          strict: true,
          schema: recipeJsonSchema
        }
      }
    })
  });

  const openAiJson = await openAiResponse.json();
  if (!openAiResponse.ok) {
    return json(
      {
        error: "OpenAI recipe parsing failed",
        details: openAiJson.error?.message ?? openAiJson
      },
      502
    );
  }

  const outputText = extractOutputText(openAiJson);
  if (!outputText) {
    return json({ error: "OpenAI returned no structured recipe text" }, 502);
  }

  try {
    const recipe = JSON.parse(outputText) as Record<string, unknown>;
    if (linkContext?.imageUrl) recipe.imageUrl = linkContext.imageUrl;
    return json({ recipe, model, source: linkContext, completionSourceText: sourceText.slice(0, 12_000) }, 200);
  } catch {
    return json({ error: "OpenAI returned invalid JSON", rawOutput: outputText }, 502);
  }
});

async function suggestRecipeCompletion(body: ParsedBody, apiKey: string) {
  const sourceText = body.sourceText?.trim().slice(0, 12_000) ?? "";
  if (!body.recipe || !sourceText) {
    return json({ error: "recipe and sourceText are required for an AI proposal" }, 400);
  }

  const servings = typeof body.servings === "number" && body.servings > 0 ? body.servings : null;
  const model = Deno.env.get("OPENAI_RECIPE_MODEL") || "gpt-5.4-mini";
  const openAiResponse = await fetch("https://api.openai.com/v1/responses", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      model,
      input: [
        {
          role: "system",
          content:
            "Create a proposed completion for an incomplete recipe. Treat the Faithful extraction JSON as immutable evidence: keep every source ingredient and every source instruction verbatim, in the same order, with source provenance. Do not remove, merge, rewrite, or reclassify those source records. For an existing source ingredient with a missing quantity, keep its source fields and set only the proposed quantity/unit to ai_suggestion provenance. Add plausible new ingredients, servings, and extra cut-off instructions only when needed to make the recipe cookable; every added or materially changed value must use ai_suggestion provenance. Return completeness complete with no missing fields. This is a proposal for user review, not a claim about the original post."
        },
        {
          role: "user",
          content: [
            servings ? `Requested servings: ${servings}` : "Requested servings: choose a reasonable value and mark it ai_suggestion.",
            "Faithful extraction JSON:",
            JSON.stringify(body.recipe),
            "Original source text:",
            sourceText
          ].join("\n\n")
        }
      ],
      text: {
        format: {
          type: "json_schema",
          name: "completed_recipe_proposal",
          strict: true,
          schema: recipeJsonSchema
        }
      }
    })
  });

  const openAiJson = await openAiResponse.json();
  if (!openAiResponse.ok) {
    return json(
      {
        error: "OpenAI recipe completion failed",
        details: openAiJson.error?.message ?? openAiJson
      },
      502
    );
  }

  const outputText = extractOutputText(openAiJson);
  if (!outputText) {
    return json({ error: "OpenAI returned no completion proposal" }, 502);
  }

  try {
    const recipe = JSON.parse(outputText) as Record<string, unknown>;
    if (isRecord(body.recipe) && typeof body.recipe.imageUrl === "string") recipe.imageUrl = body.recipe.imageUrl;
    return json({ recipe, model }, 200);
  } catch {
    return json({ error: "OpenAI returned invalid proposal JSON", rawOutput: outputText }, 502);
  }
}

async function collectLinkContext(url: string): Promise<LinkContext> {
  const parsedUrl = new URL(url);
  if (!["http:", "https:"].includes(parsedUrl.protocol)) {
    throw new Error("Only http and https links are supported");
  }

  const platform = detectPlatform(url);
  const extractionWarnings: string[] = [];
  const [pageResult, apifyResult, pinterestResult] = await Promise.allSettled([
    fetchPageMetadata(url),
    platform === "instagram"
      ? fetchInstagramPostFromApify(url)
      : platform === "facebook"
        ? fetchFacebookPostFromApify(url)
        : Promise.resolve(null),
    platform === "pinterest" ? fetchPinterestMediaFromApify(url) : Promise.resolve(null)
  ]);

  const page = pageResult.status === "fulfilled" ? pageResult.value : null;
  if (pageResult.status === "rejected") {
    extractionWarnings.push(pageResult.reason?.message ?? "Page metadata extraction failed");
  }

  const apifyPost = apifyResult.status === "fulfilled" ? apifyResult.value : null;
  if (apifyResult.status === "rejected") {
    extractionWarnings.push(apifyResult.reason?.message ?? "Apify post extraction failed");
  }

  const pinterestMedia = pinterestResult.status === "fulfilled" ? pinterestResult.value : null;
  if (pinterestResult.status === "rejected") {
    extractionWarnings.push(pinterestResult.reason?.message ?? "Pinterest media extraction failed");
  }

  const metadataUrl = page?.canonicalUrl ?? page?.finalUrl ?? url;
  let oembed: Record<string, unknown> | null = null;
  try {
    oembed = await fetchPlatformOembed(metadataUrl, platform);
  } catch (error) {
    extractionWarnings.push(error instanceof Error ? error.message : "oEmbed extraction failed");
  }

  let linkedRecipePage: PageMetadata | null = null;
  const linkedRecipeUrl = platform === "pinterest"
    ? getSafeHttpUrl(pinterestMedia?.linkedRecipeUrl ?? page?.sourceRecipeUrl)
    : undefined;
  if (linkedRecipeUrl) {
    try {
      linkedRecipePage = await fetchPageMetadata(linkedRecipeUrl);
    } catch (error) {
      extractionWarnings.push(error instanceof Error ? error.message : "Pinterest source recipe extraction failed");
    }
  }

  const title = pickString(apifyPost?.title, linkedRecipePage?.title, pinterestMedia?.title, oembed?.title, page?.title);
  const description = pickString(
    apifyPost?.description,
    linkedRecipePage?.description,
    pinterestMedia?.description,
    page?.description,
    oembed?.author_name,
    stripHtml(String(oembed?.html ?? ""))
  );
  const siteName = pickString(apifyPost?.siteName, linkedRecipePage?.siteName, pinterestMedia?.siteName, page?.siteName, oembed?.provider_name);
  const imageUrl = pickImageUrl(
    apifyPost?.imageUrl,
    pinterestMedia?.imageUrl,
    linkedRecipePage?.imageUrl,
    oembed?.thumbnail_url,
    page?.imageUrl
  );

  const baseContext: LinkContext = {
    url,
    canonicalUrl: apifyPost?.canonicalUrl ?? pinterestMedia?.canonicalUrl ?? (metadataUrl !== url ? metadataUrl : undefined),
    linkedRecipeUrl,
    platform,
    title,
    description,
    imageUrl,
    siteName,
    oembed: oembed ?? undefined,
    recipes: [...(linkedRecipePage?.recipes ?? []), ...(page?.recipes ?? [])].slice(0, 3),
    extractionWarnings
  };

  let transcript: string | undefined;
  const needsTranscript = !hasLikelyCompleteRecipeSignal(baseContext);

  // Always inspect captions and metadata before paying for transcription. This avoids
  // processing a video when the post already contains a complete written recipe.
  if (platform === "instagram" && needsTranscript && (isInstagramReelUrl(url) || !hasUsableRecipeSignal(baseContext))) {
    try {
      transcript = await fetchInstagramTranscriptFromApify(url);
    } catch (error) {
      extractionWarnings.push(error instanceof Error ? error.message : "Instagram transcript extraction failed");
    }
  }

  if (
    !transcript &&
    needsTranscript &&
    (platform === "tiktok" ||
      (platform === "facebook" &&
        (isFacebookVideoUrl(url) || isFacebookVideoUrl(metadataUrl) || !hasUsableRecipeSignal(baseContext))))
  ) {
    try {
      transcript = await fetchUniversalSocialTranscriptFromApify(url, platform);
    } catch (error) {
      extractionWarnings.push(error instanceof Error ? error.message : `${platform} transcript extraction failed`);
    }
  }

  if (!transcript && platform === "pinterest" && needsTranscript && pinterestMedia?.videoUrl) {
    if (pinterestMedia.captionsUrl) {
      try {
        transcript = await fetchPublicCaptionTranscript(pinterestMedia.captionsUrl);
      } catch (error) {
        extractionWarnings.push(error instanceof Error ? error.message : "Pinterest captions extraction failed");
      }
    }
    if (!transcript) {
      try {
        transcript = await fetchDirectMediaTranscriptFromApify(pinterestMedia);
      } catch (error) {
        extractionWarnings.push(error instanceof Error ? error.message : "Pinterest transcript extraction failed");
      }
    }
  }

  return {
    ...baseContext,
    transcript
  };
}

async function fetchInstagramPostFromApify(url: string): Promise<PageMetadata> {
  const items = await runApifyActor("apify~instagram-post-scraper", {
    username: [url],
    resultsLimit: 1,
    dataDetailLevel: "basicData"
  });
  if (!Array.isArray(items) || !isRecord(items[0])) {
    throw new Error("Apify returned no Instagram post data");
  }

  const post = items[0] as ApifyInstagramPost;
  const caption = pickPostText(post, "caption", "description", "text");
  const ownerUsername = pickPostText(post, "ownerUsername", "username");
  const inputUrl = pickPostText(post, "inputUrl", "url");

  if (!caption) {
    throw new Error("Apify returned an Instagram post without a caption");
  }

  return {
    finalUrl: inputUrl ?? url,
    canonicalUrl: inputUrl ?? url,
    title: postTitle(caption, "Instagram", ownerUsername),
    description: caption.slice(0, 12_000),
    imageUrl: pickPostImage(post),
    siteName: ownerUsername ? `Instagram - @${ownerUsername}` : "Instagram",
    recipes: []
  };
}

async function fetchFacebookPostFromApify(url: string): Promise<PageMetadata> {
  const items = await runApifyActor("KoJrdxJCTtpon81KY", {
    startUrls: [{ url }],
    resultsLimit: 1,
    captionText: true
  });
  if (!Array.isArray(items) || !isRecord(items[0])) {
    throw new Error("Apify returned no Facebook post data");
  }

  const post = items[0] as ApifyInstagramPost;
  const sharedPost = getPostRecord(post, "sharedPost");
  const text = pickPostText(post, "text") ?? (sharedPost ? pickPostText(sharedPost, "text") : undefined);
  const sourcePost = sharedPost ?? post;
  const user = getPostRecord(sourcePost, "user");
  const ownerName = user ? pickPostText(user, "name") : undefined;
  const inputUrl = pickPostText(post, "url", "facebookUrl") ?? (sharedPost ? pickPostText(sharedPost, "url") : undefined);

  if (!text) {
    throw new Error("Apify returned a Facebook post without text");
  }

  return {
    finalUrl: inputUrl ?? url,
    canonicalUrl: inputUrl ?? url,
    title: postTitle(text, "Facebook", ownerName),
    description: text.slice(0, 12_000),
    imageUrl: pickPostImage(sourcePost) ?? pickPostImage(post),
    siteName: ownerName ? `Facebook - ${ownerName}` : "Facebook",
    recipes: []
  };
}

async function fetchPinterestMediaFromApify(url: string): Promise<PinterestMedia> {
  const items = await runApifyActor(PINTEREST_MEDIA_ACTOR, {
    startUrls: [{ url }]
  }, { maxTotalChargeUsd: "0.03" });
  if (!Array.isArray(items) || !isRecord(items[0])) {
    throw new Error("Apify returned no Pinterest pin data");
  }

  const pin = items[0];
  const description = pickPostText(pin, "description", "text", "caption");
  const user = getPostRecord(pin, "user");
  const ownerName = user ? pickPostText(user, "fullName", "username") : undefined;
  const video = findPinterestVideo(pin.videos);

  return {
    canonicalUrl: pickPostText(pin, "url") ?? url,
    linkedRecipeUrl: getSafeHttpUrl(pickPostText(pin, "trackedLink", "link", "sourceUrl")),
    title: pickPostText(pin, "title", "name") ?? (description ? postTitle(description, "Pinterest", ownerName) : undefined),
    description,
    imageUrl: video?.thumbnailUrl ?? pickLargestImageUrl(pin.images) ?? pickPostImage(pin),
    siteName: ownerName ? `Pinterest - ${ownerName}` : "Pinterest",
    videoUrl: video?.url,
    captionsUrl: video?.captionsUrl,
    durationSeconds: video?.durationSeconds
  };
}

async function fetchInstagramTranscriptFromApify(url: string) {
  const items = await runApifyActor("S9A11NvceWaGorwwh", { videoUrl: url });
  if (!Array.isArray(items) || !isRecord(items[0])) {
    throw new Error("Apify returned no Instagram transcript data");
  }

  const transcript = pickPostText(items[0], "text");
  if (!transcript || transcript.length < 40) {
    throw new Error("Apify returned an Instagram Reel without a usable transcript");
  }

  return transcript.slice(0, 12_000);
}

async function fetchUniversalSocialTranscriptFromApify(url: string, platform: "tiktok" | "facebook") {
  const items = await runApifyActor(
    UNIVERSAL_SOCIAL_TRANSCRIPT_ACTOR,
    { start_urls: url },
    { maxTotalChargeUsd: "0.75" }
  );
  if (!Array.isArray(items) || !isRecord(items[0])) {
    throw new Error(`Apify returned no ${platform} transcript data`);
  }

  const item = items[0];
  const duration = pickNumber(item, "duration", "durationSec", "durationSeconds", "videoDuration", "lengthSeconds");
  if (duration !== undefined && duration > MAX_SOCIAL_VIDEO_SECONDS) {
    throw new Error("Deze video is langer dan 5 minuten en kan niet worden getranscribeerd.");
  }

  const transcript = pickTranscriptText(item);
  if (!transcript || transcript.length < 40) {
    throw new Error(`Apify returned a ${platform} video without a usable transcript`);
  }

  return transcript.slice(0, 12_000);
}

async function fetchDirectMediaTranscriptFromApify(media: PinterestMedia) {
  if (!media.videoUrl) {
    throw new Error("Pinterest did not expose a usable video URL for transcription.");
  }
  if (media.durationSeconds === undefined) {
    throw new Error("Pinterest did not expose the video duration, so transcription was skipped to protect import costs.");
  }
  if (media.durationSeconds > MAX_SOCIAL_VIDEO_SECONDS) {
    throw new Error("Deze video is langer dan 5 minuten en kan niet worden getranscribeerd.");
  }

  const items = await runApifyActor(
    DIRECT_MEDIA_TRANSCRIPT_ACTOR,
    {
      mediaUrl: media.videoUrl,
      maxAudioMinutes: 5,
      diarize: false,
      smartFormat: true
    },
    { maxTotalChargeUsd: "0.25", timeoutSeconds: 180 }
  );
  if (!Array.isArray(items) || !isRecord(items[0])) {
    throw new Error("Apify returned no Pinterest transcript data");
  }

  const item = items[0];
  const duration = pickNumber(item, "durationSeconds", "duration", "durationSec", "videoDuration");
  if (duration !== undefined && duration > MAX_SOCIAL_VIDEO_SECONDS) {
    throw new Error("Deze video is langer dan 5 minuten en kan niet worden getranscribeerd.");
  }

  const transcript = pickTranscriptText(item);
  if (!transcript || transcript.length < 40) {
    throw new Error("Apify returned a Pinterest video without a usable transcript");
  }

  return transcript.slice(0, 12_000);
}

async function fetchPublicCaptionTranscript(url: string) {
  const captionUrl = getSafeHttpUrl(url);
  if (!captionUrl) throw new Error("Pinterest returned an invalid captions URL");

  const response = await fetch(captionUrl, { headers: { Accept: "text/vtt,text/plain,application/xml;q=0.9,*/*;q=0.8" } });
  if (!response.ok) throw new Error(`Pinterest captions failed with HTTP ${response.status}`);

  const transcript = subtitleText(await response.text());
  if (transcript.length < 40) throw new Error("Pinterest captions did not contain usable spoken text");
  return transcript.slice(0, 12_000);
}

async function runApifyActor(
  actorId: string,
  input: Record<string, unknown>,
  options: { maxTotalChargeUsd?: string; timeoutSeconds?: number } = {}
) {
  const token = Deno.env.get("APIFY_API_TOKEN");
  if (!token) {
    throw new Error("APIFY_API_TOKEN is not configured for social imports");
  }

  const endpoint = new URL(`https://api.apify.com/v2/actors/${actorId}/run-sync-get-dataset-items`);
  endpoint.searchParams.set("token", token);
  endpoint.searchParams.set("format", "json");
  endpoint.searchParams.set("clean", "true");
  endpoint.searchParams.set("timeout", String(options.timeoutSeconds ?? 120));
  endpoint.searchParams.set("maxPaidDatasetItems", "1");
  if (options.maxTotalChargeUsd) endpoint.searchParams.set("maxTotalChargeUsd", options.maxTotalChargeUsd);

  const response = await fetch(endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify(input)
  });

  if (!response.ok) {
    const detail = await readResponseError(response);
    throw new Error(`Apify actor import failed${detail ? `: ${detail}` : ""}`);
  }

  return await response.json();
}

async function readResponseError(response: Response) {
  try {
    const payload = await response.json();
    const message = payload?.error?.message ?? payload?.message;
    return typeof message === "string" ? message.slice(0, 240) : "";
  } catch {
    return "";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function pickPostText(post: ApifyInstagramPost, ...keys: string[]) {
  for (const key of keys) {
    const value = post[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return undefined;
}

function pickTranscriptText(item: ApifyInstagramPost) {
  const direct = pickPostText(item, "transcript", "transcriptText", "fullTranscript", "text", "captionText", "captions");
  if (direct) return direct;

  const segments = item.segments ?? item.transcriptSegments ?? item.captions;
  if (Array.isArray(segments)) {
    const text = segments
      .map((segment) => (isRecord(segment) ? pickPostText(segment, "text", "transcript", "caption") : ""))
      .filter(Boolean)
      .join(" ")
      .trim();
    if (text) return text;
  }

  return undefined;
}

function pickNumber(item: ApifyInstagramPost, ...keys: string[]) {
  for (const key of keys) {
    const value = item[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string") {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return undefined;
}

function findPinterestVideo(value: unknown, inheritedDuration?: number): PinterestVideo | undefined {
  const candidates: PinterestVideo[] = [];

  const visit = (node: unknown, durationFromParent?: number, depth = 0) => {
    if (depth > 5 || !node) return;
    if (Array.isArray(node)) {
      node.forEach((item) => visit(item, durationFromParent, depth + 1));
      return;
    }
    if (!isRecord(node)) return;

    const duration = pinterestDurationSeconds(node) ?? durationFromParent;
    const directUrl = pickPostText(node, "url", "videoUrl", "mediaUrl", "src");
    if (directUrl && isDirectMediaUrl(directUrl)) {
      candidates.push({
        url: directUrl,
        thumbnailUrl: pickImageUrl(node.thumbnail, node.image, node.poster),
        captionsUrl: findCaptionUrl(node.captionsUrls ?? node.captionUrls ?? node.subtitles),
        durationSeconds: duration
      });
    }

    Object.values(node).forEach((child) => visit(child, duration, depth + 1));
  };

  visit(value, inheritedDuration);
  return candidates
    .sort((left, right) => {
      const leftPriority = left.url.includes(".mp4") ? 2 : 1;
      const rightPriority = right.url.includes(".mp4") ? 2 : 1;
      return rightPriority - leftPriority;
    })
    .at(0);
}

function pinterestDurationSeconds(value: Record<string, unknown>) {
  const duration = pickNumber(value, "durationSeconds", "durationSec", "duration", "videoDuration", "lengthSeconds");
  if (duration === undefined || duration < 0) return undefined;
  // Pinterest video metadata uses milliseconds; other actors typically use seconds.
  return duration > 1_000 ? duration / 1_000 : duration;
}

function isDirectMediaUrl(value: string) {
  try {
    const path = new URL(value).pathname.toLowerCase();
    return /\.(mp4|webm|mov|mkv|m4a|mp3|wav|ogg)$/.test(path);
  } catch {
    return false;
  }
}

function findCaptionUrl(value: unknown, depth = 0): string | undefined {
  if (depth > 4 || !value) return undefined;
  if (typeof value === "string") return getSafeHttpUrl(value);
  if (Array.isArray(value)) {
    for (const item of value) {
      const url = findCaptionUrl(item, depth + 1);
      if (url) return url;
    }
  }
  if (isRecord(value)) {
    for (const item of Object.values(value)) {
      const url = findCaptionUrl(item, depth + 1);
      if (url) return url;
    }
  }
  return undefined;
}

function pickLargestImageUrl(value: unknown): string | undefined {
  if (!Array.isArray(value)) return findImageUrl(value);

  const images = value
    .filter(isRecord)
    .map((image) => ({
      url: pickImageUrl(image.url, image.src),
      width: pickNumber(image, "width") ?? 0,
      height: pickNumber(image, "height") ?? 0
    }))
    .filter((image): image is { url: string; width: number; height: number } => Boolean(image.url))
    .sort((left, right) => right.width * right.height - left.width * left.height);

  return images[0]?.url;
}

function subtitleText(raw: string) {
  const lines = raw.replace(/\r/g, "").split("\n");
  const uniqueLines: string[] = [];
  let previous = "";

  for (const line of lines) {
    const cleaned = decodeHtml(stripHtml(line.replace(/<[^>]+>/g, " "))).trim();
    if (!cleaned) continue;
    if (/^(WEBVTT|NOTE|STYLE|REGION)\b/i.test(cleaned)) continue;
    if (/^\d+$/.test(cleaned)) continue;
    if (/^\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}\s+-->/.test(cleaned)) continue;
    if (cleaned === previous) continue;
    uniqueLines.push(cleaned);
    previous = cleaned;
  }

  return uniqueLines.join(" ").replace(/\s+/g, " ").trim();
}

function pickPostImage(post: ApifyInstagramPost) {
  return pickImageUrl(
    post.displayUrl,
    post.imageUrl,
    post.thumbnailUrl,
    post.thumbnail,
    post.image,
    post.images,
    post.displayResources,
    post.media
  );
}

function pickImageUrl(...values: unknown[]) {
  for (const value of values) {
    const found = findImageUrl(value);
    if (found) return found;
  }
  return undefined;
}

function findImageUrl(value: unknown, depth = 0): string | undefined {
  if (depth > 3) return undefined;
  if (typeof value === "string" && isSupportedHttpUrl(value)) return value;
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findImageUrl(item, depth + 1);
      if (found) return found;
    }
  }
  if (isRecord(value)) {
    for (const key of ["url", "src", "imageUrl", "thumbnailUrl", "displayUrl"]) {
      const found = findImageUrl(value[key], depth + 1);
      if (found) return found;
    }
  }
  return undefined;
}

function getPostRecord(post: ApifyInstagramPost, key: string) {
  const value = post[key];
  return isRecord(value) ? value : undefined;
}

function postTitle(text: string, platform: string, ownerName?: string) {
  const firstLine = text.split(/\r?\n/).map((line) => line.trim()).find(Boolean);
  return firstLine?.slice(0, 180) || (ownerName ? `${platform} post by ${ownerName}` : `${platform} post`);
}

function isSupportedHttpUrl(url: string) {
  try {
    const parsedUrl = new URL(url);
    return parsedUrl.protocol === "http:" || parsedUrl.protocol === "https:";
  } catch {
    return false;
  }
}

function fallbackLinkContext(url: string, error: unknown): LinkContext {
  const platform = detectPlatform(url);
  const message = error instanceof Error ? error.message : String(error);

  return {
    url,
    platform,
    recipes: [],
    extractionWarnings: [`Metadata extraction failed: ${message}`]
  };
}

function hasUsableRecipeSignal(context: LinkContext) {
  if (isSocialLoginPage(context)) {
    return false;
  }

  if (
    context.recipes.some(
      (recipe) =>
        (recipe.recipeIngredient?.length ?? 0) > 0 ||
        (recipe.recipeInstructions?.length ?? 0) > 0 ||
        Boolean(recipe.name || recipe.description)
    )
  ) {
    return true;
  }

  if (context.transcript && context.transcript.trim().length >= 60) {
    return true;
  }

  const title = context.title?.trim() ?? "";
  const description = context.description?.trim() ?? "";
  const oembedText = context.oembed ? formatOembed(context.oembed) : "";
  const text = [title, description, oembedText]
    .filter(Boolean)
    .join(" ")
    .trim();

  if (text.length < 80) return false;

  return /\b(ingredients?|ingredienten|directions?|bereiding|instructions?|stappen|tbsp|tsp|cups?|gram|g\b|ml\b|eieren)\b/i.test(text);
}

function hasLikelyCompleteRecipeSignal(context: LinkContext) {
  if (isSocialLoginPage(context)) return false;

  if (
    context.recipes.some(
      (recipe) => (recipe.recipeIngredient?.length ?? 0) > 0 && (recipe.recipeInstructions?.length ?? 0) > 0
    )
  ) {
    return true;
  }

  const text = [context.title, context.description, context.oembed ? formatOembed(context.oembed) : ""]
    .filter(Boolean)
    .join(" ")
    .trim();
  if (text.length < 120) return false;

  const hasIngredientList = /\b(ingredients?|ingredienten|benodigdheden)\b/i.test(text);
  const hasInstruction = /\b(stap(?:pen)?|bereid(?:ing|en)?|instructions?|directions?|kook|bak|meng|voeg|verwarm|roer)\b/i.test(text);
  const hasQuantity = /\b\d+(?:[.,]\d+)?\s*(?:g|kg|ml|cl|dl|l|el|tl|tbsp|tsp|cups?|eieren?|stuks?)\b/i.test(text);
  return hasIngredientList && hasInstruction && hasQuantity;
}

function missingRecipeSignalDetails(context: LinkContext) {
  const platformLabel = {
    instagram: "Instagram",
    tiktok: "TikTok",
    youtube: "YouTube",
    facebook: "Facebook",
    pinterest: "Pinterest",
    blog: "deze website",
    manual: "deze bron"
  }[context.platform];

  const base =
    (context.platform === "instagram" || context.platform === "facebook") &&
    context.extractionWarnings.some((warning) => warning.includes("APIFY_API_TOKEN is not configured"))
      ? `${platformLabel} import is nog niet geconfigureerd. Voeg een geldige APIFY_API_TOKEN toe aan de Edge Function secrets.`
      : context.platform === "instagram"
        ? "Instagram gaf geen bruikbare caption terug voor deze post. Deze link kan daarom niet worden geimporteerd."
        : context.platform === "facebook"
          ? "Facebook gaf geen bruikbare posttekst terug. Deze link kan daarom niet worden geimporteerd."
      : `${platformLabel} gaf geen openbare recepttitel, caption of gestructureerde receptdata terug. Deze link kan daarom niet worden geimporteerd.`;

  return context.extractionWarnings.length
    ? `${base} Details: ${context.extractionWarnings.join(" ")}`
    : base;
}

function isSocialLoginPage(context: LinkContext) {
  const text = [context.title, context.description, context.siteName].filter(Boolean).join(" ").toLowerCase();

  if (context.platform === "instagram") {
    return (
      context.title?.trim().toLowerCase() === "instagram" ||
      text.includes("maak een account of meld je aan bij instagram") ||
      text.includes("log in to instagram")
    );
  }

  if (context.platform === "facebook") {
    return (
      text.includes("recipes shared from facebook") ||
      text.includes("log into facebook") ||
      text.includes("facebook helps you connect")
    );
  }

  return false;
}

function detectPlatform(url: string): SourcePlatform {
  const host = safeHost(url);
  if (host.includes("instagram.com")) return "instagram";
  if (host.includes("tiktok.com")) return "tiktok";
  if (host.includes("facebook.com") || host.includes("fb.watch")) return "facebook";
  if (host.includes("pinterest.") || host.includes("pin.it")) return "pinterest";
  if (host.includes("youtube.com") || host.includes("youtu.be")) return "youtube";
  return "blog";
}

function isInstagramReelUrl(url: string) {
  try {
    return /^\/(reel|reels)\//i.test(new URL(url).pathname);
  } catch {
    return false;
  }
}

function isFacebookVideoUrl(url: string) {
  try {
    const parsedUrl = new URL(url);
    return (
      parsedUrl.hostname.includes("fb.watch") ||
      /\/(reel|reels|watch|videos)\//i.test(parsedUrl.pathname) ||
      parsedUrl.searchParams.has("v")
    );
  } catch {
    return false;
  }
}

function safeHost(url: string) {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return "";
  }
}

function getSafeHttpUrl(value: string | undefined) {
  if (!value || !isSupportedHttpUrl(value)) return undefined;

  const host = safeHost(value);
  if (
    !host ||
    host === "localhost" ||
    host.endsWith(".local") ||
    /^127\./.test(host) ||
    /^10\./.test(host) ||
    /^192\.168\./.test(host) ||
    /^172\.(1[6-9]|2\d|3[0-1])\./.test(host) ||
    host === "::1"
  ) {
    return undefined;
  }

  return value;
}

async function fetchPageMetadata(url: string): Promise<PageMetadata> {
  const response = await fetch(url, {
    headers: {
      "User-Agent":
        "Mozilla/5.0 (compatible; RecipeNLBot/0.1; +https://example.invalid/recipenl)",
      Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.7"
    }
  });

  if (!response.ok) {
    throw new Error(`Page fetch failed with HTTP ${response.status}`);
  }

  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("text/html") && !contentType.includes("application/xhtml")) {
    throw new Error(`Unsupported content type: ${contentType || "unknown"}`);
  }

  const html = (await response.text()).slice(0, 600_000);
  return {
    finalUrl: response.url,
    canonicalUrl: pickString(extractMeta(html, "og:url"), extractMeta(html, "twitter:url")),
    sourceRecipeUrl: pickString(extractMeta(html, "pinterestapp:source"), extractMeta(html, "og:see_also")),
    title: extractTitle(html),
    imageUrl: pickString(extractMeta(html, "og:image"), extractMeta(html, "twitter:image")),
    description: pickString(
      extractMeta(html, "og:description"),
      extractMeta(html, "twitter:description"),
      extractMeta(html, "description")
    ),
    siteName: extractMeta(html, "og:site_name"),
    recipes: extractJsonLdRecipes(html)
  };
}

async function fetchPlatformOembed(url: string, platform: SourcePlatform): Promise<Record<string, unknown> | null> {
  let endpoint: string | null = null;

  if (platform === "tiktok") {
    endpoint = `https://www.tiktok.com/oembed?url=${encodeURIComponent(url)}`;
  }

  if (platform === "pinterest") {
    endpoint = `https://www.pinterest.com/oembed.json?url=${encodeURIComponent(url)}`;
  }

  if (!endpoint) return null;

  const response = await fetch(endpoint, {
    headers: {
      Accept: "application/json"
    }
  });

  if (!response.ok) {
    throw new Error(`${platform} oEmbed failed with HTTP ${response.status}`);
  }

  return await response.json();
}

function formatLinkContext(context: LinkContext): string {
  const parts = [
    `URL: ${context.url}`,
    context.canonicalUrl ? `Canonical URL: ${context.canonicalUrl}` : "",
    context.linkedRecipeUrl ? `Linked recipe URL: ${context.linkedRecipeUrl}` : "",
    `Platform: ${context.platform}`,
    context.siteName ? `Site/provider: ${context.siteName}` : "",
    context.title ? `Title: ${context.title}` : "",
    context.description ? `Description/caption: ${context.description}` : "",
    context.transcript ? `Transcript: ${context.transcript}` : "",
    context.oembed ? `oEmbed: ${formatOembed(context.oembed)}` : "",
    ...context.recipes.map((recipe, index) => formatExtractedRecipe(recipe, index + 1)),
    context.extractionWarnings.length > 0 ? `Extraction notes: ${context.extractionWarnings.join(" ")}` : ""
  ];

  return parts.filter(Boolean).join("\n");
}

function formatOembed(oembed: Record<string, unknown>) {
  return [
    pickString(oembed.title) ? `title=${pickString(oembed.title)}` : "",
    pickString(oembed.author_name) ? `author=${pickString(oembed.author_name)}` : "",
    pickString(oembed.provider_name) ? `provider=${pickString(oembed.provider_name)}` : "",
    pickString(oembed.html) ? `html_text=${stripHtml(String(oembed.html)).slice(0, 2500)}` : ""
  ]
    .filter(Boolean)
    .join("; ");
}

function formatExtractedRecipe(recipe: ExtractedRecipe, index: number) {
  return [
    `JSON-LD Recipe ${index}:`,
    recipe.name ? `Name: ${recipe.name}` : "",
    recipe.description ? `Description: ${recipe.description}` : "",
    recipe.recipeYield ? `Yield: ${recipe.recipeYield}` : "",
    recipe.prepTime ? `Prep time: ${recipe.prepTime}` : "",
    recipe.cookTime ? `Cook time: ${recipe.cookTime}` : "",
    recipe.totalTime ? `Total time: ${recipe.totalTime}` : "",
    recipe.recipeIngredient?.length ? `Ingredients:\n${recipe.recipeIngredient.map((item) => `- ${item}`).join("\n")}` : "",
    recipe.recipeInstructions?.length ? `Instructions:\n${recipe.recipeInstructions.map((item, i) => `${i + 1}. ${item}`).join("\n")}` : ""
  ]
    .filter(Boolean)
    .join("\n");
}

function extractTitle(html: string) {
  const ogTitle = extractMeta(html, "og:title") ?? extractMeta(html, "twitter:title");
  if (ogTitle) return ogTitle;
  const match = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  return match ? decodeHtml(stripHtml(match[1])).trim() : undefined;
}

function extractMeta(html: string, name: string) {
  const escaped = escapeRegExp(name);
  const patterns = [
    new RegExp(`<meta[^>]+(?:property|name)=["']${escaped}["'][^>]+content=["']([^"']*)["'][^>]*>`, "i"),
    new RegExp(`<meta[^>]+content=["']([^"']*)["'][^>]+(?:property|name)=["']${escaped}["'][^>]*>`, "i")
  ];

  for (const pattern of patterns) {
    const match = html.match(pattern);
    if (match?.[1]) return decodeHtml(match[1]).trim();
  }

  return undefined;
}

function extractJsonLdRecipes(html: string): ExtractedRecipe[] {
  const recipes: ExtractedRecipe[] = [];
  const scripts = html.matchAll(/<script[^>]+type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi);

  for (const match of scripts) {
    const rawJson = decodeHtml(match[1]).trim();
    try {
      const parsed = JSON.parse(rawJson);
      for (const node of flattenJsonLd(parsed)) {
        if (isRecipeNode(node)) {
          recipes.push(normalizeRecipeNode(node));
        }
      }
    } catch {
      // Some pages include malformed JSON-LD. Ignore it and keep other metadata.
    }
  }

  return recipes.slice(0, 3);
}

function flattenJsonLd(value: unknown): Record<string, any>[] {
  if (Array.isArray(value)) return value.flatMap(flattenJsonLd);
  if (!value || typeof value !== "object") return [];
  const object = value as Record<string, any>;
  const graph = Array.isArray(object["@graph"]) ? object["@graph"].flatMap(flattenJsonLd) : [];
  return [object, ...graph];
}

function isRecipeNode(node: Record<string, any>) {
  const type = node["@type"];
  if (Array.isArray(type)) return type.some((item) => String(item).toLowerCase() === "recipe");
  return String(type ?? "").toLowerCase() === "recipe";
}

function normalizeRecipeNode(node: Record<string, any>): ExtractedRecipe {
  return {
    name: pickString(node.name),
    description: pickString(node.description),
    recipeIngredient: asStringArray(node.recipeIngredient),
    recipeInstructions: normalizeInstructions(node.recipeInstructions),
    recipeYield: Array.isArray(node.recipeYield) ? node.recipeYield.join(", ") : pickString(node.recipeYield),
    prepTime: pickString(node.prepTime),
    cookTime: pickString(node.cookTime),
    totalTime: pickString(node.totalTime)
  };
}

function normalizeInstructions(value: unknown): string[] {
  if (!value) return [];
  if (typeof value === "string") return [decodeHtml(stripHtml(value))];
  if (Array.isArray(value)) {
    return value.flatMap((item) => {
      if (typeof item === "string") return [decodeHtml(stripHtml(item))];
      if (item && typeof item === "object") {
        const object = item as Record<string, any>;
        if (Array.isArray(object.itemListElement)) return normalizeInstructions(object.itemListElement);
        return asStringArray(object.text ?? object.name);
      }
      return [];
    });
  }
  if (typeof value === "object") {
    const object = value as Record<string, any>;
    return asStringArray(object.text ?? object.name);
  }
  return [];
}

function asStringArray(value: unknown): string[] {
  if (!value) return [];
  if (typeof value === "string") return [decodeHtml(stripHtml(value)).trim()].filter(Boolean);
  if (Array.isArray(value)) return value.flatMap(asStringArray);
  return [];
}

function stripHtml(value: string) {
  return value.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
}

function decodeHtml(value: string) {
  return value
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, " ")
    .replace(/&#x([0-9a-f]+);/gi, (_, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, decimal) => String.fromCodePoint(parseInt(decimal, 10)));
}

function pickString(...values: unknown[]): string | undefined {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return decodeHtml(stripHtml(value)).trim();
  }
  return undefined;
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function extractOutputText(response: any): string | null {
  if (typeof response.output_text === "string") {
    return response.output_text;
  }

  for (const output of response.output ?? []) {
    for (const content of output.content ?? []) {
      if (typeof content.text === "string") {
        return content.text;
      }
      if (content.type === "refusal" && typeof content.refusal === "string") {
        return null;
      }
    }
  }

  return null;
}

function json(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      ...corsHeaders,
      "Content-Type": "application/json"
    }
  });
}
