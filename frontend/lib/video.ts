const VIDEO_EXTS = new Set([
  ".mp4",
  ".mkv",
  ".avi",
  ".wmv",
  ".mov",
  ".flv",
  ".ts",
  ".m4v",
  ".webm",
]);

export function isVideo(name: string | null | undefined): boolean {
  if (!name) return false;
  const i = name.lastIndexOf(".");
  if (i < 0) return false;
  return VIDEO_EXTS.has(name.slice(i).toLowerCase());
}
