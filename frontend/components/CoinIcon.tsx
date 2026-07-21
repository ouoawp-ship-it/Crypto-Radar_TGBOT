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
  ACE: "https://coin-images.coingecko.com/coins/images/33528/large/ACE.png?1702254943",
  ADA: "https://coin-images.coingecko.com/coins/images/975/large/cardano.png?1696502090",
  AKE: "https://coin-images.coingecko.com/coins/images/68410/large/akedo.png?1755678461",
  ALLO: "https://coin-images.coingecko.com/coins/images/70609/large/allo-token.png?1763451165",
  ALICE: "https://coin-images.coingecko.com/coins/images/14375/large/alice_logo.jpg?1696514067",
  AVAX: "https://coin-images.coingecko.com/coins/images/12559/large/Avalanche_Circle_RedWhite_Trans.png?1696512369",
  BANANAS31: "https://coin-images.coingecko.com/coins/images/52230/large/Banana_token_image.png?1732801941",
  BANK: "https://coin-images.coingecko.com/coins/images/55250/large/lorenzo.jpg?1744963693",
  BLESS: "https://coin-images.coingecko.com/coins/images/69339/large/Layer_1_%284%29.png",
  BNB: "https://coin-images.coingecko.com/coins/images/825/large/bnb-icon2_2x.png?1696501970",
  BONK: "https://coin-images.coingecko.com/coins/images/28600/large/bonk.jpg?1696527587",
  BR: "https://coin-images.coingecko.com/coins/images/54908/large/BR_200px.png",
  BTC: "https://coin-images.coingecko.com/coins/images/1/large/bitcoin.png?1696501400",
  BTW: "https://coin-images.coingecko.com/coins/images/39533/large/btw.jpg?1722829990",
  CASHCAT: "https://coin-images.coingecko.com/coins/images/102174280/large/cashcat-logo.jpg?1782922765",
  CKB: "https://coin-images.coingecko.com/coins/images/9566/large/Nervos_White.png",
  CYS: "https://coin-images.coingecko.com/coins/images/71025/large/cysic.png?1765330348",
  DATA: "https://coin-images.coingecko.com/coins/images/54035/large/CMC_%281%29.png",
  DEXE: "https://coin-images.coingecko.com/coins/images/12713/large/DEXE_token_logo.png",
  DODO: "https://coin-images.coingecko.com/coins/images/12651/large/dodo_logo.png?1696512458",
  DOGE: "https://coin-images.coingecko.com/coins/images/5/large/dogecoin.png?1696501409",
  DYDX: "https://coin-images.coingecko.com/coins/images/32594/large/dydx.png?1698673495",
  ENS: "https://coin-images.coingecko.com/coins/images/19785/large/ENS.jpg?1727872989",
  ESPORTS: "https://coin-images.coingecko.com/coins/images/67430/large/symbol-esports.png?1770141653",
  ETH: "https://coin-images.coingecko.com/coins/images/279/large/ethereum.png?1696501628",
  ETHFI: "https://coin-images.coingecko.com/coins/images/35958/large/etherfi.jpeg",
  FARTCOIN: "https://coin-images.coingecko.com/coins/images/50891/large/fart.jpg",
  HYPE: "https://coin-images.coingecko.com/coins/images/50882/large/hyperliquid.jpg?1729431300",
  IDOL: "https://coin-images.coingecko.com/coins/images/66490/large/meet48.jpg",
  INJ: "https://coin-images.coingecko.com/coins/images/12882/large/Other_200x200.png",
  JUP: "https://coin-images.coingecko.com/coins/images/34188/large/jup.png?1704266489",
  JELLYJELLY: "https://coin-images.coingecko.com/coins/images/54087/large/jellyjelly-logo.webp",
  KAITO: "https://coin-images.coingecko.com/coins/images/54411/large/Qm4DW488_400x400.jpg?1739552780",
  LIT: "https://coin-images.coingecko.com/coins/images/71121/large/lighter.png?1765888098",
  LUNC: "https://coin-images.coingecko.com/coins/images/8284/large/01_LunaClassic_color.png",
  LUMIA: "https://coin-images.coingecko.com/coins/images/50867/large/lumia.jpg?1729321993",
  MAGMA: "https://coin-images.coingecko.com/coins/images/71100/large/magma.png?1765796989",
  MANTRA: "https://coin-images.coingecko.com/coins/images/102172151/large/OM_Token_Primary-1K.png",
  NEAR: "https://coin-images.coingecko.com/coins/images/10365/large/near.jpg",
  ONDO: "https://coin-images.coingecko.com/coins/images/26580/large/ONDO.png?1696525656",
  OPN: "https://coin-images.coingecko.com/coins/images/102171893/large/Opinon.jpg?1770269253",
  PEPE: "https://coin-images.coingecko.com/coins/images/29850/large/pepe-token.jpeg?1696528776",
  PHA: "https://coin-images.coingecko.com/coins/images/12451/large/phala.png?1696512270",
  POWR: "https://coin-images.coingecko.com/coins/images/1104/large/Powerledger_Token_logo_%281%29.png?1741750417",
  PROM: "https://coin-images.coingecko.com/coins/images/8825/large/Ticker.png?1696508978",
  PUMP: "https://coin-images.coingecko.com/coins/images/67164/large/pump.jpg",
  Q: "https://coin-images.coingecko.com/coins/images/68793/large/quack_ai.png",
  QTUM: "https://coin-images.coingecko.com/coins/images/684/large/Qtum_Logo_blue_CG.png?1696501874",
  SENT: "https://coin-images.coingecko.com/coins/images/70508/large/SENTIENT-Icon-BlushForce-L.png?1762267532",
  SLP: "https://coin-images.coingecko.com/coins/images/10366/large/SLP.png?1696510368",
  SLX: "https://coin-images.coingecko.com/coins/images/71128/large/slx.png",
  SOL: "https://coin-images.coingecko.com/coins/images/4128/large/solana.png?1718769756",
  STRK: "https://coin-images.coingecko.com/coins/images/26433/large/starknet.png?1696525507",
  SUI: "https://coin-images.coingecko.com/coins/images/26375/large/sui-ocean-square.png?1727791290",
  TAO: "https://coin-images.coingecko.com/coins/images/28452/large/ARUsPeNQ_400x400.jpeg",
  TLM: "https://coin-images.coingecko.com/coins/images/14676/large/kY-C4o7RThfWrDQsLCAG4q4clZhBDDfJQVhWUEKxXAzyQYMj4Jmq1zmFwpRqxhAJFPOa0AsW_PTSshoPuMnXNwq3rU7Imp15QimXTjlXMx0nC088mt1rIwRs75GnLLugWjSllxgzvQ9YrP4tBgclK4_rb17hjnusGj_c0u2fx0AvVokjSNB-v2poTj0xT9BZRCbzRE3-lF1.jpg?1696514350",
  AUDIO: "https://coin-images.coingecko.com/coins/images/12913/large/audio-token-asset_2x.png",
  ATH: "https://coin-images.coingecko.com/coins/images/36179/large/logogram_circle_dark_green_vb_green_%281%29.png?1718232706",
  BEAT: "https://coin-images.coingecko.com/coins/images/70428/large/audiera.png",
  BILL: "https://coin-images.coingecko.com/coins/images/68464/large/billions.png",
  GRAM: "https://coin-images.coingecko.com/coins/images/17980/large/Gram_Circular_Badge.png",
  GIGGLE: "https://coin-images.coingecko.com/coins/images/69414/large/giggle-fund.jpg",
  JCT: "https://coin-images.coingecko.com/coins/images/70608/large/janction.png?1762738430",
  OPG: "https://coin-images.coingecko.com/coins/images/102172863/large/coingecko_logo.png",
  TRAC: "https://coin-images.coingecko.com/coins/images/1877/large/TRAC.jpg?1696502873",
  UNI: "https://coin-images.coingecko.com/coins/images/12504/large/uniswap-logo.png?1720676669",
  VANRY: "https://coin-images.coingecko.com/coins/images/33466/large/apple-touch-icon.png?1701942541",
  WLD: "https://coin-images.coingecko.com/coins/images/31069/large/worldcoin.jpeg?1696529903",
  WIF: "https://coin-images.coingecko.com/coins/images/33566/large/dogwifhat.jpg",
  XAUT: "https://coin-images.coingecko.com/coins/images/10481/large/logo.png",
  XEC: "https://coin-images.coingecko.com/coins/images/16646/large/Logo_final-22.png",
  XVG: "https://coin-images.coingecko.com/coins/images/203/large/Verge_Coin_%28native%29_icon_200x200.jpg?1699220755",
  XMR: "https://coin-images.coingecko.com/coins/images/69/large/monero_logo.png?1696501460",
  XRP: "https://coin-images.coingecko.com/coins/images/44/large/xrp-symbol-white-128.png?1696501442",
  YB: "https://coin-images.coingecko.com/coins/images/54871/large/yieldbasis_400x400.png?1760514173",
  ZBT: "https://coin-images.coingecko.com/coins/images/69446/large/zbt.png?1758621515",
  ZEC: "https://coin-images.coingecko.com/coins/images/486/large/circle-zcash-color.png?1696501740",
  ZEREBRO: "https://coin-images.coingecko.com/coins/images/51289/large/zerebro_2.png?1730588883",
  APT: "https://coin-images.coingecko.com/coins/images/26455/large/Aptos-Network-Symbol-Black-RGB-1x.png",
};

