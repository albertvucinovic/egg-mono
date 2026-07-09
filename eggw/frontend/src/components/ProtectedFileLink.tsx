"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { fetchProtectedBlob } from "@/lib/api";

interface ProtectedFileLinkProps {
  url: string;
  filename?: string;
  newWindow?: boolean;
  className?: string;
  style?: React.CSSProperties;
  "aria-label"?: string;
  children: ReactNode;
}

/** Open/download a protected API resource without putting its token in the URL. */
export function ProtectedFileLink({
  url,
  filename,
  newWindow = false,
  className,
  style,
  "aria-label": ariaLabel,
  children,
}: ProtectedFileLinkProps) {
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  const open = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const blob = await fetchProtectedBlob(url);
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      if (filename) link.download = filename;
      if (newWindow) {
        link.target = "_blank";
        link.rel = "noreferrer";
      }
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }, [filename, newWindow, url]);

  return (
    <button
      type="button"
      onClick={() => void open()}
      disabled={busy}
      className={className}
      style={style}
      aria-label={ariaLabel}
    >
      {children}
    </button>
  );
}

interface ProtectedImageProps extends Omit<React.ImgHTMLAttributes<HTMLImageElement>, "src"> {
  url: string;
}

/** Render an authenticated image via a short-lived same-page object URL. */
export function ProtectedImage({ url, onError, ...props }: ProtectedImageProps) {
  const [src, setSrc] = useState<string | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  useEffect(() => {
    let disposed = false;
    let objectUrl: string | null = null;
    setSrc(null);
    void fetchProtectedBlob(url)
      .then((blob) => {
        if (disposed) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      })
      .catch(() => {
        if (!disposed && imgRef.current) {
          onErrorRef.current?.({ currentTarget: imgRef.current, target: imgRef.current } as unknown as React.SyntheticEvent<HTMLImageElement>);
        }
      });
    return () => {
      disposed = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [url]);

  if (!src) return null;
  return <img ref={imgRef} src={src} onError={onError} {...props} />;
}
