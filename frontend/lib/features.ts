export type CockpitV2Mode = "enabled" | "preview" | "disabled";

const rawMode = String(process.env.NEXT_PUBLIC_PAOXX_COCKPIT_V2_MODE || "enabled").trim().toLowerCase();

export const cockpitV2Mode: CockpitV2Mode = rawMode === "preview" || rawMode === "disabled" ? rawMode : "enabled";
export const cockpitV2Enabled = cockpitV2Mode !== "disabled";
export const cockpitV2Preview = cockpitV2Mode === "preview";
