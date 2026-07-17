export function InlineEvidenceText({ text = "" }: { text?: string }) {
  const parts = String(text).split(/(`[^`]+`)/g);
  return (
    <>
      {parts.map((part, index) => part.startsWith("`") && part.endsWith("`")
        ? <code className="rounded bg-surface-container-low px-1 py-0.5 font-mono text-[0.92em] font-semibold text-text-primary" key={`${part}-${index}`}>{part.slice(1, -1)}</code>
        : <span key={`${part}-${index}`}>{part}</span>)}
    </>
  );
}
