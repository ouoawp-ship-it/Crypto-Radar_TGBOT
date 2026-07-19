const ICON_ALIASES: Record<string, string> = {
  XBT: "btc",
  "1000PEPE": "pepe",
  "1000BONK": "bonk",
  "1000SHIB": "shib",
};

const ICON_RELEASE = "1a63530be6e374711a8554f31b17e4cb92c25fa5";

// Mercu resolves exchange symbols to CoinGecko artwork. Keep the audited URLs
// explicit so the workstation does not silently swap an ambiguous ticker for a
// different project (BANK, HYPE, LIT and OPN are the common failure cases).
const COINGECKO_ICONS: Record<string, string> = {
  AAVE: "https://coin-images.coingecko.com/coins/images/12645/large/aave-token-round.png?1720472354",
  ADA: "https://coin-images.coingecko.com/coins/images/975/large/cardano.png?1696502090",
  AKE: "https://coin-images.coingecko.com/coins/images/68410/large/akedo.png?1755678461",
  ALLO: "https://coin-images.coingecko.com/coins/images/70609/large/allo-token.png?1763451165",
  AVAX: "https://coin-images.coingecko.com/coins/images/12559/large/Avalanche_Circle_RedWhite_Trans.png?1696512369",
  BANANAS31: "https://coin-images.coingecko.com/coins/images/52230/large/Banana_token_image.png?1732801941",
  BANK: "https://coin-images.coingecko.com/coins/images/55250/large/lorenzo.jpg?1744963693",
  BNB: "https://coin-images.coingecko.com/coins/images/825/large/bnb-icon2_2x.png?1696501970",
  BONK: "https://coin-images.coingecko.com/coins/images/28600/large/bonk.jpg?1696527587",
  BTC: "https://coin-images.coingecko.com/coins/images/1/large/bitcoin.png?1696501400",
  BTW: "https://coin-images.coingecko.com/coins/images/39533/large/btw.jpg?1722829990",
  CASHCAT: "https://coin-images.coingecko.com/coins/images/102174280/large/cashcat-logo.jpg?1782922765",
  CYS: "https://coin-images.coingecko.com/coins/images/71025/large/cysic.png?1765330348",
  DODO: "https://coin-images.coingecko.com/coins/images/12651/large/dodo_logo.png?1696512458",
  DOGE: "https://coin-images.coingecko.com/coins/images/5/large/dogecoin.png?1696501409",
  DYDX: "https://coin-images.coingecko.com/coins/images/32594/large/dydx.png?1698673495",
  ENS: "https://coin-images.coingecko.com/coins/images/19785/large/ENS.jpg?1727872989",
  ESPORTS: "https://coin-images.coingecko.com/coins/images/67430/large/symbol-esports.png?1770141653",
  ETH: "https://coin-images.coingecko.com/coins/images/279/large/ethereum.png?1696501628",
  HYPE: "https://coin-images.coingecko.com/coins/images/50882/large/hyperliquid.jpg?1729431300",
  JUP: "https://coin-images.coingecko.com/coins/images/34188/large/jup.png?1704266489",
  KAITO: "https://coin-images.coingecko.com/coins/images/54411/large/Qm4DW488_400x400.jpg?1739552780",
  LIT: "https://coin-images.coingecko.com/coins/images/71121/large/lighter.png?1765888098",
  LUMIA: "https://coin-images.coingecko.com/coins/images/50867/large/lumia.jpg?1729321993",
  MAGMA: "https://coin-images.coingecko.com/coins/images/71100/large/magma.png?1765796989",
  ONDO: "https://coin-images.coingecko.com/coins/images/26580/large/ONDO.png?1696525656",
  OPN: "https://coin-images.coingecko.com/coins/images/102171893/large/Opinon.jpg?1770269253",
  PEPE: "https://coin-images.coingecko.com/coins/images/29850/large/pepe-token.jpeg?1696528776",
  PHA: "https://coin-images.coingecko.com/coins/images/12451/large/phala.png?1696512270",
  POWR: "https://coin-images.coingecko.com/coins/images/1104/large/Powerledger_Token_logo_%281%29.png?1741750417",
  QTUM: "https://coin-images.coingecko.com/coins/images/684/large/Qtum_Logo_blue_CG.png?1696501874",
  SENT: "https://coin-images.coingecko.com/coins/images/70508/large/SENTIENT-Icon-BlushForce-L.png?1762267532",
  SLP: "https://coin-images.coingecko.com/coins/images/10366/large/SLP.png?1696510368",
  SOL: "https://coin-images.coingecko.com/coins/images/4128/large/solana.png?1718769756",
  STRK: "https://coin-images.coingecko.com/coins/images/26433/large/starknet.png?1696525507",
  TLM: "https://coin-images.coingecko.com/coins/images/14676/large/kY-C4o7RThfWrDQsLCAG4q4clZhBDDfJQVhWUEKxXAzyQYMj4Jmq1zmFwpRqxhAJFPOa0AsW_PTSshoPuMnXNwq3rU7Imp15QimXTjlXMx0nC088mt1rIwRs75GnLLugWjSllxgzvQ9YrP4tBgclK4_rb17hjnusGj_c0u2fx0AvVokjSNB-v2poTj0xT9BZRCbzRE3-lF1.jpg?1696514350",
  TRAC: "https://coin-images.coingecko.com/coins/images/1877/large/TRAC.jpg?1696502873",
  UNI: "https://coin-images.coingecko.com/coins/images/12504/large/uniswap-logo.png?1720676669",
  VANRY: "https://coin-images.coingecko.com/coins/images/33466/large/apple-touch-icon.png?1701942541",
  WLD: "https://coin-images.coingecko.com/coins/images/31069/large/worldcoin.jpeg?1696529903",
  XMR: "https://coin-images.coingecko.com/coins/images/69/large/monero_logo.png?1696501460",
  XRP: "https://coin-images.coingecko.com/coins/images/44/large/xrp-symbol-white-128.png?1696501442",
  ZBT: "https://coin-images.coingecko.com/coins/images/69446/large/zbt.png?1758621515",
  ZEC: "https://coin-images.coingecko.com/coins/images/486/large/circle-zcash-color.png?1696501740",
  ZEREBRO: "https://coin-images.coingecko.com/coins/images/51289/large/zerebro_2.png?1730588883",
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
  SPCX: { background: "#111318", glyph: "×" },
  SAKE: { background: "#5a173f", glyph: "△" },
  SNDK: { background: "#111318", glyph: "S" },
  CL: { background: "#2bbf9b", glyph: "CL" },
  XAU: { background: "#d5ac3c", color: "#fff", glyph: "◈" },
  OP: { background: "#ff0420", glyph: "O" },
  SUI: { background: "#6fbcf0", glyph: "S" },
  ARB: { background: "#2d374b", glyph: "A" },
  UNI: { background: "#ff5db1", glyph: "U" },
};

