import type { ComponentProps } from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva("inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium whitespace-nowrap", {
  variants: {
    variant: {
      default: "bg-primary/10 text-primary",
      secondary: "bg-muted text-muted-foreground",
      success: "bg-success/10 text-success",
      warning: "bg-warning/10 text-warning",
    },
  },
  defaultVariants: { variant: "default" },
});

export function Badge({ className, variant, ...props }: ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span data-slot="badge" className={cn(badgeVariants({ variant }), className)} {...props} />;
}
