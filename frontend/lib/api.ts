export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

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

export type MovieDetail = {
  code: string;
  title: string;
  cover: string;
  release_date: string;
  duration: string;
  studio: string;
  label: string;
  director: string;
  series: string;
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
};
