import { cockpitV2Enabled } from "./features";

const stableItems = [
  { href: "/", label: "总览" },
  { href: "/radar", label: "信号雷达" },
];

const v2Items = [
  { href: "/funds", label: "资金中心" },
  { href: "/info", label: "信息中心" },
  { href: "/agents", label: "AI 决策" },
];

export const navItems = [...stableItems, ...(cockpitV2Enabled ? v2Items : []), { href: "/watchlist", label: "我的自选" }];
