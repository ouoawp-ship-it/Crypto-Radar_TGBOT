import { cockpitV2Enabled } from "./features";

const coreItems = [
  { href: "/radar", label: "雷达", icon: "radar" as const },
];

const cockpitItems = [
  { href: "/info", label: "信息", icon: "info" as const },
  { href: "/funds", label: "资金", icon: "funds" as const },
  { href: "/agents", label: "泡泡智选", icon: "spark" as const, badge: "预留" },
];

export const navItems = [
  ...coreItems,
  ...(cockpitV2Enabled ? cockpitItems : []),
  { href: "/watchlist", label: "自选", icon: "watchlist" as const },
];
