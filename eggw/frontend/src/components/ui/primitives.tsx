"use client";

import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";
import clsx from "clsx";

export type StatusTone = "neutral" | "info" | "success" | "warning" | "danger" | "special";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "secondary" | "primary" | "danger" | "warning" | "ghost";
}

export function Button({ variant = "secondary", className, type = "button", ...props }: ButtonProps) {
  return <button type={type} className={clsx("ui-button", `ui-button-${variant}`, className)} {...props} />;
}

export function IconButton({ className, ...props }: ButtonProps) {
  return <Button className={clsx("ui-icon-button", className)} {...props} />;
}

export function StatusChip({ tone = "neutral", className, ...props }: HTMLAttributes<HTMLSpanElement> & { tone?: StatusTone }) {
  return <span className={clsx("ui-status-chip", `ui-status-${tone}`, className)} {...props} />;
}

export function ControlGroup({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={clsx("ui-control-group", className)} {...props} />;
}

export function Select({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={clsx("ui-select", className)} {...props} />;
}

interface SwitchProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  checked: boolean;
  label: string;
}

export function Switch({ checked, label, className, ...props }: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={clsx("ui-switch", checked && "ui-switch-checked", className)}
      {...props}
    >
      <span className="ui-switch-thumb" aria-hidden="true" />
    </button>
  );
}

export function Surface({ as = "div", className, children, ...props }: HTMLAttributes<HTMLElement> & { as?: "div" | "section" | "aside"; children: ReactNode }) {
  const Component = as;
  return <Component className={clsx("ui-surface", className)} {...props}>{children}</Component>;
}