const LOCAL_ICONS: Record<string, { background: string; color?: string; glyph: string }> = {
  "1000XEC": { background: "#34acbc", color: "#0c0e12", glyph: "1" },
  ACE: { background: "transparent", color: "#d5d7d8", glyph: "A" },
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
  BABA: { background: "#bc3460", color: "#0c0e12", glyph: "B" },
  BLESS: { background: "transparent", color: "#0c0e12", glyph: "B" },
  SPCX: { background: "#111318", glyph: "×" },
  SAKE: { background: "#5a173f", glyph: "△" },
  JELLYJELLY: { background: "transparent", glyph: "J" },
  SNDK: { background: "#3468bc", color: "#0c0e12", glyph: "S" },
  SKHY: { background: "#be379a", color: "#0c0e12", glyph: "S" },
  SKHYNIX: { background: "#bc34a8", color: "#0c0e12", glyph: "S" },
  MU: { background: "#5cbc34", color: "#0c0e12", glyph: "M" },
  CL: { background: "#2bbf9b", glyph: "CL" },
  XAU: { background: "#d5ac3c", color: "#fff", glyph: "◈" },
  OP: { background: "#ff0420", glyph: "O" },
  PROM: { background: "transparent", glyph: "P" },
  SUI: { background: "#6fbcf0", glyph: "S" },
  ARB: { background: "#2d374b", glyph: "A" },
  UNI: { background: "#ff5db1", glyph: "U" },
};

