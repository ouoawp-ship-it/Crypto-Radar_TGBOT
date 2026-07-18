const ICON_ALIASES: Record<string, string> = {
  XBT: "btc",
  "1000PEPE": "pepe",
  "1000BONK": "bonk",
  "1000SHIB": "shib",
};

export function CoinIcon({ coin, size = 18 }: { coin?: string; size?: number }) {
  const raw = String(coin || "?").replace(/USDT$/i, "").toUpperCase();
  const label = raw.slice(0, 2);
  const slug = ICON_ALIASES[raw] || raw.toLowerCase();
  const hue = [...label].reduce((sum, char) => sum + char.charCodeAt(0), 0) % 360;
  return <span aria-label={`${raw} 图标`} className="relative grid shrink-0 place-items-center overflow-hidden rounded-full text-[7px] font-bold text-white" role="img" style={{ width: size, height: size, background: `linear-gradient(145deg,hsl(${hue} 72% 58%),hsl(${(hue + 32) % 360} 68% 43%))` }}>
    <span aria-hidden="true">{label}</span>
    <img alt="" aria-hidden="true" className="absolute inset-0 h-full w-full bg-white object-cover" loading="lazy" onError={(event) => { event.currentTarget.style.display = "none"; }} src={`https://cdn.jsdelivr.net/gh/atomiclabs/cryptocurrency-icons@master/128/color/${slug}.png`}/>
  </span>;
}
