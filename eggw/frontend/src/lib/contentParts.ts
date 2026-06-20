export interface TextContentPart {
  type: "text";
  text: string;
}

export interface AttachmentContentPart {
  type: "attachment";
  input_id: string;
  owner_thread_id: string;
  presentation: string;
  mime_type: string;
  filename?: string | null;
  size_bytes?: number;
  sha256?: string;
  options?: Record<string, unknown>;
}

export interface ArtifactContentPart {
  type: "artifact";
  artifact_id: string;
  owner_thread_id: string;
  presentation: string;
  mime_type: string;
  filename?: string | null;
  size_bytes?: number;
  sha256?: string;
  provenance?: Record<string, unknown>;
  options?: Record<string, unknown>;
}

export type UnknownContentPart = {
  type: string;
  [key: string]: unknown;
};

export type ContentPart = TextContentPart | AttachmentContentPart | ArtifactContentPart | UnknownContentPart;
export type EggMessageContent = string | ContentPart[];

export interface AttachmentUploadResponse {
  input_id: string;
  metadata: Record<string, unknown>;
  content_part: AttachmentContentPart;
  content_text: string;
}

export function isContentPartArray(content: unknown): content is ContentPart[] {
  return Array.isArray(content);
}

export function isTextPart(part: ContentPart): part is TextContentPart {
  return part?.type === "text" && typeof (part as TextContentPart).text === "string";
}

export function isAttachmentPart(part: ContentPart): part is AttachmentContentPart {
  return part?.type === "attachment";
}

export function isArtifactPart(part: ContentPart): part is ArtifactContentPart {
  return part?.type === "artifact";
}

export function formatBytes(sizeBytes: unknown): string {
  const size = typeof sizeBytes === "number" ? sizeBytes : Number(sizeBytes);
  if (!Number.isFinite(size) || size < 0) return "unknown size";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = size;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  if (unitIndex === 0) return `${Math.trunc(value)} B`;
  const rendered = value >= 100 ? value.toFixed(0) : value >= 10 ? value.toFixed(1) : value.toFixed(2);
  return `${Number.parseFloat(rendered).toString()} ${units[unitIndex]}`;
}

export function attachmentFilename(part: AttachmentContentPart): string {
  return part.filename || "(unnamed)";
}

export function artifactFilename(part: ArtifactContentPart): string {
  return part.filename || "(unnamed)";
}

export function attachmentPlaceholder(part: AttachmentContentPart): string {
  const filename = attachmentFilename(part);
  const presentation = part.presentation || "file";
  const mimeType = part.mime_type || "application/octet-stream";
  const size = formatBytes(part.size_bytes);
  const sha = typeof part.sha256 === "string" && part.sha256 ? part.sha256.slice(0, 8) : "unknown";
  return `[Attachment: ${presentation} ${filename} ${mimeType} ${size} sha256:${sha}]`;
}

export function artifactPlaceholder(part: ArtifactContentPart): string {
  const filename = artifactFilename(part);
  const presentation = part.presentation || "file";
  const mimeType = part.mime_type || "application/octet-stream";
  const size = formatBytes(part.size_bytes);
  const sha = typeof part.sha256 === "string" && part.sha256 ? part.sha256.slice(0, 8) : "unknown";
  const artifactId = part.artifact_id || "unknown";
  return `[Provider artifact: ${presentation} ${filename} ${mimeType} ${size} sha256:${sha} artifact_id:${artifactId}]`;
}

export function contentToPlainText(content: unknown, fallback = ""): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return fallback;
  return content
    .map((part) => {
      if (!part || typeof part !== "object") return "";
      const typedPart = part as ContentPart;
      if (isTextPart(typedPart)) return typedPart.text;
      if (isAttachmentPart(typedPart)) return attachmentPlaceholder(typedPart);
      if (isArtifactPart(typedPart)) return artifactPlaceholder(typedPart);
      try {
        return JSON.stringify(typedPart);
      } catch {
        return String(typedPart);
      }
    })
    .filter(Boolean)
    .join("\n");
}

export function buildMessageContentWithAttachments(
  text: string,
  attachments: AttachmentContentPart[],
): EggMessageContent {
  const trimmed = text.trim();
  if (!attachments.length) return trimmed;
  const parts: ContentPart[] = [];
  if (trimmed) {
    parts.push({ type: "text", text: trimmed });
  }
  parts.push(...attachments);
  return parts;
}