const LOCAL_ONLY = new Set(["1000XEC", "BABA", "SPCX", "SAKE", "SNDK", "SKHY", "SKHYNIX", "MU", "CL", "XAU"]);

const SVG_FALLBACKS: Record<string, string> = {
  ACE: `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><path fill="#d5d7d8" d="M14 238 111 18h28l103 220h-63l-51-119-24 56h31l23 53H79l-13 30H14Z"/><path fill="#ff9d18" d="m96 238 32-72 32 72H96Z"/></svg>')}`,
  BLESS: `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><g fill="#0c0e12"><circle cx="92" cy="48" r="22"/><circle cx="164" cy="48" r="22"/><circle cx="56" cy="112" r="22"/><circle cx="128" cy="112" r="22"/><circle cx="200" cy="112" r="22"/><circle cx="92" cy="176" r="22"/><circle cx="164" cy="176" r="22"/><circle cx="128" cy="226" r="18"/></g></svg>')}`,
  JELLYJELLY: "data:image/webp;base64,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",
  PROM: `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><defs><linearGradient id="g" x1="0" x2="1" y1="1" y2="0"><stop stop-color="#f73b84"/><stop offset="1" stop-color="#f4de64"/></linearGradient></defs><path fill="url(#g)" d="M70 128V95c0-42 34-76 76-76 24 0 46 11 60 29l-26 20-110 60Zm0 19 123-84v69c0 30-18 58-46 70l-77 35v-90Z"/></svg>')}`,
  SUI: `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><circle cx="128" cy="128" r="128" fill="#eef5fb"/><path d="M128 38c-29 38-62 77-62 119a62 62 0 0 0 124 0c0-42-33-81-62-119Z" fill="none" stroke="#6fbcf0" stroke-linejoin="round" stroke-width="22"/><path d="M92 163c7 18 20 28 38 28 13 0 25-6 33-16" fill="none" stroke="#6fbcf0" stroke-linecap="round" stroke-width="16"/></svg>')}`,
  XVG: `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><rect width="256" height="256" fill="#050708"/><path fill="#34b9db" d="m43 46 37 1 48 105 48-105h37L128 218 43 46Zm56 1h58l-29 65-29-65Z"/></svg>')}`,
};

export function CoinIcon({ coin, iconUrl, size = 18 }: { coin?: string; iconUrl?: string; size?: number }) {
  const raw = String(coin || "?").replace(/USDT$/i, "").toUpperCase();
  const label = raw.slice(0, 2);
  const slug = ICON_ALIASES[raw] || raw.toLowerCase();
  const hue = [...label].reduce((sum, char) => sum + char.charCodeAt(0), 0) % 360;
  const local = LOCAL_ICONS[raw];
  const fallbackSource = SVG_FALLBACKS[raw];
  const source = iconUrl || fallbackSource || COINGECKO_ICONS[raw] || (LOCAL_ONLY.has(raw) ? "" : `https://cdn.jsdelivr.net/gh/atomiclabs/cryptocurrency-icons@${ICON_RELEASE}/128/color/${slug}.png`);
  return <span aria-label={`${raw} 图标`} className="relative grid shrink-0 place-items-center overflow-hidden rounded-full text-[7px] font-bold text-white" role="img" style={{ width: size, height: size, color: local?.color, background: local?.background || `linear-gradient(145deg,hsl(${hue} 72% 58%),hsl(${(hue + 32) % 360} 68% 43%))`, filter: raw === "APT" ? "grayscale(1)" : undefined }}>
    <span aria-hidden="true">{local?.glyph || label}</span>
    {source ? <img alt="" aria-hidden="true" className="absolute inset-0 h-full w-full object-cover" decoding="async" loading="eager" onError={(event) => { const image = event.currentTarget; if (fallbackSource && image.dataset.fallbackApplied !== "true") { image.dataset.fallbackApplied = "true"; image.src = fallbackSource; return; } image.style.display = "none"; }} src={source}/> : null}
  </span>;
}
