import clsx from "clsx";
import { motion, type HTMLMotionProps } from "framer-motion";
import { Loader2 } from "lucide-react";
import type { ReactNode } from "react";
import { Link, type LinkProps } from "react-router-dom";

type Variant = "primary" | "ghost" | "outline" | "danger";

const styles: Record<Variant, string> = {
  primary:
    "text-ink-950 bg-[linear-gradient(135deg,#9bffe2,#7cf5d2_40%,#5ad8ff)] shadow-[0_0_0_1px_rgba(255,255,255,0.25)_inset,0_10px_30px_-10px_rgba(124,245,210,0.7)] hover:shadow-[0_0_0_1px_rgba(255,255,255,0.35)_inset,0_14px_40px_-10px_rgba(124,245,210,0.9)]",
  ghost: "text-white/75 hover:text-white hover:bg-white/[0.06]",
  outline: "text-white/85 border border-[var(--control-border)] bg-white/[0.03] hover:bg-white/[0.07] hover:border-[var(--control-border-hover)]",
  danger: "text-rose border border-rose/80 bg-rose/[0.06] hover:bg-rose/[0.12] hover:border-rose",
};

/** Button styles, shared with ButtonLink (a link that looks like a button: one element, one tab stop). */
export function buttonClass(variant: Variant = "outline", size: "sm" | "md" = "md", className?: string) {
  return clsx(
    "focus-ring relative inline-flex items-center justify-center gap-2 overflow-hidden rounded-xl font-medium transition-[background,box-shadow,border-color,color] disabled:pointer-events-none disabled:opacity-45",
    size === "sm" ? "h-8 px-3 text-[12.5px]" : "h-10 px-4 text-[13.5px]",
    styles[variant],
    className,
  );
}

/** Navigation styled as a button. Never nest a <Button> inside a <Link>: that is two tab stops for one action. */
export function ButtonLink({ variant = "outline", size = "md", className, icon, children, ...rest }: LinkProps & { variant?: Variant; size?: "sm" | "md"; icon?: ReactNode }) {
  return (
    <Link data-variant={variant} className={buttonClass(variant, size, className)} {...rest}>
      {icon}
      {children}
    </Link>
  );
}

export function Button({
  variant = "outline",
  size = "md",
  loading,
  icon,
  className,
  children,
  disabled,
  ...rest
}: HTMLMotionProps<"button"> & { variant?: Variant; size?: "sm" | "md"; loading?: boolean; icon?: ReactNode; children?: ReactNode }) {
  return (
    <motion.button
      data-variant={variant}
      whileHover={{ y: -1 }}
      whileTap={{ scale: 0.97 }}
      transition={{ type: "spring", stiffness: 500, damping: 30 }}
      disabled={disabled || loading}
      className={buttonClass(variant, size, className)}
      {...rest}
    >
      {loading ? <Loader2 className="size-4 animate-spin" /> : icon}
      {children}
    </motion.button>
  );
}
