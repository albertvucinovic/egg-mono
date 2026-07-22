"use client";

import { useId, useLayoutEffect, useRef, type ReactNode } from "react";
import clsx from "clsx";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { IconButton } from "./primitives";

const FOCUSABLE = [
  "a[href]", "button:not([disabled])", "input:not([disabled])", "select:not([disabled])",
  "textarea:not([disabled])", "summary", '[tabindex]:not([tabindex="-1"])',
].join(",");

export function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((element) => {
    const style = window.getComputedStyle(element);
    return !element.hidden && style.display !== "none" && style.visibility !== "hidden";
  });
}

interface OverlayPanelProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  variant?: "dialog" | "drawer";
  drawerSide?: "left" | "right";
  description?: string;
  closeLabel?: string;
  testId?: string;
  footer?: ReactNode;
  returnFocusSelector?: string;
  portal?: boolean;
  panelClassName?: string;
  bodyClassName?: string;
  footerClassName?: string;
}

export function OverlayPanel({
  open,
  onClose,
  title,
  children,
  variant = "dialog",
  drawerSide = "right",
  description,
  closeLabel = `Close ${title}`,
  testId,
  footer,
  returnFocusSelector,
  portal = false,
  panelClassName,
  bodyClassName,
  footerClassName,
}: OverlayPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descriptionId = useId();

  useLayoutEffect(() => {
    if (!open) {
      if (returnFocusRef.current) {
        const target = returnFocusRef.current;
        returnFocusRef.current = null;
        window.requestAnimationFrame(() => target.focus());
      }
      return;
    }
    returnFocusRef.current = (returnFocusSelector ? document.querySelector<HTMLElement>(returnFocusSelector) : null)
      ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    const backgrounds = Array.from(document.querySelectorAll<HTMLElement>("[data-overlay-background]"));
    const previous = backgrounds.map((element) => ({ element, inert: element.inert }));
    backgrounds.forEach((element) => { element.inert = true; });
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const target = panelRef.current?.querySelector<HTMLElement>("[data-autofocus]")
      ?? (panelRef.current ? focusableElements(panelRef.current)[0] : null)
      ?? panelRef.current;
    target?.focus();
    return () => {
      previous.forEach(({ element, inert }) => { element.inert = inert; });
      document.body.style.overflow = previousOverflow;
      if (returnFocusRef.current) {
        const returnTarget = returnFocusRef.current;
        returnFocusRef.current = null;
        window.requestAnimationFrame(() => returnTarget.focus());
      }
    };
  }, [open, returnFocusSelector]);

  if (!open) return null;

  const overlay = (
    <div
      className={clsx(
        "ui-overlay",
        variant === "drawer" && "ui-overlay-drawer",
        variant === "drawer" && drawerSide === "left" && "ui-overlay-drawer-left",
      )}
      onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}
      data-testid={testId}
    >
      <div
        ref={panelRef}
        className={clsx("ui-overlay-panel", variant === "drawer" ? "ui-drawer-panel" : "ui-dialog-panel", panelClassName)}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            onClose();
            return;
          }
          if (event.key !== "Tab" || !panelRef.current) return;
          const focusable = focusableElements(panelRef.current);
          if (!focusable.length) {
            event.preventDefault();
            panelRef.current.focus();
            return;
          }
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        }}
      >
        <div className="ui-overlay-header">
          <div className="min-w-0">
            <h2 id={titleId} className="ui-overlay-title">{title}</h2>
            {description && <p id={descriptionId} className="ui-overlay-description">{description}</p>}
          </div>
          <IconButton aria-label={closeLabel} title={closeLabel} onClick={onClose} data-autofocus>
            <X className="h-5 w-5" aria-hidden="true" />
          </IconButton>
        </div>
        <div className={clsx("ui-overlay-body", bodyClassName)}>{children}</div>
        {footer && <div className={clsx("ui-overlay-footer", footerClassName)}>{footer}</div>}
      </div>
    </div>
  );
  if (portal && typeof document !== "undefined") {
    return createPortal(overlay, document.body);
  }
  return overlay;
}
