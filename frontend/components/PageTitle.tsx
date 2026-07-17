export function PageTitle({ title, subtitle, tags = [] }: { title: string; subtitle: string; tags?: string[] }) {
  return (
    <section className="mb-4 border-b border-border-subtle pb-4">
      <div className="flex flex-col justify-between gap-3 md:flex-row md:items-end">
        <div>
          <h1 className="text-xl font-bold tracking-[-0.025em] text-text-primary md:text-[22px] md:leading-8">{title}</h1>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-text-secondary md:text-[13px]">{subtitle}</p>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {tags.map((tag) => (
            <span className="chip" key={tag}>
              {tag}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}
