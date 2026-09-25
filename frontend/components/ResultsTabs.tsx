"use client";

import { useState, useSyncExternalStore } from "react";

export interface ResultsTab {
  id: string;
  label: string;
  icon?: React.ReactNode;
  badge?: React.ReactNode;
  content: React.ReactNode;
}

// Client-side tab switcher for the (server-rendered) results page. Panels are
// rendered on the server and passed in as nodes; inactive ones stay mounted
// but hidden so client state inside them (e.g. the D3 simulation, a half-filled
// PR form) survives switching tabs. The active tab is mirrored to the URL hash
// so a specific view can be linked to.
function subscribeToHash(onChange: () => void) {
  window.addEventListener("hashchange", onChange);
  return () => window.removeEventListener("hashchange", onChange);
}

export default function ResultsTabs({ tabs }: { tabs: ResultsTab[] }) {
  const hash = useSyncExternalStore(
    subscribeToHash,
    () => window.location.hash.replace("#", ""),
    () => ""
  );
  const [picked, setPicked] = useState<string | null>(null);
  const active = picked ?? (tabs.some((t) => t.id === hash) ? hash : tabs[0]?.id);

  const select = (id: string) => {
    setPicked(id);
    // replaceState (not location.hash) so switching tabs doesn't pile up
    // history entries or scroll the page.
    window.history.replaceState(null, "", `#${id}`);
  };

  return (
    <div className="space-y-5">
      <div className="border-b border-slate-200 -mx-4 px-4 sm:mx-0 sm:px-0 overflow-x-auto">
        <div role="tablist" className="flex gap-1 min-w-max">
          {tabs.map((t) => {
            const isActive = t.id === active;
            return (
              <button
                key={t.id}
                role="tab"
                id={`tab-${t.id}`}
                aria-selected={isActive}
                aria-controls={`panel-${t.id}`}
                onClick={() => select(t.id)}
                className={`relative flex items-center gap-2 px-3.5 py-2.5 text-xs font-semibold transition-colors ${
                  isActive ? "text-brand-700" : "text-slate-500 hover:text-slate-900"
                }`}
              >
                {t.icon}
                {t.label}
                {t.badge !== undefined && t.badge !== null && (
                  <span
                    className={`px-1.5 py-px rounded-md text-3xs tabular-nums ${
                      isActive ? "bg-brand-100 text-brand-700" : "bg-slate-100 text-slate-500"
                    }`}
                  >
                    {t.badge}
                  </span>
                )}
                {isActive && <span className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-brand-600" />}
              </button>
            );
          })}
        </div>
      </div>

      {tabs.map((t) => (
        <div
          key={t.id}
          role="tabpanel"
          id={`panel-${t.id}`}
          aria-labelledby={`tab-${t.id}`}
          hidden={t.id !== active}
          className="space-y-5 animate-fade-in"
        >
          {t.content}
        </div>
      ))}
    </div>
  );
}
