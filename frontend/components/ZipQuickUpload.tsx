"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { FileArchive, Loader2, Upload, X } from "lucide-react";
import { uploadDeploymentSource } from "@/lib/api";
import { DEPLOY_REGIONS, MAX_UPLOAD_MB } from "@/lib/deployments";

function isZip(file: File): boolean {
  return file.name.toLowerCase().endsWith(".zip") || file.type === "application/zip";
}

/** Pick or drop a project .zip and start a deployment (backend/routers/deployments.py::upload_source). */
export default function ZipQuickUpload() {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [region, setRegion] = useState("us-east-1");
  const [dragging, setDragging] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const choose = (f: File | undefined) => {
    if (!f) return;
    if (!isZip(f)) {
      setError("Please choose a .zip archive.");
      return;
    }
    if (f.size > MAX_UPLOAD_MB * 1024 * 1024) {
      setError(`This file is larger than ${MAX_UPLOAD_MB} MB.`);
      return;
    }
    setFile(f);
    setError(null);
  };

  const clear = () => {
    setFile(null);
    if (inputRef.current) inputRef.current.value = "";
  };

  const submit = async () => {
    if (!file) return;
    setSubmitting(true);
    setError(null);
    try {
      const accepted = await uploadDeploymentSource(file, region, "production");
      router.push(`/deployments/${accepted.deployment_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed");
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-3">
      <div
        role="button"
        tabIndex={0}
        aria-label="Choose a .zip file to upload"
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          choose(e.dataTransfer.files?.[0]);
        }}
        className={`flex items-center gap-3 rounded-xl border-2 border-dashed px-4 py-4 cursor-pointer transition-colors ${
          dragging ? "border-brand-500 bg-brand-50" : "border-slate-300 bg-white hover:border-brand-400 hover:bg-slate-50"
        }`}
      >
        <div className="w-9 h-9 rounded-lg bg-brand-50 text-brand-600 flex items-center justify-center shrink-0">
          {file ? <FileArchive className="w-5 h-5" /> : <Upload className="w-5 h-5" />}
        </div>
        <div className="min-w-0 flex-1">
          {file ? (
            <>
              <div className="text-xs font-semibold text-slate-900 truncate">{file.name}</div>
              <div className="text-2xs text-slate-500">{(file.size / 1024 / 1024).toFixed(2)} MB · ready to analyze</div>
            </>
          ) : (
            <>
              <div className="text-xs font-semibold text-slate-900">Drop a project .zip here, or click to choose</div>
              <div className="text-2xs text-slate-500">Max {MAX_UPLOAD_MB} MB · .git, node_modules and .env files are never packaged</div>
            </>
          )}
        </div>
        {file && (
          <button
            type="button"
            aria-label="Remove file"
            onClick={(e) => {
              e.stopPropagation();
              clear();
            }}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100"
          >
            <X className="w-4 h-4" />
          </button>
        )}
        <input
          ref={inputRef}
          type="file"
          accept=".zip,application/zip"
          className="hidden"
          onChange={(e) => choose(e.target.files?.[0])}
        />
      </div>

      <div className="flex flex-col sm:flex-row sm:items-center gap-2">
        <label htmlFor="zip-region" className="text-2xs font-semibold text-slate-600 shrink-0">AWS region</label>
        <select
          id="zip-region"
          className="field-input sm:w-44 py-2"
          value={region}
          onChange={(e) => setRegion(e.target.value)}
        >
          {DEPLOY_REGIONS.map((r) => (
            <option key={r} value={r}>{r}</option>
          ))}
        </select>
        <button type="button" className="btn-primary sm:ml-auto py-2" onClick={submit} disabled={!file || submitting}>
          {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
          {submitting ? "Uploading…" : "Upload & analyze"}
        </button>
      </div>

      {error && <div className="p-2.5 rounded-lg bg-rose-50 border border-rose-200 text-xs text-rose-800">{error}</div>}
    </div>
  );
}
