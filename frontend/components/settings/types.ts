// Settings 各 section 共用的小型別。

export type SetMsg = (m: { kind: "ok" | "err"; text: string } | null) => void;
