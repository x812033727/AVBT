"use client";

import { useEffect, useState } from "react";
import { User } from "lucide-react";
import ListingPage from "@/components/ListingPage";
import { api, imgProxy, type StarProfile } from "@/lib/api";

// 女優 profile 卡:取代 ListingPage 預設的 label/slug 標題區塊,透過
// headerSlot 掛上頭像/生日/身高/三圍/出生地/愛好。presence overlay、
// pagination、追蹤、bulk-send 都直接沿用 ListingPage,不再另外複製一份。
function ProfileCard({
  id,
  uncensored,
  onProfile,
}: {
  id: string;
  uncensored: boolean;
  onProfile: (p: StarProfile | null) => void;
}) {
  const [profile, setProfile] = useState<StarProfile | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .get<StarProfile | null>(
        `/api/javbus/star/${encodeURIComponent(id)}/profile?uncensored=${uncensored}`
      )
      .then((p) => {
        if (!alive) return;
        setProfile(p);
        onProfile(p);
      })
      .catch(() => {
        if (!alive) return;
        setProfile(null);
        onProfile(null);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, uncensored]);

  return (
    <div className="flex flex-wrap items-start gap-4 rounded-lg border border-border bg-card p-4">
      {profile?.avatar ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={imgProxy(profile.avatar)}
          alt={profile.name || id}
          referrerPolicy="no-referrer"
          className="h-32 w-24 flex-none rounded-md object-cover"
        />
      ) : (
        <div className="grid h-32 w-24 flex-none place-items-center rounded-md bg-muted">
          <User className="h-8 w-8 text-muted-foreground/50" aria-hidden />
        </div>
      )}
      <div className="min-w-0 flex-1 space-y-1">
        <div className="text-xs text-muted-foreground">女優</div>
        <h1 className="text-xl font-semibold text-primary">
          {profile?.name || id}
        </h1>
        {profile && (
          <dl className="grid grid-cols-[64px_1fr] gap-x-2 gap-y-0.5 text-xs">
            {profile.birthday && (
              <>
                <dt className="text-muted-foreground">生日</dt>
                <dd>
                  {profile.birthday}
                  {profile.age ? ` (${profile.age})` : ""}
                </dd>
              </>
            )}
            {profile.height && (
              <>
                <dt className="text-muted-foreground">身高</dt>
                <dd>{profile.height}</dd>
              </>
            )}
            {(profile.bust || profile.cup) && (
              <>
                <dt className="text-muted-foreground">三圍</dt>
                <dd>
                  {[
                    profile.bust && `${profile.bust}${profile.cup ? ` (${profile.cup})` : ""}`,
                    profile.waist,
                    profile.hip,
                  ]
                    .filter(Boolean)
                    .join(" / ")}
                </dd>
              </>
            )}
            {profile.birthplace && (
              <>
                <dt className="text-muted-foreground">出生地</dt>
                <dd>{profile.birthplace}</dd>
              </>
            )}
            {profile.hobby && (
              <>
                <dt className="text-muted-foreground">愛好</dt>
                <dd className="line-clamp-2">{profile.hobby}</dd>
              </>
            )}
          </dl>
        )}
      </div>
    </div>
  );
}

export default function StarPage({ params }: { params: { id: string } }) {
  const id = decodeURIComponent(params.id);
  const [profile, setProfile] = useState<StarProfile | null>(null);
  // Tracking before the profile settles would store avatar="" forever
  // (the backend never backfills avatars) — gate the button until then.
  const [profileSettled, setProfileSettled] = useState(false);

  return (
    <ListingPage
      kind="star"
      id={id}
      label="女優"
      headerSlot={(ctx) => (
        <ProfileCard
          id={id}
          uncensored={ctx.uncensored}
          onProfile={(p) => {
            setProfile(p);
            setProfileSettled(true);
          }}
        />
      )}
      trackName={profile?.name}
      trackAvatar={profile?.avatar}
      trackDisabled={!profileSettled}
    />
  );
}
