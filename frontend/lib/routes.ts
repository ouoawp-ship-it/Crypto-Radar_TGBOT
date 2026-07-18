import { cockpitV2Enabled } from "./features";

const coreItems = [
  { href: "/radar", label: "雷达", icon: "radar" as const },
];

const workstationItems = [
  { href: "/info", label: "信息", icon: "info" as const },
  { href: "/funds", label: "资金", icon: "funds" as const },
  { href: "/agents", label: "Paoxx AI", icon: "spark" as const, badge: "NEW" },
];

export const navItems = [
  ...coreItems,
  ...(cockpitV2Enabled ? workstationItems : []),
  { href: "/watchlist", label: "自选", icon: "watchlist" as const },
];
