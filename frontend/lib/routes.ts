import { cockpitV2Enabled } from "./features";

const coreItems = [
  { href: "/radar", label: "雷达", icon: "radar" as const },
];

const workstationItems = [
  { href: "/info", label: "信息", icon: "info" as const },
  { href: "/funds", label: "资金", icon: "funds" as const },
];

export const navItems = [
  ...coreItems,
  ...(cockpitV2Enabled ? workstationItems : []),
];
