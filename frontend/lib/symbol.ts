export function normalizeMarketSymbol(value: string): string {
  const clean = String(value || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (!clean || clean.length > 24) return "";
  const symbol = clean.endsWith("USDT") ? clean : `${clean}USDT`;
  return /^[A-Z0-9]{2,20}USDT$/.test(symbol) ? symbol : "";
}
