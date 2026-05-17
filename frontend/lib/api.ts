export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

/**
 * Route a remote image through our backend proxy. JavBus' image CDN has
 * hot-link protection that blocks browser-direct requests, so the
 * backend re-fetches with the right Referer and serves the bytes back.
 *
 * - Empty / data URIs are returned untouched
 * - Already-relative URLs (start with /) are returned untouched
 */
export function imgProxy(url: string | null | undefined): string {
  if (!url) return "";
  if (url.startsWith("data:") || url.startsWith("blob:")) return url;
  if (url.startsWith("/") && !url.startsWith("//")) return url;
  return `${API_BASE}/api/img/proxy?url=${encodeURIComponent(url)}`;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init.headers || {}),
    },
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    let msg = text || `${res.status} ${res.statusText}`;
    try {
      const j = JSON.parse(text);
      if (j && typeof j === "object" && j.detail) msg = String(j.detail);
    } catch {
      /* not JSON */
    }
    throw new Error(msg);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(p: string) => request<T>(p),
  post: <T>(p: string, body?: unknown) =>
    request<T>(p, { method: "POST", body: body ? JSON.stringify(body) : undefined }),
  del: <T>(p: string) => request<T>(p, { method: "DELETE" }),
};

/** POST + read newline-delimited JSON events. Invokes onEvent for each. */
export async function streamNdjson(
  path: string,
  body: unknown,
  onEvent: (event: any) => void,
  signal?: AbortSignal
): Promise<void> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `${res.status} ${res.statusText}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        onEvent(JSON.parse(line));
      } catch {
        /* ignore malformed line */
      }
    }
  }
  if (buf.trim()) {
    try {
      onEvent(JSON.parse(buf));
    } catch {
      /* ignore */
    }
  }
}

// ---------- types ----------

export type MovieListItem = {
  code: string;
  title: string;
  cover: string;
  detail_url: string;
  date: string;
};

export type SearchResult = {
  items: MovieListItem[];
  page: number;
  has_next: boolean;
  total_pages: number | null;
};

export type Magnet = {
  name: string;
  link: string;
  size: string;
  date: string;
  is_hd: boolean;
  has_subtitle: boolean;
};

export type ActressRef = { name: string; id: string };
export type GenreRef = { name: string; id: string };
export type LinkRef = { name: string; id: string };

export type StarProfile = {
  id: string;
  name: string;
  avatar: string;
  birthday: string;
  age: string;
  height: string;
  cup: string;
  bust: string;
  waist: string;
  hip: string;
  birthplace: string;
  hobby: string;
};

export type MovieDetail = {
  code: string;
  title: string;
  cover: string;
  release_date: string;
  duration: string;
  studio: LinkRef | null;
  label: LinkRef | null;
  director: LinkRef | null;
  series: LinkRef | null;
  actresses: ActressRef[];
  genres: GenreRef[];
  samples: string[];
  magnets: Magnet[];
};

export function btih(magnet: string): string {
  const m = magnet.match(/xt=urn:btih:([A-Za-z0-9]+)/);
  return m ? m[1].toUpperCase() : "";
}

export type CollectionItem = {
  code: string;
  title: string;
  cover: string;
  release_date: string;
  duration: string;
  actresses: string[];
  genres: string[];
  note: string;
  status: string;
  created_at: string;
  updated_at: string;
};

export type PikPakTask = {
  id: string;
  name: string;
  phase: string;
  progress: number | null;
  file_id: string | null;
  file_size: number | null;
  message: string | null;
  created_time: string | null;
};

export type PikPakFile = {
  id: string;
  name: string;
  kind: string;
  size: number | null;
  parent_id: string | null;
  created_time: string | null;
  thumbnail_link: string | null;
};

export type PikPakQuota = {
  used: number;
  limit: number;
  expire: string | null;
};

export type ArchiverStatus = {
  enabled: boolean;
  interval_seconds: number;
  archive_folder: string;
  last_run: string | null;
  archived_total: number;
  last_error: string;
  // Root-sweep tidy-up (orphans not in OfflineTaskLog).
  sweep_enabled: boolean;
  sweep_interval_seconds: number;
  last_sweep_at: string | null;
  last_sweep_moved: number;
  last_sweep_error: string;
  sweep_swept_total: number;
  task_folder: string;
  sweep_fallback_root: boolean;
};

export type HistoryItem = {
  id: number;
  code: string;
  magnet: string;
  task_id: string;
  file_id: string;
  name: string;
  phase: string;
  message: string;
  archived: boolean;
  archived_at: string | null;
  created_at: string;
};

export type HistoryPage = {
  items: HistoryItem[];
  total: number;
  offset: number;
  limit: number;
};

export type TrackedKind = "star" | "studio" | "label" | "series" | "director";

export type TrackedListing = {
  kind: TrackedKind;
  id: string;
  name: string;
  avatar: string;
  uncensored: boolean;
  auto_send: boolean;
  last_seen_code: string;
  last_checked_at: string | null;
  last_error: string;
  new_count: number;
  created_at: string;
};

export type CheckListingResult = {
  kind: TrackedKind;
  id: string;
  name: string;
  new_codes: string[];
  error: string;
};

// Legacy aliases.
export type TrackedActress = TrackedListing;
export type CheckActressResult = CheckListingResult;

export const TRACKED_LABELS: Record<TrackedKind, string> = {
  star: "女優",
  studio: "製作商",
  label: "發行商",
  series: "系列",
  director: "導演",
};

export type PikPakStatus = {
  logged_in: boolean;
  username: string;
  has_stored_token: boolean;
  has_env_credentials: boolean;
  quota?: PikPakQuota;
  quota_error?: string;
};

export type TrackerStatus = {
  enabled: boolean;
  interval_seconds: number;
  last_run: string | null;
  last_error: string;
  last_new_total: number;
};

export type ExtraCode = {
  code: string;
  paths: string[];
};

export type MissingCodesResult = {
  kind: TrackedKind;
  id: string;
  name: string;
  total: number;
  present_codes: string[];
  missing: MovieListItem[];
  extras: ExtraCode[];
  pages_scanned: number;
  expected_root: string;
  built_at: string;
};

export type MissingSummaryItem = {
  kind: TrackedKind;
  id: string;
  name: string;
  total: number;
  missing_count: number;
  extras_count: number;
  pages_scanned: number;
  expected_root: string;
  error: string;
};

export type MissingSummary = {
  built_at: string;
  presence_built_at: string | null;
  items: MissingSummaryItem[];
};

export type AggregatedMissingItem = {
  kind: TrackedKind;
  id: string;
  name: string;
  missing: MovieListItem[];
};

export type AggregatedMissing = {
  built_at: string;
  presence_built_at: string | null;
  items: AggregatedMissingItem[];
};

export type PresenceStatus = {
  built_at: string | null;
  size: number;
  last_error: string;
  ttl_seconds: number;
  ready: boolean;
};

export type PresenceRoot = {
  path: string;
  leaves: number;
  codes: number;
  unrecognized: number;
};

export type PresenceUnrecognized = {
  parent: string;
  name: string;
};

export type PresenceDetail = PresenceStatus & {
  roots: PresenceRoot[];
  unrecognized: PresenceUnrecognized[];
  unrecognized_total: number;
};

export type PresenceCodeLookup = {
  code: string;
  paths: string[];
};