const LOCAL_ONLY = new Set(["SPCX", "SAKE", "SNDK", "CL", "XAU"]);

export function CoinIcon({ coin, iconUrl, size = 18 }: { coin?: string; iconUrl?: string; size?: number }) {
  const raw = String(coin || "?").replace(/USDT$/i, "").toUpperCase();
  const label = raw.slice(0, 2);
  const slug = ICON_ALIASES[raw] || raw.toLowerCase();
  const hue = [...label].reduce((sum, char) => sum + char.charCodeAt(0), 0) % 360;
  const local = LOCAL_ICONS[raw];
  const source = iconUrl || COINGECKO_ICONS[raw] || (LOCAL_ONLY.has(raw) ? "" : `https://cdn.jsdelivr.net/gh/atomiclabs/cryptocurrency-icons@${ICON_RELEASE}/128/color/${slug}.png`);
  return <span aria-label={`${raw} 图标`} className="relative grid shrink-0 place-items-center overflow-hidden rounded-full text-[7px] font-bold text-white" role="img" style={{ width: size, height: size, color: local?.color, background: local?.background || `linear-gradient(145deg,hsl(${hue} 72% 58%),hsl(${(hue + 32) % 360} 68% 43%))` }}>
    <span aria-hidden="true">{local?.glyph || label}</span>
    {source ? <img alt="" aria-hidden="true" className="absolute inset-0 h-full w-full object-cover" decoding="async" loading="eager" onError={(event) => { event.currentTarget.style.display = "none"; }} src={source}/> : null}
  </span>;
}
