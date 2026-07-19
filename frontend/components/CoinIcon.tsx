const ICON_ALIASES: Record<string, string> = {
  XBT: "btc",
  "1000PEPE": "pepe",
  "1000BONK": "bonk",
  "1000SHIB": "shib",
};

const LOCAL_ICONS: Record<string, { background: string; color?: string; glyph: string }> = {
  BTC: { background: "#f7931a", glyph: "₿" },
  ETH: { background: "#eef0f6", color: "#627eea", glyph: "◆" },
  SOL: { background: "#111318", color: "#63e6be", glyph: "≋" },
  XRP: { background: "#f5f6f8", color: "#23272d", glyph: "×" },
  ADA: { background: "#2a71d0", glyph: "✣" },
  DOGE: { background: "#c9a633", glyph: "Ð" },
  ZEC: { background: "#ecb244", color: "#24272c", glyph: "Z" },
  LINK: { background: "#2a5ada", glyph: "⬡" },
  AAVE: { background: "#7668d8", glyph: "A" },
  USDT: { background: "#26a17b", glyph: "₮" },
  BNB: { background: "#f3ba2f", color: "#24272c", glyph: "◆" },
  AVAX: { background: "#e84142", glyph: "A" },
  OP: { background: "#ff0420", glyph: "O" },
  SUI: { background: "#6fbcf0", glyph: "S" },
  ARB: { background: "#2d374b", glyph: "A" },
  UNI: { background: "#ff5db1", glyph: "U" },
};

export function CoinIcon({ coin, size = 18 }: { coin?: string; size?: number }) {
  const raw = String(coin || "?").replace(/USDT$/i, "").toUpperCase();
  const label = raw.slice(0, 2);
  const slug = ICON_ALIASES[raw] || raw.toLowerCase();
  const hue = [...label].reduce((sum, char) => sum + char.charCodeAt(0), 0) % 360;
  const local = LOCAL_ICONS[raw];
  return <span aria-label={`${raw} 图标`} className="relative grid shrink-0 place-items-center overflow-hidden rounded-full text-[7px] font-bold text-white" role="img" style={{ width: size, height: size, color: local?.color, background: local?.background || `linear-gradient(145deg,hsl(${hue} 72% 58%),hsl(${(hue + 32) % 360} 68% 43%))` }}>
    <span aria-hidden="true">{local?.glyph || label}</span>
    <img alt="" aria-hidden="true" className="absolute inset-0 h-full w-full bg-white object-cover" loading="lazy" onError={(event) => { event.currentTarget.style.display = "none"; }} src={`https://cdn.jsdelivr.net/gh/atomiclabs/cryptocurrency-icons@master/128/color/${slug}.png`}/>
  </span>;
}
