"use client";

import { usePathname } from "next/navigation";
import { useEffect } from "react";
import { reportPublicTelemetry } from "@/lib/api";

export function FrontendTelemetry() {
  const pathname = usePathname();

  useEffect(() => {
    reportPublicTelemetry("frontend_route_loaded");
  }, [pathname]);

  useEffect(() => {
    const onError = () => reportPublicTelemetry("frontend_unhandled_error");
    const onRejection = () => reportPublicTelemetry("frontend_unhandled_error");
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
    };
  }, []);

  return null;
}
